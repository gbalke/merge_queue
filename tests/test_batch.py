"""Tests for batch.py — batch lifecycle with mocked client."""

from __future__ import annotations

import datetime
from unittest.mock import MagicMock, call, patch

import pytest

from merge_queue.batch import (
    BatchError,
    _git_create_and_merge,
    _unlock,
    abort_batch,
    complete_batch,
    create_batch,
    fail_batch,
    run_ci,
)
from merge_queue.types import Batch, BatchStatus, Stack

from tests.conftest import make_pr

T0 = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)


def _stack(*prs):
    return Stack(prs=tuple(prs), queued_at=T0)


def _batch(stack, **kwargs):
    defaults = dict(batch_id="123", branch="mq/123", stack=stack, status=BatchStatus.RUNNING, ruleset_id=42)
    defaults.update(kwargs)
    return Batch(**defaults)


class TestGitCreateAndMerge:
    def test_single_pr(self):
        pr = make_pr(1, "feat-a", head_sha="sha-1")
        stack = _stack(pr)
        calls = []

        def fake_git(*args):
            calls.append(args)
            if args[0] == "rev-parse":
                return "sha-1\n"
            return ""

        _git_create_and_merge("mq/123", stack, git=fake_git)

        assert ("checkout", "-b", "mq/123") in calls
        assert ("fetch", "origin", "feat-a") in calls
        assert ("rev-parse", "origin/feat-a") in calls
        assert any(a[0] == "merge" for a in calls)
        assert any(a[0] == "push" for a in calls)

    def test_multi_pr_stack(self):
        a = make_pr(1, "feat-a", head_sha="sha-1")
        b = make_pr(2, "feat-b", "feat-a", head_sha="sha-2")
        stack = _stack(a, b)
        calls = []

        def fake_git(*args):
            calls.append(args)
            if args == ("rev-parse", "origin/feat-a"):
                return "sha-1\n"
            if args == ("rev-parse", "origin/feat-b"):
                return "sha-2\n"
            return ""

        _git_create_and_merge("mq/123", stack, git=fake_git)

        merge_calls = [c for c in calls if c[0] == "merge"]
        assert len(merge_calls) == 2
        assert "feat-a" in merge_calls[0][2]
        assert "feat-b" in merge_calls[1][2]

    def test_sha_mismatch_raises(self):
        pr = make_pr(1, "feat-a", head_sha="sha-1")
        stack = _stack(pr)

        def fake_git(*args):
            if args[0] == "rev-parse":
                return "different-sha\n"
            return ""

        with pytest.raises(BatchError, match="head changed"):
            _git_create_and_merge("mq/123", stack, git=fake_git)


class TestCreateBatch:
    def test_creates_branch_locks_labels(self, mock_client):
        pr = make_pr(1, "feat-a", head_sha="sha-1")
        stack = _stack(pr)

        def fake_git(*args):
            if args[0] == "rev-parse":
                return "sha-1\n"
            return ""

        batch = create_batch(mock_client, stack, git=fake_git)

        assert batch.branch.startswith("mq/")
        assert batch.status == BatchStatus.RUNNING
        assert batch.ruleset_id == 42
        mock_client.create_ruleset.assert_called_once()
        mock_client.add_label.assert_called_once_with(1, "locked")

    def test_ruleset_failure_still_creates_batch(self, mock_client):
        pr = make_pr(1, "feat-a", head_sha="sha-1")
        stack = _stack(pr)
        mock_client.create_ruleset.side_effect = RuntimeError("no admin token")

        def fake_git(*args):
            if args[0] == "rev-parse":
                return "sha-1\n"
            return ""

        batch = create_batch(mock_client, stack, git=fake_git)

        assert batch.ruleset_id is None
        assert batch.status == BatchStatus.RUNNING
        mock_client.add_label.assert_called_once()


class TestUnlock:
    def test_deletes_ruleset(self, mock_client):
        batch = _batch(_stack(make_pr(1, "feat-a")))
        _unlock(mock_client, batch)
        mock_client.delete_ruleset.assert_called_once_with(42)

    def test_no_ruleset_noop(self, mock_client):
        batch = _batch(_stack(make_pr(1, "feat-a")), ruleset_id=None)
        _unlock(mock_client, batch)
        mock_client.delete_ruleset.assert_not_called()

    def test_delete_failure_logs_warning(self, mock_client):
        batch = _batch(_stack(make_pr(1, "feat-a")))
        mock_client.delete_ruleset.side_effect = RuntimeError("api error")
        # Should not raise
        _unlock(mock_client, batch)


class TestRunCi:
    def test_dispatches_and_polls(self, mock_client):
        stack = _stack(make_pr(1, "feat-a"))
        batch = _batch(stack)
        mock_client.poll_ci.return_value = True

        result = run_ci(mock_client, batch)

        assert result is True
        mock_client.dispatch_ci.assert_called_once_with("mq/123")
        mock_client.poll_ci.assert_called_once()

    def test_returns_false_on_failure(self, mock_client):
        stack = _stack(make_pr(1, "feat-a"))
        batch = _batch(stack)
        mock_client.poll_ci.return_value = False

        assert run_ci(mock_client, batch) is False


class TestCompleteBatch:
    def test_happy_path(self, mock_client):
        pr = make_pr(1, "feat-a", head_sha="sha-1")
        stack = _stack(pr)
        batch = _batch(stack)
        mock_client.get_pr.return_value = {"head": {"sha": "sha-1"}}

        complete_batch(mock_client, batch)

        assert batch.status == BatchStatus.PASSED
        mock_client.update_pr_base.assert_called_once_with(1, "main")
        mock_client.update_ref.assert_called_once_with("main", "abc123")
        mock_client.delete_ruleset.assert_called_once_with(42)
        mock_client.remove_label.assert_any_call(1, "locked")
        mock_client.remove_label.assert_any_call(1, "queue")
        mock_client.create_comment.assert_called_once()
        mock_client.delete_branch.assert_any_call("mq/123")
        mock_client.delete_branch.assert_any_call("feat-a")

    def test_sha_mismatch_raises(self, mock_client):
        pr = make_pr(1, "feat-a", head_sha="sha-1")
        stack = _stack(pr)
        batch = _batch(stack)
        mock_client.get_pr.return_value = {"head": {"sha": "different-sha"}}

        with pytest.raises(BatchError, match="head changed"):
            complete_batch(mock_client, batch)

    def test_main_diverged_raises(self, mock_client):
        pr = make_pr(1, "feat-a", head_sha="sha-1")
        stack = _stack(pr)
        batch = _batch(stack)
        mock_client.get_pr.return_value = {"head": {"sha": "sha-1"}}
        mock_client.compare_commits.return_value = "diverged"

        with pytest.raises(BatchError, match="diverged"):
            complete_batch(mock_client, batch)

    def test_multi_pr_stack(self, mock_client):
        a = make_pr(1, "feat-a", head_sha="sha-1")
        b = make_pr(2, "feat-b", "feat-a", head_sha="sha-2")
        stack = _stack(a, b)
        batch = _batch(stack)
        mock_client.get_pr.side_effect = [
            {"head": {"sha": "sha-1"}},
            {"head": {"sha": "sha-2"}},
        ]

        complete_batch(mock_client, batch)

        assert mock_client.update_pr_base.call_count == 2
        assert mock_client.delete_branch.call_count == 3  # mq/ + 2 PR branches

    def test_no_ruleset_skips_unlock(self, mock_client):
        pr = make_pr(1, "feat-a", head_sha="sha-1")
        stack = _stack(pr)
        batch = _batch(stack, ruleset_id=None)
        mock_client.get_pr.return_value = {"head": {"sha": "sha-1"}}

        complete_batch(mock_client, batch)

        mock_client.delete_ruleset.assert_not_called()

    def test_retarget_failure_continues(self, mock_client):
        """Retarget failure should not prevent the merge from completing."""
        pr = make_pr(1, "feat-a", head_sha="sha-1")
        stack = _stack(pr)
        batch = _batch(stack)
        mock_client.get_pr.return_value = {"head": {"sha": "sha-1"}}
        mock_client.update_pr_base.side_effect = RuntimeError("no new commits")

        complete_batch(mock_client, batch)

        assert batch.status == BatchStatus.PASSED
        mock_client.update_ref.assert_called_once()


class TestFailBatch:
    def test_cleans_up(self, mock_client):
        pr = make_pr(1, "feat-a")
        stack = _stack(pr)
        batch = _batch(stack)

        fail_batch(mock_client, batch, "CI failed")

        assert batch.status == BatchStatus.FAILED
        mock_client.delete_ruleset.assert_called_once_with(42)
        mock_client.remove_label.assert_any_call(1, "locked")
        mock_client.remove_label.assert_any_call(1, "queue")
        mock_client.create_comment.assert_called_once()
        assert "CI failed" in mock_client.create_comment.call_args[0][1]
        mock_client.delete_branch.assert_called_once_with("mq/123")


class TestAbortBatch:
    def test_cleans_everything(self, mock_client):
        mock_client.list_rulesets.return_value = [
            {"id": 42, "name": "mq-lock-123"},
            {"id": 99, "name": "other-ruleset"},
        ]
        mock_client.list_open_prs.return_value = [
            {"number": 1, "labels": [{"name": "locked"}, {"name": "queue"}]},
            {"number": 2, "labels": [{"name": "queue"}]},
        ]
        mock_client.list_mq_branches.return_value = ["mq/123"]

        abort_batch(mock_client)

        # Only deletes mq-lock rulesets
        mock_client.delete_ruleset.assert_called_once_with(42)
        # Only removes locked from PRs that have it
        mock_client.remove_label.assert_called_once_with(1, "locked")
        # Deletes mq branch
        mock_client.delete_branch.assert_called_once_with("mq/123")

    def test_no_active_batch(self, mock_client):
        """Abort when nothing is active should be a no-op."""
        abort_batch(mock_client)
        mock_client.delete_ruleset.assert_not_called()
        mock_client.delete_branch.assert_not_called()

    def test_ruleset_delete_failure_continues(self, mock_client):
        """Should continue cleanup even if ruleset deletion fails."""
        mock_client.list_rulesets.return_value = [
            {"id": 42, "name": "mq-lock-123"},
        ]
        mock_client.delete_ruleset.side_effect = RuntimeError("api error")
        mock_client.list_open_prs.return_value = []
        mock_client.list_mq_branches.return_value = ["mq/123"]

        abort_batch(mock_client)

        # Should still delete the branch despite ruleset failure
        mock_client.delete_branch.assert_called_once_with("mq/123")
