"""Tests for multi-branch target support via merge-queue.yml config."""

from __future__ import annotations

import base64
import datetime
from unittest.mock import MagicMock, patch

import pytest

from merge_queue.cli import do_enqueue, do_process
from merge_queue.config import get_target_branches
from merge_queue.types import empty_state

T0 = datetime.datetime(2026, 1, 1, 0, 0, 0, tzinfo=datetime.timezone.utc)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _encode_yaml(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


def _make_client(
    default_branch: str = "main",
    config_content: str | None = None,
) -> MagicMock:
    """Return a minimal mock client.

    If *config_content* is given it is returned as the merge-queue.yml body.
    Otherwise, ``get_file_content`` raises a 404-style exception.
    """
    client = MagicMock()
    client.owner = "owner"
    client.repo = "repo"
    client.get_default_branch.return_value = default_branch

    if config_content is not None:
        client.get_file_content.return_value = {"content": _encode_yaml(config_content)}
    else:
        client.get_file_content.side_effect = Exception("404 Not Found")

    return client


def _make_enqueue_client(
    base_ref: str = "main",
    default_branch: str = "main",
    config_content: str | None = None,
) -> MagicMock:
    """Return a fully-configured mock client for do_enqueue tests."""
    client = _make_client(default_branch=default_branch, config_content=config_content)
    client.get_pr.return_value = {
        "state": "open",
        "head": {"ref": "feat-x", "sha": "abc123"},
        "base": {"ref": base_ref},
        "title": "Test PR",
        "labels": [{"name": "queue"}],
    }
    client.list_open_prs.return_value = []
    client.list_mq_branches.return_value = []
    client.list_rulesets.return_value = []
    client.get_pr_ci_status.return_value = (True, "")
    client.create_comment.return_value = 42
    client.create_deployment.return_value = 99
    client.update_deployment_status.return_value = None
    return client


@pytest.fixture
def mock_store():
    with patch("merge_queue.cli.StateStore") as cls:
        store = MagicMock()
        store.read.return_value = empty_state()
        cls.return_value = store
        yield store


# ---------------------------------------------------------------------------
# get_target_branches
# ---------------------------------------------------------------------------


class TestGetTargetBranches:
    def test_returns_default_branch_when_no_config_file(self):
        client = _make_client(default_branch="main")
        assert get_target_branches(client) == ["main"]

    def test_returns_default_branch_when_section_absent(self):
        config = "break_glass_users:\n  - alice\n"
        client = _make_client(config_content=config)
        assert get_target_branches(client) == ["main"]

    def test_returns_default_branch_when_list_empty(self):
        # An empty target_branches section (no items)
        config = "target_branches:\nbreak_glass_users:\n  - alice\n"
        client = _make_client(config_content=config)
        # No items parsed → fall back to default branch
        assert get_target_branches(client) == ["main"]

    def test_parses_single_target_branch(self):
        config = "target_branches:\n  - main\n"
        client = _make_client(config_content=config)
        assert get_target_branches(client) == ["main"]

    def test_parses_multiple_target_branches(self):
        config = "target_branches:\n  - main\n  - release/1.0\n"
        client = _make_client(config_content=config)
        assert get_target_branches(client) == ["main", "release/1.0"]

    def test_parses_branches_alongside_other_keys(self):
        config = (
            "target_branches:\n"
            "  - main\n"
            "  - release/2.0\n"
            "break_glass_users:\n"
            "  - gbalke\n"
        )
        client = _make_client(config_content=config)
        assert get_target_branches(client) == ["main", "release/2.0"]

    def test_stops_at_next_key_boundary(self):
        config = "target_branches:\n  - main\nother_key: true\n"
        client = _make_client(config_content=config)
        assert get_target_branches(client) == ["main"]

    def test_returns_default_branch_on_client_error(self):
        client = MagicMock()
        client.get_default_branch.return_value = "develop"
        client.get_file_content.side_effect = Exception("network error")
        assert get_target_branches(client) == ["develop"]

    @pytest.mark.parametrize(
        "default_branch",
        ["main", "master", "develop", "trunk"],
    )
    def test_backward_compat_various_default_branches(self, default_branch):
        """No config → always returns [default_branch], whatever it is."""
        client = _make_client(default_branch=default_branch)
        assert get_target_branches(client) == [default_branch]


# ---------------------------------------------------------------------------
# do_enqueue — target branch routing
# ---------------------------------------------------------------------------


def _api_state_with_active_batch():
    """Return a QueueState that looks like a batch is in progress.

    Setting mq_branches to a non-empty list makes has_active_batch return True,
    so do_enqueue will not trigger do_process and there will be exactly one
    store.write call — the enqueue write — making assertions straightforward.
    """
    from tests.conftest import make_api_state

    return make_api_state(mq_branches=["mq/active"])


def _get_branch_queue(written_state: dict, branch: str) -> list:
    """Extract the queue list for a branch from a v2 state dict."""
    return written_state.get("branches", {}).get(branch, {}).get("queue", [])


class TestDoEnqueueTargetBranch:
    def test_enqueue_pr_targeting_main_no_config(self, mock_store):
        """PR targeting main works without any config (backward compat)."""
        client = _make_enqueue_client(base_ref="main")

        with patch("merge_queue.cli.QueueState") as qs:
            qs.fetch.return_value = _api_state_with_active_batch()
            result = do_enqueue(client, 42)

        assert result == "queued_waiting"
        written_state = mock_store.write.call_args[0][0]
        queue = _get_branch_queue(written_state, "main")
        assert queue, "Expected at least one queue entry"
        assert queue[-1]["target_branch"] == "main"

    def test_enqueue_pr_targeting_release_branch(self, mock_store):
        """PR targeting release/1.0 is stored with that target branch."""
        config = "target_branches:\n  - main\n  - release/1.0\n"
        client = _make_enqueue_client(base_ref="release/1.0", config_content=config)

        with patch("merge_queue.cli.QueueState") as qs:
            qs.fetch.return_value = _api_state_with_active_batch()
            result = do_enqueue(client, 42)

        assert result == "queued_waiting"
        written_state = mock_store.write.call_args[0][0]
        queue = _get_branch_queue(written_state, "release/1.0")
        assert queue, "Expected at least one queue entry for release/1.0"
        assert queue[-1]["target_branch"] == "release/1.0"

    def test_enqueue_stores_target_branch_in_entry(self, mock_store):
        """Queue entry dict contains 'target_branch' field."""
        config = "target_branches:\n  - main\n  - release/1.0\n"
        client = _make_enqueue_client(base_ref="main", config_content=config)

        with patch("merge_queue.cli.QueueState") as qs:
            qs.fetch.return_value = _api_state_with_active_batch()
            do_enqueue(client, 42)

        written_state = mock_store.write.call_args[0][0]
        queue = _get_branch_queue(written_state, "main")
        assert queue
        assert "target_branch" in queue[-1]

    def test_backward_compat_no_config_uses_default_branch(self, mock_store):
        """Without merge-queue.yml, enqueue defaults to the repo default branch."""
        client = _make_enqueue_client(base_ref="main")

        with patch("merge_queue.cli.QueueState") as qs:
            qs.fetch.return_value = _api_state_with_active_batch()
            do_enqueue(client, 42)

        written_state = mock_store.write.call_args[0][0]
        queue = _get_branch_queue(written_state, "main")
        assert queue
        assert queue[-1]["target_branch"] == "main"

    @pytest.mark.parametrize(
        "chain, expected_target",
        [
            # Direct: PR targets main → resolved immediately
            ([], "main"),
            # One hop: PR targets greg/feature (head of PR #100 whose base is main)
            ([("greg/feature", "main")], "main"),
            # Deep chain: PR #102 → PR #101 → PR #100 → main
            ([("greg/feat-b", "greg/feat-a"), ("greg/feat-a", "main")], "main"),
        ],
    )
    def test_stacked_pr_chain_resolves_to_target(
        self,
        mock_store,
        chain: list[tuple[str, str]],
        expected_target: str,
    ) -> None:
        """PRs targeting another PR's branch resolve to the ultimate target."""
        pr_base_ref = chain[0][0] if chain else "main"
        client = _make_enqueue_client(base_ref=pr_base_ref)
        client.list_open_prs.return_value = [
            {
                "head": {"ref": head_ref, "sha": f"sha-{head_ref}"},
                "base": {"ref": base_ref},
                "number": idx + 100,
                "labels": [],
            }
            for idx, (head_ref, base_ref) in enumerate(chain)
        ]

        with patch("merge_queue.cli.QueueState") as qs:
            qs.fetch.return_value = _api_state_with_active_batch()
            result = do_enqueue(client, 42)

        assert result == "queued_waiting"
        written_state = mock_store.write.call_args[0][0]
        queue = _get_branch_queue(written_state, expected_target)
        assert queue, f"Expected entry in {expected_target} queue"
        assert queue[-1]["target_branch"] == expected_target

    def test_stacked_pr_unresolvable_chain_rejected(self, mock_store) -> None:
        """PR whose chain never reaches a target branch is rejected."""
        client = _make_enqueue_client(base_ref="greg/orphan-branch")
        client.list_open_prs.return_value = [
            {
                "head": {"ref": "greg/orphan-branch", "sha": "sha-a"},
                "base": {"ref": "greg/dead-end"},
                "number": 55,
                "labels": [],
            }
        ]

        with patch("merge_queue.cli.QueueState") as qs:
            qs.fetch.return_value = _api_state_with_active_batch()
            result = do_enqueue(client, 42)

        assert result == "invalid_target"


# ---------------------------------------------------------------------------
# do_process — uses target_branch from queue entry
# ---------------------------------------------------------------------------


def _make_process_client(default_branch: str = "main") -> MagicMock:
    client = MagicMock()
    client.owner = "owner"
    client.repo = "repo"
    client.get_default_branch.return_value = default_branch
    client.list_mq_branches.return_value = []
    client.list_rulesets.return_value = []
    client.list_open_prs.return_value = []
    client.get_pr.return_value = {"state": "open"}
    client.get_branch_sha.return_value = "sha-batch"
    client.compare_commits.return_value = "ahead"
    client.create_ruleset.return_value = 42
    client.get_ruleset.return_value = {
        "enforcement": "active",
        "conditions": {"ref_name": {"include": ["refs/heads/feat-x"]}},
    }
    client.poll_ci_with_url.return_value = (True, "")
    client.get_pr_ci_status.return_value = (True, "")
    client.create_comment.return_value = 77
    client.get_file_content.side_effect = Exception("404")
    return client


def _queue_entry(
    number: int = 1,
    head_ref: str = "feat-x",
    base_ref: str = "main",
    target_branch: str = "main",
) -> dict:
    return {
        "position": 1,
        "queued_at": T0.isoformat(),
        "stack": [
            {
                "number": number,
                "head_sha": f"sha-{number}",
                "head_ref": head_ref,
                "base_ref": base_ref,
                "title": "PR title",
            }
        ],
        "deployment_id": 99,
        "comment_ids": {number: 1000 + number},
        "target_branch": target_branch,
    }


class TestDoProcessTargetBranch:
    def test_passes_target_branch_to_complete_batch(self):
        """complete_batch receives the target_branch stored in the queue entry."""
        client = _make_process_client()
        entry = _queue_entry(target_branch="release/1.0", base_ref="release/1.0")

        with (
            patch("merge_queue.cli.StateStore") as store_cls,
            patch("merge_queue.cli.QueueState") as qs,
            patch("merge_queue.batch.complete_batch") as mock_complete,
        ):
            from tests.conftest import make_api_state, make_v2_state

            store = MagicMock()
            store.read.return_value = make_v2_state(branch="release/1.0", queue=[entry])
            store_cls.return_value = store
            qs.fetch.return_value = make_api_state()
            mock_complete.return_value = None
            client.list_open_prs.return_value = [
                {
                    "number": 1,
                    "head": {"ref": "feat-x", "sha": "sha-1"},
                    "base": {"ref": "release/1.0"},
                    "labels": [{"name": "queue"}],
                }
            ]

            with patch("merge_queue.cli.batch_mod") as mock_batch_mod:
                mock_batch = MagicMock()
                mock_batch.batch_id = "ts123"
                mock_batch.branch = "mq/release/1.0/ts123"
                mock_batch.ruleset_id = 42
                mock_batch.stack.prs = []
                mock_batch_mod.create_batch.return_value = mock_batch
                mock_batch_mod.run_ci.return_value = MagicMock(passed=True, run_url="")
                mock_batch_mod.complete_batch.return_value = None

                do_process(client)

                mock_batch_mod.complete_batch.assert_called_once()
                call_kwargs = mock_batch_mod.complete_batch.call_args
                assert call_kwargs.kwargs.get("target_branch") == "release/1.0"

    def test_stores_target_branch_in_active_batch(self):
        """active_batch dict written to state includes the target_branch field."""
        client = _make_process_client()
        entry = _queue_entry(target_branch="release/1.0", base_ref="release/1.0")

        captured_states: list[dict] = []

        with (
            patch("merge_queue.cli.StateStore") as store_cls,
            patch("merge_queue.cli.QueueState") as qs,
        ):
            from tests.conftest import make_api_state, make_v2_state

            store = MagicMock()
            store.read.return_value = make_v2_state(branch="release/1.0", queue=[entry])
            store.write.side_effect = lambda s: captured_states.append(
                {"branches": {k: dict(v) for k, v in s.get("branches", {}).items()}}
            )
            store_cls.return_value = store
            qs.fetch.return_value = make_api_state()
            client.list_open_prs.return_value = [
                {
                    "number": 1,
                    "head": {"ref": "feat-x", "sha": "sha-1"},
                    "base": {"ref": "release/1.0"},
                    "labels": [{"name": "queue"}],
                }
            ]

            with patch("merge_queue.cli.batch_mod") as mock_batch_mod:
                mock_batch = MagicMock()
                mock_batch.batch_id = "ts456"
                mock_batch.branch = "mq/release/1.0/ts456"
                mock_batch.ruleset_id = 42
                mock_batch.stack.prs = []
                mock_batch_mod.create_batch.return_value = mock_batch
                mock_batch_mod.run_ci.return_value = MagicMock(passed=False, run_url="")
                mock_batch_mod.fail_batch.return_value = None
                mock_batch_mod.complete_batch.return_value = None

                do_process(client)

            active_batch_writes = [
                s["branches"].get("release/1.0", {}).get("active_batch")
                for s in captured_states
                if s.get("branches", {}).get("release/1.0", {}).get("active_batch")
                is not None
            ]
            assert active_batch_writes, "active_batch was never written to state"
            assert active_batch_writes[0]["target_branch"] == "release/1.0"

    def test_backward_compat_entry_without_target_branch(self):
        """Entries lacking target_branch (old state) fall back to target_branch_to_process."""
        client = _make_process_client(default_branch="main")
        entry = _queue_entry()
        del entry["target_branch"]

        with (
            patch("merge_queue.cli.StateStore") as store_cls,
            patch("merge_queue.cli.QueueState") as qs,
        ):
            from tests.conftest import make_api_state, make_v2_state

            store = MagicMock()
            store.read.return_value = make_v2_state(branch="main", queue=[entry])
            store_cls.return_value = store
            qs.fetch.return_value = make_api_state()
            client.list_open_prs.return_value = [
                {
                    "number": 1,
                    "head": {"ref": "feat-x", "sha": "sha-1"},
                    "base": {"ref": "main"},
                    "labels": [{"name": "queue"}],
                }
            ]

            with patch("merge_queue.cli.batch_mod") as mock_batch_mod:
                mock_batch = MagicMock()
                mock_batch.batch_id = "ts789"
                mock_batch.branch = "mq/main/ts789"
                mock_batch.ruleset_id = 42
                mock_batch.stack.prs = []
                mock_batch_mod.create_batch.return_value = mock_batch
                mock_batch_mod.run_ci.return_value = MagicMock(passed=True, run_url="")
                mock_batch_mod.complete_batch.return_value = None

                do_process(client)

                mock_batch_mod.complete_batch.assert_called_once()
                call_kwargs = mock_batch_mod.complete_batch.call_args
                assert call_kwargs.kwargs.get("target_branch") == "main"


# ---------------------------------------------------------------------------
# complete_batch — target_branch parameter
# ---------------------------------------------------------------------------


class TestCompleteBatchTargetBranch:
    def test_uses_provided_target_branch(self):
        """complete_batch uses the target_branch arg, not get_default_branch."""
        from tests.conftest import make_batch, make_pr, make_stack

        client = MagicMock()
        client.get_default_branch.return_value = "main"
        client.get_branch_sha.return_value = "sha-batch"
        client.compare_commits.return_value = "ahead"
        client.get_ruleset.return_value = {
            "enforcement": "active",
            "conditions": {"ref_name": {"include": []}},
        }

        pr = make_pr(1, "feat-x", base_ref="release/1.0")
        stack = make_stack(pr)
        batch = make_batch(stack)

        with (
            patch("merge_queue.batch._parallel_cleanup") as mock_cleanup,
            patch("merge_queue.batch.time"),
        ):
            from merge_queue.batch import complete_batch

            complete_batch(client, batch, target_branch="release/1.0")

        # compare_commits and update_ref must reference release/1.0, not main
        client.compare_commits.assert_called_once_with("release/1.0", "sha-batch")
        client.update_ref.assert_called_once_with("release/1.0", "sha-batch")
        # update_pr_base retargets each PR to release/1.0
        client.update_pr_base.assert_called_once_with(1, "release/1.0")
        # Cleanup receives the correct branch
        mock_cleanup.assert_called_once_with(client, batch, "release/1.0")

    def test_defaults_to_get_default_branch_when_no_arg(self):
        """Omitting target_branch preserves the old behaviour."""
        from tests.conftest import make_batch, make_pr, make_stack

        client = MagicMock()
        client.get_default_branch.return_value = "main"
        client.get_branch_sha.return_value = "sha-batch"
        client.compare_commits.return_value = "ahead"

        pr = make_pr(1, "feat-x")
        stack = make_stack(pr)
        batch = make_batch(stack)

        with (
            patch("merge_queue.batch._parallel_cleanup"),
            patch("merge_queue.batch.time"),
        ):
            from merge_queue.batch import complete_batch

            complete_batch(client, batch)

        client.compare_commits.assert_called_once_with("main", "sha-batch")
        client.update_ref.assert_called_once_with("main", "sha-batch")
