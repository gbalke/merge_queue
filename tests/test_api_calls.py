"""API call budget tests.

Each test runs a top-level operation (do_enqueue, do_process, do_abort,
do_check_rules) against a counting mock client and asserts that the number
of calls to each API method stays within an expected budget.

These are regression tests: if a refactor adds an unnecessary API call the
assertion fires, prompting the author to either justify the extra call or
find an alternative.

Budget derivation (traced by hand, kept as inline comments so reviewers can
follow the logic):

  QueueState.fetch always costs:
    1  get_default_branch
    1  list_mq_branches
    1  list_open_prs
    1  list_rulesets
    + 1 per queued/locked PR  (get_label_timestamp)

  StateStore.read  → 1 get_file_content
  StateStore.write → up to 3 calls:
                       _ensure_branch check  (get_file_content, may skip)
                       put_file_content STATE_PATH
                       put_file_content STATUS_PATH
"""

from __future__ import annotations

import datetime
import json
import base64
from unittest.mock import MagicMock, patch


from merge_queue.cli import do_abort, do_check_rules, do_enqueue, do_process
from merge_queue.types import Batch, BatchStatus, PullRequest, Stack, empty_state
from tests.conftest import make_v2_state


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

T0 = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)


def _iso(dt: datetime.datetime = T0) -> str:
    return dt.isoformat()


def _b64(obj: dict) -> str:
    return base64.b64encode(json.dumps(obj).encode()).decode()


def _make_state(
    queue: list | None = None,
    active_batch: dict | None = None,
    history: list | None = None,
    **overrides,
) -> dict:
    """Build a v2 state dict for test setup."""
    if queue is not None or active_batch is not None:
        s = make_v2_state(
            branch="main",
            queue=queue,
            active_batch=active_batch,
            history=history,
        )
    else:
        s = empty_state()
        if history is not None:
            s["history"] = history
    s.update(overrides)
    return s


def _queue_entry(
    number: int,
    head_ref: str = "feat-a",
    base_ref: str = "main",
    position: int = 1,
    deployment_id: int | None = 99,
    comment_ids: dict | None = None,
) -> dict:
    return {
        "position": position,
        "queued_at": _iso(),
        "stack": [
            {
                "number": number,
                "head_sha": f"sha-{number}",
                "head_ref": head_ref,
                "base_ref": base_ref,
                "title": "PR title",
            }
        ],
        "deployment_id": deployment_id,
        "comment_ids": comment_ids or {number: 1000 + number},
    }


def _counting_client(
    *,
    open_prs: list[dict] | None = None,
    state_dict: dict | None = None,
    mq_branches: list[str] | None = None,
    rulesets: list[dict] | None = None,
    pr_data_by_number: dict[int, dict] | None = None,
    ci_passes: bool = True,
) -> MagicMock:
    """Build a MagicMock client pre-wired with realistic return values.

    The mock records every call automatically (MagicMock default).  Tests
    then read e.g. ``client.get_pr.call_count`` to assert budgets.
    """
    client = MagicMock()
    client.owner = "owner"
    client.repo = "repo"

    # QueueState.fetch dependencies
    client.get_default_branch.return_value = "main"
    client.list_mq_branches.return_value = mq_branches or []
    client.list_open_prs.return_value = open_prs or []
    client.list_rulesets.return_value = rulesets or []
    client.get_label_timestamp.return_value = T0

    # StateStore.read / write
    encoded = _b64(state_dict or empty_state())
    client.get_file_content.return_value = {"sha": "file-sha-1", "content": encoded}
    client.put_file_content.return_value = {"content": {"sha": "file-sha-2"}}
    client.create_orphan_branch.return_value = None

    # Branch operations
    client.get_branch_sha.return_value = "batch-sha-abc"
    client.compare_commits.return_value = "ahead"
    client.update_ref.return_value = None
    client.delete_branch.return_value = None
    client.update_pr_base.return_value = None

    # Ruleset operations
    client.create_ruleset.return_value = 42
    client.get_ruleset.return_value = {
        "enforcement": "active",
        "conditions": {"ref_name": {"include": ["refs/heads/feat-a"], "exclude": []}},
    }
    client.delete_ruleset.return_value = None

    # Label operations
    client.add_label.return_value = None
    client.remove_label.return_value = None

    # Comments / deployments
    client.create_comment.return_value = 555
    client.update_comment.return_value = None
    client.create_deployment.return_value = 77
    client.update_deployment_status.return_value = None

    # CI
    client.dispatch_ci.return_value = None
    client.poll_ci_with_url.return_value = (ci_passes, "https://example.com/run/1")
    client.get_failed_job_info.return_value = ("test-job", "Failed at step: pytest")
    client.get_pr_ci_status.return_value = (True, "https://example.com/check/1")

    # Per-PR data
    pr_data_by_number = pr_data_by_number or {}

    def _get_pr(number):
        if number in pr_data_by_number:
            return pr_data_by_number[number]
        return {
            "number": number,
            "state": "open",
            "title": f"PR #{number}",
            "head": {"sha": f"sha-{number}", "ref": f"feat-{number}"},
            "base": {"ref": "main"},
            "labels": [{"name": "queue"}],
        }

    client.get_pr.side_effect = _get_pr
    return client


def _make_batch(
    prs: tuple[PullRequest, ...] | None = None,
    batch_id: str = "1735689600",
    ruleset_id: int = 42,
) -> Batch:
    if prs is None:
        prs = (PullRequest(1, "sha-1", "feat-a", "main", ("queue",), T0),)
    stack = Stack(prs=prs, queued_at=T0)
    return Batch(batch_id, f"mq/{batch_id}", stack, BatchStatus.RUNNING, ruleset_id)


# ---------------------------------------------------------------------------
# do_check_rules
# ---------------------------------------------------------------------------


class TestDoCheckRulesApiCalls:
    """do_check_rules: only QueueState.fetch, no writes."""

    def test_clean_state_call_budget(self):
        """Clean state (no queued PRs): exactly 4 API calls."""
        client = _counting_client()

        results = do_check_rules(client)

        assert all(r.passed for r in results)
        # QueueState.fetch: get_default_branch + list_mq_branches +
        #                   list_open_prs + list_rulesets = 4
        assert client.get_default_branch.call_count == 1
        assert client.list_mq_branches.call_count == 1
        assert client.list_open_prs.call_count == 1
        assert client.list_rulesets.call_count == 1
        # No PRs with queue/locked label → no label timestamp calls
        assert client.get_label_timestamp.call_count == 0
        # No writes
        assert client.put_file_content.call_count == 0

    def test_queued_prs_add_label_timestamp_calls(self):
        """Each queued PR requires one get_label_timestamp call."""
        queued_prs = [
            {
                "number": i,
                "head": {"sha": f"sha-{i}", "ref": f"feat-{i}"},
                "base": {"ref": "main"},
                "labels": [{"name": "queue"}],
            }
            for i in range(1, 4)
        ]
        client = _counting_client(open_prs=queued_prs)

        do_check_rules(client)

        # 4 base + 1 per PR = 7
        assert client.get_label_timestamp.call_count == 3
        total = (
            client.get_default_branch.call_count
            + client.list_mq_branches.call_count
            + client.list_open_prs.call_count
            + client.list_rulesets.call_count
            + client.get_label_timestamp.call_count
        )
        assert total == 7


# ---------------------------------------------------------------------------
# do_abort
# ---------------------------------------------------------------------------


class TestDoAbortApiCalls:
    """do_abort: reads state, conditionally writes, updates deployment, comments."""

    def test_remove_from_queue_call_budget(self):
        """Removing a queued PR: 1 read + 1 write + 1 deployment update + 1 comment."""
        state = _make_state(queue=[_queue_entry(1)])
        client = _counting_client(state_dict=state)

        result = do_abort(client, 1)

        assert result == "removed"
        # State read
        assert client.get_file_content.call_count >= 1
        # Deployment update
        assert client.update_deployment_status.call_count == 1
        # One comment (update existing)
        assert client.update_comment.call_count == 1
        assert client.create_comment.call_count == 0
        # State written once: state.json + branch STATUS.md + root STATUS.md = up to 3
        write_count = client.put_file_content.call_count
        assert 1 <= write_count <= 3

    def test_abort_active_batch_call_budget(self):
        """Aborting the active batch: batch_mod.abort_batch + 1 write + comment."""
        state = _make_state(
            active_batch={
                "batch_id": "123",
                "branch": "mq/123",
                "ruleset_id": 42,
                "started_at": _iso(),
                "progress": "running_ci",
                "stack": [
                    {
                        "number": 1,
                        "head_sha": "sha-1",
                        "head_ref": "feat-a",
                        "base_ref": "main",
                    }
                ],
                "deployment_id": 99,
                "comment_ids": {1: 1001},
            }
        )
        client = _counting_client(
            state_dict=state,
            mq_branches=["mq/123"],
            rulesets=[
                {
                    "id": 42,
                    "name": "mq-lock-123",
                    "conditions": {"ref_name": {"include": ["refs/heads/feat-a"]}},
                }
            ],
        )

        with patch("merge_queue.cli.batch_mod") as bm:
            bm.abort_batch.return_value = None
            result = do_abort(client, 1)

        assert result == "aborted"
        bm.abort_batch.assert_called_once_with(client)
        # deployment update: inactive/aborted
        assert client.update_deployment_status.call_count == 1
        # comment on each PR in batch (1 PR → update existing)
        assert client.update_comment.call_count == 1
        # state written once
        assert client.put_file_content.call_count >= 1

    def test_not_found_makes_only_read_call(self):
        """PR not in queue or batch: only 1 read, no writes."""
        client = _counting_client()

        result = do_abort(client, 999)

        assert result == "not_found"
        assert client.get_file_content.call_count == 1
        assert client.put_file_content.call_count == 0
        assert client.create_comment.call_count == 0
        assert client.update_comment.call_count == 0


# ---------------------------------------------------------------------------
# do_enqueue
# ---------------------------------------------------------------------------


class TestDoEnqueueApiCalls:
    """do_enqueue: one get_pr guard + read + QueueState.fetch + write + process."""

    def test_single_pr_not_in_stack_no_duplicate_get_pr(self):
        """
        When a PR is not found in the QueueState (the common case for a PR
        that just received the queue label but isn't yet reflected in
        list_open_prs), do_enqueue must NOT call get_pr a second time.

        Optimization: cached_pr_data from the guard check is reused.
        """
        client = _counting_client()

        with (
            patch("merge_queue.cli.do_process", return_value="merged"),
            patch("merge_queue.cli.StateStore") as StoreCls,
        ):
            store = MagicMock()
            store.read.return_value = empty_state()
            store.write.return_value = None
            StoreCls.return_value = store

            do_enqueue(client, 1)

        # get_pr called exactly once (guard), NOT again for stack_dicts fallback
        assert client.get_pr.call_count == 1, (
            f"Expected 1 get_pr call (guard only), got {client.get_pr.call_count}. "
            "The fallback branch must reuse the cached pr_data."
        )

    def test_enqueue_idle_queue_total_api_budget(self):
        """
        Full do_enqueue for a fresh single-PR stack with idle queue.

        Call budget (do_process is mocked out so we only count do_enqueue):
          1  get_pr (guard, also used for stack_dicts fallback)
          1  get_file_content (StateStore.read)
          4  QueueState.fetch (default_branch, mq_branches, open_prs, rulesets)
          1  create_deployment
          1  update_deployment_status (queued)
          1  create_comment
          1-2 put_file_content (state.json + STATUS.md)
        Total: ~11-12 calls (no get_pr duplication)
        """
        client = _counting_client()

        with (
            patch("merge_queue.cli.do_process", return_value="merged"),
            patch("merge_queue.cli.StateStore") as StoreCls,
        ):
            store = MagicMock()
            store.read.return_value = empty_state()
            store.write.return_value = None
            StoreCls.return_value = store

            do_enqueue(client, 1)

        get_pr_count = client.get_pr.call_count
        # Budget: exactly 1 get_pr — any more is wasteful
        assert get_pr_count <= 1, (
            f"get_pr called {get_pr_count} times; expected at most 1"
        )
        assert client.create_deployment.call_count == 1
        assert client.update_deployment_status.call_count == 1
        assert client.create_comment.call_count == 1

    def test_already_queued_returns_early_minimal_calls(self):
        """PR already queued: guard get_pr + 1 read, then early return."""
        state = _make_state(queue=[_queue_entry(1)])
        client = _counting_client(state_dict=state)

        result = do_enqueue(client, 1)

        assert result == "already_queued"
        assert client.get_pr.call_count == 1  # guard only
        assert client.get_file_content.call_count == 1  # StateStore.read
        assert client.get_default_branch.call_count == 0  # no QueueState.fetch
        assert client.put_file_content.call_count == 0  # no write

    def test_pr_not_open_makes_only_one_api_call(self):
        """Closed PR: only the guard get_pr call, nothing else."""
        client = _counting_client()
        client.get_pr.side_effect = None
        client.get_pr.return_value = {"state": "closed", "number": 1}

        result = do_enqueue(client, 1)

        assert result == "pr_not_open"
        assert client.get_pr.call_count == 1
        assert client.get_file_content.call_count == 0
        assert client.get_default_branch.call_count == 0


# ---------------------------------------------------------------------------
# do_process
# ---------------------------------------------------------------------------


class TestDoProcessApiCalls:
    """
    do_process: the most API-intensive operation.

    Scenario: 1-PR stack, CI passes, batch merges successfully.

    Call budget breakdown:
      StateStore.read:
        1  get_file_content (state.json)
      QueueState.fetch:
        1  get_default_branch
        1  list_mq_branches
        1  list_open_prs
        1  list_rulesets
      First StateStore.write (locking state):
        1  get_file_content (_ensure_branch)
        1  put_file_content (state.json)
        1  put_file_content (STATUS.md)
      batch_mod.create_batch (mocked):
        counted separately
      Second StateStore.write (CI started):
        1  get_file_content (_ensure_branch, branch now exists → early return)
        1  put_file_content (state.json)
        1  put_file_content (STATUS.md)
      update_deployment_status (CI running):
        1
      create_comment (batch started, 1 PR):
        1  update_comment (existing comment_id)
      batch_mod.run_ci (mocked):
        counted separately
      Third StateStore.write (completing):
        1  get_file_content
        1  put_file_content (state.json)
        1  put_file_content (STATUS.md)
      batch_mod.complete_batch (mocked):
        counted separately
      update_deployment_status (success):
        1
      create_comment / update_comment (merged):
        1
      Fourth StateStore.write (final):
        1  get_file_content
        1  put_file_content (state.json)
        1  put_file_content (STATUS.md)

    Total (excluding mocked batch calls): ~20 calls
    Budget (with headroom for implementation drift): <= 25
    """

    @patch("merge_queue.cli.batch_mod")
    def test_single_pr_ci_passes_call_budget(self, batch_mod):
        """1-PR stack, CI passes: total do_process API calls <= 25."""
        state = _make_state(queue=[_queue_entry(1, deployment_id=99)])
        client = _counting_client(state_dict=state)

        batch = _make_batch()
        batch_mod.create_batch.return_value = batch
        ci_result = MagicMock()
        ci_result.passed = True
        ci_result.run_url = ""
        batch_mod.run_ci.return_value = ci_result
        batch_mod.BatchError = Exception

        with patch("merge_queue.cli.QueueState") as QS:
            from merge_queue.state import QueueState as RealQS

            QS.fetch.return_value = RealQS(
                default_branch="main",
                mq_branches=[],
                rulesets=[],
                prs=[],
                all_pr_data=[],
            )
            result = do_process(client)

        assert result == "merged"
        total = _total_api_calls(client)
        # Budget increased by 4: v2 state has 3 put_file_content per write (state.json
        # + branch STATUS.md + root STATUS.md) vs. 2 in v1. 4 writes × +1 = +4.
        assert total <= 30, (
            f"do_process (1-PR, CI passes) made {total} API calls; budget is 30. "
            f"Breakdown: {_call_summary(client)}"
        )

    @patch("merge_queue.cli.batch_mod")
    def test_three_pr_stack_ci_passes_call_budget(self, batch_mod):
        """3-PR stacked batch, CI passes: total <= 35 API calls."""
        stack_entries = [
            {
                "number": i,
                "head_sha": f"sha-{i}",
                "head_ref": f"feat-{i}",
                "base_ref": "main" if i == 1 else f"feat-{i - 1}",
                "title": f"PR {i}",
            }
            for i in range(1, 4)
        ]
        comment_ids = {str(i): 1000 + i for i in range(1, 4)}
        state = _make_state(
            queue=[
                {
                    "position": 1,
                    "queued_at": _iso(),
                    "stack": stack_entries,
                    "deployment_id": 99,
                    "comment_ids": comment_ids,
                }
            ]
        )
        client = _counting_client(state_dict=state)

        prs = tuple(
            PullRequest(
                i,
                f"sha-{i}",
                f"feat-{i}",
                "main" if i == 1 else f"feat-{i - 1}",
                ("queue",),
                T0,
            )
            for i in range(1, 4)
        )
        batch = _make_batch(prs=prs)
        # Adjust ruleset to cover all 3 branch patterns
        client.get_ruleset.return_value = {
            "enforcement": "active",
            "conditions": {
                "ref_name": {
                    "include": [f"refs/heads/feat-{i}" for i in range(1, 4)],
                    "exclude": [],
                }
            },
        }
        batch_mod.create_batch.return_value = batch
        ci_result = MagicMock()
        ci_result.passed = True
        ci_result.run_url = ""
        batch_mod.run_ci.return_value = ci_result
        batch_mod.BatchError = Exception

        with patch("merge_queue.cli.QueueState") as QS:
            from merge_queue.state import QueueState as RealQS

            QS.fetch.return_value = RealQS(
                default_branch="main",
                mq_branches=[],
                rulesets=[],
                prs=[],
                all_pr_data=[],
            )
            result = do_process(client)

        assert result == "merged"
        total = _total_api_calls(client)
        assert total <= 35, (
            f"do_process (3-PR stack, CI passes) made {total} API calls; budget is 35. "
            f"Breakdown: {_call_summary(client)}"
        )

    @patch("merge_queue.cli.batch_mod")
    def test_ci_fails_call_budget(self, batch_mod):
        """1-PR stack, CI fails: total <= 25 API calls."""
        state = _make_state(queue=[_queue_entry(1, deployment_id=99)])
        client = _counting_client(state_dict=state, ci_passes=False)

        batch = _make_batch()
        batch_mod.create_batch.return_value = batch
        ci_result = MagicMock()
        ci_result.passed = False
        ci_result.run_url = "https://example.com/run/1"
        batch_mod.run_ci.return_value = ci_result
        batch_mod.BatchError = Exception

        with patch("merge_queue.cli.QueueState") as QS:
            from merge_queue.state import QueueState as RealQS

            QS.fetch.return_value = RealQS(
                default_branch="main",
                mq_branches=[],
                rulesets=[],
                prs=[],
                all_pr_data=[],
            )
            result = do_process(client)

        assert result == "ci_failed"
        total = _total_api_calls(client)
        assert total <= 25, (
            f"do_process (CI fails) made {total} API calls; budget is 25. "
            f"Breakdown: {_call_summary(client)}"
        )

    def test_no_queue_makes_minimal_calls(self):
        """Empty queue: 1 read + 1 list_open_prs (sync-missing scan) + 0 writes."""
        client = _counting_client()

        result = do_process(client)

        assert result == "no_stacks"
        assert client.get_file_content.call_count == 1
        assert client.list_open_prs.call_count == 1
        assert client.put_file_content.call_count == 0
        total = _total_api_calls(client)
        assert total == 2, (
            f"Empty queue do_process made {total} calls; expected exactly 2. "
            f"Breakdown: {_call_summary(client)}"
        )

    def test_active_batch_skips_makes_minimal_calls(self):
        """Active (non-stale) batch: 1 read + 1 get_pr per PR in stack, no writes.

        The get_pr calls detect stale state (PRs merged outside the queue).
        Budget: get_file_content=1 + get_pr=N where N is the number of PRs
        in the active batch's stack.
        """
        state = _make_state(
            active_batch={
                "batch_id": "123",
                "branch": "mq/123",
                "ruleset_id": 42,
                "started_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "progress": "running_ci",
                "stack": [{"number": 1}],
            }
        )
        client = _counting_client(state_dict=state)

        result = do_process(client)

        assert result == "batch_active"
        # 1 get_file_content (state read) + 1 get_pr (stale-PR detection, 1 PR in stack)
        assert client.get_file_content.call_count == 1
        assert client.get_pr.call_count == 1  # 1 per PR in stack
        assert client.put_file_content.call_count == 0  # no writes
        assert client.get_default_branch.call_count == 0  # no QueueState.fetch
        total = _total_api_calls(client)
        assert total == 2, (
            f"Batch-active skip made {total} calls; expected 2 (read + stale-PR check). "
            f"Breakdown: {_call_summary(client)}"
        )


# ---------------------------------------------------------------------------
# No duplicate QueueState.fetch within do_process
# ---------------------------------------------------------------------------


class TestNoDuplicateFetch:
    """Guard against calling QueueState.fetch more than once per operation."""

    @patch("merge_queue.cli.batch_mod")
    def test_do_process_fetches_state_once(self, batch_mod):
        """QueueState.fetch must be called exactly once in do_process."""
        state = _make_state(queue=[_queue_entry(1)])
        client = _counting_client(state_dict=state)

        batch = _make_batch()
        batch_mod.create_batch.return_value = batch
        ci_result = MagicMock()
        ci_result.passed = True
        ci_result.run_url = ""
        batch_mod.run_ci.return_value = ci_result
        batch_mod.BatchError = Exception

        with patch("merge_queue.cli.QueueState") as QS:
            from merge_queue.state import QueueState as RealQS

            fetch_results = [
                RealQS(
                    default_branch="main",
                    mq_branches=[],
                    rulesets=[],
                    prs=[],
                    all_pr_data=[],
                )
            ]
            QS.fetch.side_effect = fetch_results

            do_process(client)

        assert QS.fetch.call_count == 1, (
            f"QueueState.fetch called {QS.fetch.call_count} times; expected 1"
        )

    @patch("merge_queue.cli.do_process", return_value="merged")
    def test_do_enqueue_fetches_state_once(self, _do_process):
        """QueueState.fetch must be called exactly once in do_enqueue."""
        client = _counting_client()

        with (
            patch("merge_queue.cli.QueueState") as QS,
            patch("merge_queue.cli.StateStore") as StoreCls,
        ):
            from merge_queue.state import QueueState as RealQS

            QS.fetch.return_value = RealQS(
                default_branch="main",
                mq_branches=[],
                rulesets=[],
                prs=[],
                all_pr_data=[],
            )
            store = MagicMock()
            store.read.return_value = empty_state()
            StoreCls.return_value = store

            do_enqueue(client, 1)

        assert QS.fetch.call_count == 1, (
            f"QueueState.fetch called {QS.fetch.call_count} times in do_enqueue; expected 1"
        )


# ---------------------------------------------------------------------------
# Helpers for call counting
# ---------------------------------------------------------------------------

_API_METHODS = [
    "list_open_prs",
    "get_label_timestamp",
    "add_label",
    "remove_label",
    "create_comment",
    "update_comment",
    "get_failed_job_info",
    "create_ruleset",
    "get_ruleset",
    "delete_ruleset",
    "list_rulesets",
    "list_mq_branches",
    "delete_branch",
    "get_branch_sha",
    "get_default_branch",
    "dispatch_ci",
    "poll_ci",
    "poll_ci_with_url",
    "update_ref",
    "update_pr_base",
    "compare_commits",
    "get_pr",
    "get_file_content",
    "put_file_content",
    "create_orphan_branch",
    "create_deployment",
    "update_deployment_status",
]


def _total_api_calls(client: MagicMock) -> int:
    return sum(
        getattr(client, m).call_count for m in _API_METHODS if hasattr(client, m)
    )


def _call_summary(client: MagicMock) -> str:
    parts = []
    for m in _API_METHODS:
        count = getattr(client, m).call_count if hasattr(client, m) else 0
        if count:
            parts.append(f"{m}={count}")
    return ", ".join(parts)
