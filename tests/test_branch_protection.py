"""Tests for auto-configuration of branch protection rulesets."""

from __future__ import annotations

import base64
import datetime
from unittest.mock import MagicMock, patch

import pytest

from merge_queue.cli import do_enqueue, do_process
from merge_queue.config import ensure_branch_protection
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
    existing_rulesets: list[dict] | None = None,
) -> MagicMock:
    client = MagicMock()
    client.owner = "owner"
    client.repo = "repo"
    client.get_default_branch.return_value = default_branch

    if config_content is not None:
        client.get_file_content.return_value = {"content": _encode_yaml(config_content)}
    else:
        client.get_file_content.side_effect = Exception("404 Not Found")

    client.list_rulesets.return_value = existing_rulesets or []
    client.create_protection_ruleset.return_value = 101
    return client


def _make_enqueue_client(
    base_ref: str = "main",
    default_branch: str = "main",
    config_content: str | None = None,
    existing_rulesets: list[dict] | None = None,
) -> MagicMock:
    client = _make_client(
        default_branch=default_branch,
        config_content=config_content,
        existing_rulesets=existing_rulesets,
    )
    client.get_pr.return_value = {
        "state": "open",
        "head": {"ref": "feat-x", "sha": "abc123"},
        "base": {"ref": base_ref},
        "title": "Test PR",
        "labels": [{"name": "queue"}],
    }
    client.list_open_prs.return_value = []
    client.list_mq_branches.return_value = []
    client.get_pr_ci_status.return_value = (True, "")
    client.create_comment.return_value = 42
    client.create_deployment.return_value = 99
    client.update_deployment_status.return_value = None
    return client


def _make_process_client(
    default_branch: str = "main",
    config_content: str | None = None,
    existing_rulesets: list[dict] | None = None,
) -> MagicMock:
    client = _make_client(
        default_branch=default_branch,
        config_content=config_content,
        existing_rulesets=existing_rulesets,
    )
    client.list_mq_branches.return_value = []
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
    return client


def _ruleset_fixture(name: str, branch: str) -> dict:
    """Build a minimal ruleset dict as returned by list_rulesets."""
    return {
        "name": name,
        "conditions": {
            "ref_name": {
                "include": [f"refs/heads/{branch}"],
                "exclude": [],
            }
        },
    }


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


@pytest.fixture
def mock_store():
    with patch("merge_queue.cli.StateStore") as cls:
        store = MagicMock()
        store.read.return_value = empty_state()
        cls.return_value = store
        yield store


# ---------------------------------------------------------------------------
# ensure_branch_protection — unit tests
# ---------------------------------------------------------------------------


class TestEnsureBranchProtection:
    def test_creates_ruleset_for_unprotected_branch(self):
        """A branch with no existing protection gets a ruleset created."""
        client = _make_client(existing_rulesets=[])
        ensure_branch_protection(client, ["main"])
        client.create_protection_ruleset.assert_called_once_with(
            name="mq-protect-main",
            branch="main",
        )

    def test_skips_already_protected_branch(self):
        """A branch already covered by an mq-protect-* ruleset is skipped."""
        existing = [_ruleset_fixture("mq-protect-main", "main")]
        client = _make_client(existing_rulesets=existing)
        ensure_branch_protection(client, ["main"])
        client.create_protection_ruleset.assert_not_called()

    def test_creates_only_missing_rulesets(self):
        """With two target branches, only the unprotected one gets a ruleset."""
        existing = [_ruleset_fixture("mq-protect-main", "main")]
        client = _make_client(existing_rulesets=existing)
        ensure_branch_protection(client, ["main", "release/1.0"])
        client.create_protection_ruleset.assert_called_once_with(
            name="mq-protect-release-1.0",
            branch="release/1.0",
        )

    def test_creates_rulesets_for_all_unprotected_branches(self):
        """All unprotected branches in a list each get a creation call."""
        client = _make_client(existing_rulesets=[])
        ensure_branch_protection(client, ["main", "develop"])
        assert client.create_protection_ruleset.call_count == 2
        client.create_protection_ruleset.assert_any_call(
            name="mq-protect-main", branch="main"
        )
        client.create_protection_ruleset.assert_any_call(
            name="mq-protect-develop", branch="develop"
        )

    def test_slash_in_branch_name_replaced_with_dash(self):
        """Branches like release/1.0 get ruleset name mq-protect-release-1.0."""
        client = _make_client(existing_rulesets=[])
        ensure_branch_protection(client, ["release/1.0"])
        client.create_protection_ruleset.assert_called_once_with(
            name="mq-protect-release-1.0",
            branch="release/1.0",
        )

    def test_non_mq_protect_rulesets_are_ignored(self):
        """Existing rulesets not named mq-protect-* do not count as protected."""
        existing = [
            {
                "name": "other-ruleset",
                "conditions": {
                    "ref_name": {
                        "include": ["refs/heads/main"],
                        "exclude": [],
                    }
                },
            }
        ]
        client = _make_client(existing_rulesets=existing)
        ensure_branch_protection(client, ["main"])
        client.create_protection_ruleset.assert_called_once()

    def test_creation_failure_is_swallowed(self):
        """If create_protection_ruleset raises, no exception propagates."""
        client = _make_client(existing_rulesets=[])
        client.create_protection_ruleset.side_effect = RuntimeError("no admin token")
        # Should not raise
        ensure_branch_protection(client, ["main"])

    def test_empty_target_branches_list(self):
        """Empty list produces no calls."""
        client = _make_client(existing_rulesets=[])
        ensure_branch_protection(client, [])
        client.create_protection_ruleset.assert_not_called()


# ---------------------------------------------------------------------------
# create_protection_ruleset — API payload tests
# ---------------------------------------------------------------------------


class TestCreateProtectionRulesetPayload:
    def test_api_call_structure(self):
        """Verify the JSON payload sent to the GitHub API."""
        import requests

        mock_resp = MagicMock(spec=requests.Response)
        mock_resp.status_code = 201
        mock_resp.headers = {}
        mock_resp.json.return_value = {"id": 999}

        from merge_queue.github_client import GitHubClient

        gh = GitHubClient("owner", "repo", token="tok", admin_token="admin-tok")
        gh._admin_session = MagicMock()
        gh._admin_session.post.return_value = mock_resp

        ruleset_id = gh.create_protection_ruleset(name="mq-protect-main", branch="main")

        assert ruleset_id == 999
        gh._admin_session.post.assert_called_once()
        _, kwargs = gh._admin_session.post.call_args
        payload = kwargs["json"]

        assert payload["name"] == "mq-protect-main"
        assert payload["target"] == "branch"
        assert payload["enforcement"] == "active"
        assert payload["conditions"]["ref_name"]["include"] == ["refs/heads/main"]
        assert payload["conditions"]["ref_name"]["exclude"] == []

        rule_types = {r["type"] for r in payload["rules"]}
        assert rule_types == {"pull_request", "required_status_checks"}

        status_rule = next(
            r for r in payload["rules"] if r["type"] == "required_status_checks"
        )
        checks = status_rule["parameters"]["required_status_checks"]
        assert any(c["context"] == "Final Results" for c in checks)

        bypass = payload["bypass_actors"]
        assert len(bypass) == 1
        assert bypass[0]["actor_type"] == "RepositoryRole"
        assert bypass[0]["bypass_mode"] == "always"

    def test_invalidates_ruleset_cache(self):
        """create_protection_ruleset clears the internal ruleset cache."""
        import requests

        mock_resp = MagicMock(spec=requests.Response)
        mock_resp.status_code = 201
        mock_resp.headers = {}
        mock_resp.json.return_value = {"id": 77}

        from merge_queue.github_client import GitHubClient

        gh = GitHubClient("owner", "repo", token="tok", admin_token="admin-tok")
        gh._admin_session = MagicMock()
        gh._admin_session.post.return_value = mock_resp
        gh._cache_rulesets = [{"name": "stale"}]

        gh.create_protection_ruleset("mq-protect-main", "main")
        assert gh._cache_rulesets is None


# ---------------------------------------------------------------------------
# do_enqueue — invalid target branch rejection
# ---------------------------------------------------------------------------


class TestDoEnqueueInvalidTarget:
    def test_rejects_pr_targeting_unconfigured_branch(self, mock_store):
        """PR targeting a branch not in target_branches is rejected."""
        config = "target_branches:\n  - main\n"
        client = _make_enqueue_client(base_ref="feature-branch", config_content=config)

        with patch("merge_queue.cli.QueueState") as qs:
            from tests.conftest import make_api_state

            qs.fetch.return_value = make_api_state()
            result = do_enqueue(client, 42)

        assert result == "invalid_target"

    def test_rejection_posts_comment(self, mock_store):
        """Rejection due to invalid target branch posts an informative comment."""
        config = "target_branches:\n  - main\n"
        client = _make_enqueue_client(base_ref="not-a-target", config_content=config)

        with patch("merge_queue.cli.QueueState") as qs:
            from tests.conftest import make_api_state

            qs.fetch.return_value = make_api_state()
            do_enqueue(client, 42)

        client.create_comment.assert_called_once()
        comment_body = client.create_comment.call_args[0][1]
        assert "not-a-target" in comment_body
        assert "main" in comment_body

    def test_rejection_removes_queue_label(self, mock_store):
        """Rejection removes the 'queue' label from the PR."""
        config = "target_branches:\n  - main\n"
        client = _make_enqueue_client(base_ref="not-a-target", config_content=config)

        with patch("merge_queue.cli.QueueState") as qs:
            from tests.conftest import make_api_state

            qs.fetch.return_value = make_api_state()
            do_enqueue(client, 42)

        client.remove_label.assert_called_with(42, "queue")

    def test_accepts_pr_targeting_configured_branch(self, mock_store):
        """PR targeting a configured branch is accepted (not rejected)."""
        config = "target_branches:\n  - main\n  - release/1.0\n"
        client = _make_enqueue_client(base_ref="release/1.0", config_content=config)

        with patch("merge_queue.cli.QueueState") as qs:
            from tests.conftest import make_api_state

            qs.fetch.return_value = make_api_state(mq_branches=["mq/active"])
            result = do_enqueue(client, 42)

        assert result == "queued_waiting"

    def test_no_config_accepts_default_branch(self, mock_store):
        """Without a config file, PRs targeting the default branch are accepted."""
        client = _make_enqueue_client(base_ref="main")  # no config_content

        with patch("merge_queue.cli.QueueState") as qs:
            from tests.conftest import make_api_state

            qs.fetch.return_value = make_api_state(mq_branches=["mq/active"])
            result = do_enqueue(client, 42)

        assert result == "queued_waiting"

    def test_no_config_rejects_non_default_branch(self, mock_store):
        """Without a config file, PRs targeting a non-default branch are rejected."""
        client = _make_enqueue_client(base_ref="some-random-branch")  # no config

        with patch("merge_queue.cli.QueueState") as qs:
            from tests.conftest import make_api_state

            qs.fetch.return_value = make_api_state()
            result = do_enqueue(client, 42)

        assert result == "invalid_target"


# ---------------------------------------------------------------------------
# do_process — calls ensure_branch_protection
# ---------------------------------------------------------------------------


class TestDoProcessEnsureProtection:
    def test_calls_ensure_branch_protection(self):
        """do_process calls ensure_branch_protection when there is work to process."""
        client = _make_process_client()

        with (
            patch("merge_queue.cli.StateStore") as store_cls,
            patch("merge_queue.cli.QueueState") as qs,
            patch("merge_queue.config.ensure_branch_protection") as mock_ensure,
        ):
            from tests.conftest import make_api_state, make_v2_state

            store = MagicMock()
            store.read.return_value = make_v2_state(
                branch="main", queue=[_queue_entry()]
            )
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
                mock_batch.batch_id = "ts1"
                mock_batch.branch = "mq/main/ts1"
                mock_batch.ruleset_id = 42
                mock_batch.stack.prs = []
                mock_batch_mod.create_batch.return_value = mock_batch
                mock_batch_mod.run_ci.return_value = MagicMock(passed=True, run_url="")
                mock_batch_mod.complete_batch.return_value = None

                do_process(client)

        mock_ensure.assert_called_once()
        _, args, _ = mock_ensure.mock_calls[0]
        # First positional arg is client, second is target_branches list
        assert "main" in args[1]

    def test_ensure_protection_not_called_when_queue_empty(self):
        """ensure_branch_protection is skipped when the queue is empty (early exit)."""
        client = _make_process_client()

        with (
            patch("merge_queue.cli.StateStore") as store_cls,
            patch("merge_queue.cli.QueueState") as qs,
            patch("merge_queue.config.ensure_branch_protection") as mock_ensure,
        ):
            from tests.conftest import make_api_state, make_v2_state

            store = MagicMock()
            store.read.return_value = make_v2_state()  # empty queue
            store_cls.return_value = store
            qs.fetch.return_value = make_api_state()

            result = do_process(client)

        assert result == "no_stacks"
        mock_ensure.assert_not_called()

    def test_ensure_protection_uses_configured_target_branches(self):
        """do_process passes target_branches from config to ensure_branch_protection."""
        config = "target_branches:\n  - main\n  - release/1.0\n"
        client = _make_process_client(config_content=config)
        captured: list[list[str]] = []

        def capture_ensure(c, branches):
            captured.append(list(branches))

        with (
            patch("merge_queue.cli.StateStore") as store_cls,
            patch("merge_queue.cli.QueueState") as qs,
            patch(
                "merge_queue.config.ensure_branch_protection",
                side_effect=capture_ensure,
            ),
        ):
            from tests.conftest import make_api_state, make_v2_state

            store = MagicMock()
            store.read.return_value = make_v2_state(
                branch="main", queue=[_queue_entry()]
            )
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
                mock_batch.batch_id = "ts2"
                mock_batch.branch = "mq/main/ts2"
                mock_batch.ruleset_id = 42
                mock_batch.stack.prs = []
                mock_batch_mod.create_batch.return_value = mock_batch
                mock_batch_mod.run_ci.return_value = MagicMock(passed=True, run_url="")
                mock_batch_mod.complete_batch.return_value = None

                do_process(client)

        assert captured, "ensure_branch_protection was not called"
        assert "main" in captured[0]
        assert "release/1.0" in captured[0]
