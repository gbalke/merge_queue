"""Tests for the CI gate in do_enqueue and the do_retest function."""

from __future__ import annotations

import datetime
from unittest.mock import MagicMock, patch

import pytest

from merge_queue import comments
from merge_queue.cli import do_enqueue, do_retest
from merge_queue.types import empty_state


T0 = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)


def _make_client(ci_passed: bool = True, ci_url: str = "") -> MagicMock:
    client = MagicMock()
    client.owner = "owner"
    client.repo = "repo"
    client.get_default_branch.return_value = "main"
    client.list_mq_branches.return_value = []
    client.list_rulesets.return_value = []
    client.list_open_prs.return_value = []
    client.get_branch_sha.return_value = "abc123"
    client.compare_commits.return_value = "ahead"
    client.create_ruleset.return_value = 42
    client.poll_ci.return_value = True
    client.get_pr.return_value = {
        "number": 1,
        "state": "open",
        "title": "Test PR",
        "head": {"ref": "feature/test", "sha": "sha-1"},
        "base": {"ref": "main"},
        "labels": [{"name": "queue"}],
    }
    client.get_pr_ci_status.return_value = (ci_passed, ci_url)
    return client


@pytest.fixture
def mock_store_empty():
    with patch("merge_queue.cli.StateStore") as cls:
        store = MagicMock()
        store.read.return_value = empty_state()
        cls.return_value = store
        yield store


@pytest.fixture
def mock_queue_state():
    """Patch QueueState.fetch to return a minimal idle state."""
    from merge_queue.state import QueueState

    api_state = QueueState(
        default_branch="main",
        mq_branches=[],
        rulesets=[],
        prs=[],
        all_pr_data=[],
    )
    with patch("merge_queue.cli.QueueState") as cls:
        cls.fetch.return_value = api_state
        yield api_state


# --- CI gate in do_enqueue ---


class TestDoEnqueueCIGate:
    def test_rejects_pr_when_ci_not_passed(
        self, mock_store_empty, mock_queue_state
    ) -> None:
        """do_enqueue must return 'ci_not_ready' and comment when CI is not green."""
        client = _make_client(ci_passed=False)

        result = do_enqueue(client, 1)

        assert result == "ci_not_ready"
        # A comment should have been posted to the PR
        client.create_comment.assert_called_once()
        body = client.create_comment.call_args[0][1]
        assert "CI Required" in body
        assert "#1" in body
        assert "re-test" in body
        # The queue label should have been removed
        client.remove_label.assert_called_once_with(1, "queue")

    def test_accepts_pr_when_ci_passed(
        self, mock_store_empty, mock_queue_state
    ) -> None:
        """do_enqueue must proceed past the CI gate when CI is green."""
        client = _make_client(ci_passed=True)
        # Prevent the subsequent do_process from doing real work
        with patch("merge_queue.cli.do_process", return_value="processing"):
            result = do_enqueue(client, 1)

        assert result != "ci_not_ready"
        # CI gate should not have removed the queue label
        remove_calls = [
            c for c in client.remove_label.call_args_list if c[0] == (1, "queue")
        ]
        assert not remove_calls

    def test_ci_gate_checks_every_pr_in_stack(
        self, mock_store_empty, mock_queue_state
    ) -> None:
        """CI gate checks all PRs; failure on any one blocks the whole stack."""
        client = _make_client(ci_passed=True)
        # Stack of two PRs: second one has failing CI
        client.get_pr_ci_status.side_effect = [
            (True, ""),  # PR #1 passes
            (False, ""),  # PR #2 fails
        ]

        # Build a two-PR stack via mock_queue_state
        from merge_queue.state import QueueState
        from merge_queue.types import PullRequest, Stack

        pr1 = PullRequest(1, "sha-1", "feat-a", "main", ("queue",), T0)
        pr2 = PullRequest(2, "sha-2", "feat-b", "main", ("queue",), T0)
        stack = Stack(prs=(pr1, pr2), queued_at=T0)
        api_state = QueueState(
            default_branch="main",
            mq_branches=[],
            rulesets=[],
            prs=[pr1, pr2],
            all_pr_data=[],
        )

        def fake_get_pr(n: int) -> dict:
            return {
                "number": n,
                "state": "open",
                "title": f"PR {n}",
                "head": {"ref": f"feat-{n}", "sha": f"sha-{n}"},
                "base": {"ref": "main"},
                "labels": [{"name": "queue"}],
            }

        client.get_pr.side_effect = fake_get_pr

        with (
            patch("merge_queue.cli.QueueState") as qs_cls,
            patch("merge_queue.cli.detect_stacks", return_value=[stack]),
        ):
            qs_cls.fetch.return_value = api_state
            result = do_enqueue(client, 1)

        assert result == "ci_not_ready"


# --- do_retest ---


class TestDoRetest:
    def test_dispatches_ci_removes_label_and_comments(self) -> None:
        """do_retest must dispatch CI, remove the re-test label, and leave a comment."""
        client = _make_client()
        client.get_pr.return_value = {
            "number": 7,
            "state": "open",
            "title": "Test",
            "head": {"ref": "feature/xyz", "sha": "sha-7"},
            "base": {"ref": "main"},
            "labels": [{"name": "re-test"}],
        }

        result = do_retest(client, 7)

        assert result == "retriggered"
        client.dispatch_ci_on_ref.assert_called_once_with("feature/xyz")
        client.remove_label.assert_called_once_with(7, "re-test")
        client.create_comment.assert_called_once()
        body = client.create_comment.call_args[0][1]
        assert "retriggered" in body.lower()

    def test_uses_pr_head_ref_for_dispatch(self) -> None:
        """The CI dispatch uses the PR head ref, not its number or SHA."""
        client = _make_client()
        client.get_pr.return_value = {
            "number": 42,
            "state": "open",
            "title": "Another PR",
            "head": {"ref": "my-feature-branch", "sha": "deadbeef"},
            "base": {"ref": "main"},
            "labels": [{"name": "re-test"}],
        }

        do_retest(client, 42)

        client.dispatch_ci_on_ref.assert_called_once_with("my-feature-branch")


# --- Comment templates ---


class TestCICommentTemplates:
    @pytest.mark.parametrize(
        "owner,repo",
        [
            ("", ""),
            ("acme", "widget"),
        ],
    )
    def test_ci_not_ready_contains_pr_number(self, owner: str, repo: str) -> None:
        msg = comments.ci_not_ready(99, owner, repo)
        assert "99" in msg
        assert "CI Required" in msg
        assert "re-test" in msg

    def test_ci_not_ready_includes_mq_link_when_owner_repo_set(self) -> None:
        msg = comments.ci_not_ready(5, "octocat", "hello-world")
        assert "octocat/hello-world" in msg
        assert "actions" in msg

    def test_ci_not_ready_no_link_when_empty(self) -> None:
        msg = comments.ci_not_ready(5)
        assert "deployments" not in msg

    @pytest.mark.parametrize(
        "owner,repo",
        [
            ("", ""),
            ("acme", "widget"),
        ],
    )
    def test_ci_retriggered_mentions_label(self, owner: str, repo: str) -> None:
        msg = comments.ci_retriggered(owner, repo)
        assert "re-test" in msg

    def test_ci_retriggered_includes_mq_link(self) -> None:
        msg = comments.ci_retriggered("octocat", "hello-world")
        assert "octocat/hello-world" in msg
        assert "actions" in msg
