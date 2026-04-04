"""Tests for batch.py — batch lifecycle with mocked client."""

from __future__ import annotations

import datetime
from unittest.mock import MagicMock, call

import pytest

from merge_queue.batch import (
    BatchError,
    LockError,
    UnlockError,
    _git_create_and_merge,
    _lock_branches,
    _unlock,
    _unlock_ruleset,
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
    defaults = dict(
        batch_id="123", branch="mq/123", stack=stack,
        status=BatchStatus.RUNNING, ruleset_id=42,
    )
    defaults.update(kwargs)
    return Batch(**defaults)


def _good_ruleset(ruleset_id=42, patterns=None):
    return {
        "id": ruleset_id,
        "enforcement": "active",
        "conditions": {
            "ref_name": {
                "include": patterns or ["refs/heads/feat-a"],
                "exclude": [],
            }
        },
    }


# ── _lock_branches ──────────────────────────────────────────────


class TestLockBranches:
    def test_success_first_attempt(self, mock_client):
        mock_client.create_ruleset.return_value = 42
        mock_client.get_ruleset.return_value = _good_ruleset()

        result = _lock_branches(mock_client, "mq-lock-1", ["refs/heads/feat-a"])

        assert result == 42
        mock_client.create_ruleset.assert_called_once()
        mock_client.get_ruleset.assert_called_once_with(42)

    def test_retries_on_create_failure(self, mock_client):
        mock_client.create_ruleset.side_effect = [
            RuntimeError("network error"),
            42,
        ]
        mock_client.get_ruleset.return_value = _good_ruleset()

        result = _lock_branches(
            mock_client, "mq-lock-1", ["refs/heads/feat-a"],
            retry_delay=0,
        )

        assert result == 42
        assert mock_client.create_ruleset.call_count == 2

    def test_all_retries_exhausted_raises_lock_error(self, mock_client):
        mock_client.create_ruleset.side_effect = RuntimeError("always fails")

        with pytest.raises(LockError, match="Failed to lock.*3 attempts"):
            _lock_branches(
                mock_client, "mq-lock-1", ["refs/heads/feat-a"],
                retry_delay=0,
            )

        assert mock_client.create_ruleset.call_count == 3

    def test_verification_wrong_enforcement_raises(self, mock_client):
        mock_client.create_ruleset.return_value = 42
        mock_client.get_ruleset.return_value = {
            "id": 42,
            "enforcement": "disabled",
            "conditions": {"ref_name": {"include": ["refs/heads/feat-a"], "exclude": []}},
        }

        with pytest.raises(LockError, match="enforcement.*disabled"):
            _lock_branches(
                mock_client, "mq-lock-1", ["refs/heads/feat-a"],
                retry_delay=0,
            )

    def test_verification_missing_branches_raises(self, mock_client):
        mock_client.create_ruleset.return_value = 42
        mock_client.get_ruleset.return_value = {
            "id": 42,
            "enforcement": "active",
            "conditions": {"ref_name": {"include": [], "exclude": []}},
        }

        with pytest.raises(LockError, match="missing branch patterns"):
            _lock_branches(
                mock_client, "mq-lock-1", ["refs/heads/feat-a"],
                retry_delay=0,
            )

    def test_verification_not_retried(self, mock_client):
        """LockError from verification should not be retried (not transient)."""
        mock_client.create_ruleset.return_value = 42
        mock_client.get_ruleset.return_value = {
            "id": 42,
            "enforcement": "disabled",
            "conditions": {"ref_name": {"include": [], "exclude": []}},
        }

        with pytest.raises(LockError):
            _lock_branches(
                mock_client, "mq-lock-1", ["refs/heads/feat-a"],
                retry_delay=0,
            )

        # Only 1 attempt — verification failures are not retried
        assert mock_client.create_ruleset.call_count == 1

    def test_multi_branch_patterns(self, mock_client):
        patterns = ["refs/heads/feat-a", "refs/heads/feat-b"]
        mock_client.create_ruleset.return_value = 42
        mock_client.get_ruleset.return_value = _good_ruleset(patterns=patterns)

        result = _lock_branches(mock_client, "mq-lock-1", patterns)
        assert result == 42


# ── _unlock_ruleset ─────────────────────────────────────────────


class TestUnlockRuleset:
    def test_success_first_attempt(self, mock_client):
        mock_client.get_ruleset.side_effect = RuntimeError("404 Not Found")

        _unlock_ruleset(mock_client, 42, retry_delay=0)

        mock_client.delete_ruleset.assert_called_once_with(42)

    def test_none_ruleset_noop(self, mock_client):
        _unlock_ruleset(mock_client, None)
        mock_client.delete_ruleset.assert_not_called()

    def test_retries_on_delete_failure(self, mock_client):
        mock_client.delete_ruleset.side_effect = [
            RuntimeError("network error"),
            None,  # success
        ]
        mock_client.get_ruleset.side_effect = RuntimeError("404 Not Found")

        _unlock_ruleset(mock_client, 42, retry_delay=0)

        assert mock_client.delete_ruleset.call_count == 2

    def test_all_retries_exhausted_raises_unlock_error(self, mock_client):
        mock_client.delete_ruleset.side_effect = RuntimeError("always fails")

        with pytest.raises(UnlockError, match="Failed to unlock.*3 attempts"):
            _unlock_ruleset(mock_client, 42, retry_delay=0)

        assert mock_client.delete_ruleset.call_count == 3

    def test_verification_ruleset_still_exists_raises(self, mock_client):
        mock_client.get_ruleset.return_value = _good_ruleset()  # still there

        with pytest.raises(UnlockError, match="still exists"):
            _unlock_ruleset(mock_client, 42, retry_delay=0)

    def test_verification_404_means_success(self, mock_client):
        mock_client.get_ruleset.side_effect = RuntimeError("404 Not Found")

        _unlock_ruleset(mock_client, 42, retry_delay=0)  # should not raise

    def test_verification_other_error_treated_as_deleted(self, mock_client):
        """Non-404 verification errors are treated as 'probably deleted'."""
        mock_client.get_ruleset.side_effect = RuntimeError("connection timeout")

        _unlock_ruleset(mock_client, 42, retry_delay=0)  # should not raise


# ── _unlock (batch wrapper) ────────────────────────────────────


class TestUnlock:
    def test_delegates_to_unlock_ruleset(self, mock_client):
        batch = _batch(_stack(make_pr(1, "feat-a")))
        mock_client.get_ruleset.side_effect = RuntimeError("404")
        _unlock(mock_client, batch)
        mock_client.delete_ruleset.assert_called_once_with(42)

    def test_no_ruleset_noop(self, mock_client):
        batch = _batch(_stack(make_pr(1, "feat-a")), ruleset_id=None)
        _unlock(mock_client, batch)
        mock_client.delete_ruleset.assert_not_called()


# ── _git_create_and_merge ──────────────────────────────────────


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

    def test_sha_mismatch_raises(self):
        pr = make_pr(1, "feat-a", head_sha="sha-1")
        stack = _stack(pr)

        def fake_git(*args):
            if args[0] == "rev-parse":
                return "different-sha\n"
            return ""

        with pytest.raises(BatchError, match="head changed"):
            _git_create_and_merge("mq/123", stack, git=fake_git)


# ── create_batch ───────────────────────────────────────────────


class TestCreateBatch:
    def _fake_git(self, *args):
        if args[0] == "rev-parse":
            return "sha-1\n"
        return ""

    def test_locks_then_merges(self, mock_client):
        pr = make_pr(1, "feat-a", head_sha="sha-1")
        stack = _stack(pr)
        mock_client.get_ruleset.return_value = _good_ruleset()
        call_order = []
        mock_client.create_ruleset.side_effect = lambda *a, **k: (call_order.append("lock"), 42)[1]
        mock_client.add_label.side_effect = lambda *a, **k: call_order.append("label")

        def tracking_git(*args):
            call_order.append(f"git:{args[0]}")
            return self._fake_git(*args)

        batch = create_batch(mock_client, stack, git=tracking_git)

        lock_idx = call_order.index("lock")
        first_git = next(i for i, c in enumerate(call_order) if c.startswith("git:"))
        assert lock_idx < first_git
        assert batch.status == BatchStatus.RUNNING
        assert batch.ruleset_id == 42

    def test_lock_failure_raises_batch_error(self, mock_client):
        pr = make_pr(1, "feat-a", head_sha="sha-1")
        stack = _stack(pr)
        mock_client.create_ruleset.side_effect = RuntimeError("no token")

        with pytest.raises(BatchError, match="Could not lock"):
            create_batch(mock_client, stack, git=self._fake_git)

    def test_merge_failure_rolls_back(self, mock_client):
        pr = make_pr(1, "feat-a", head_sha="sha-1")
        stack = _stack(pr)
        mock_client.get_ruleset.return_value = _good_ruleset()
        # Unlock verification: 404 means deleted
        mock_client.get_ruleset.side_effect = [
            _good_ruleset(),  # verify after lock
            RuntimeError("404 Not Found"),  # verify after unlock
        ]

        def bad_git(*args):
            if args[0] == "rev-parse":
                return "wrong-sha\n"
            return ""

        with pytest.raises(BatchError, match="head changed"):
            create_batch(mock_client, stack, git=bad_git)

        mock_client.delete_ruleset.assert_called_once_with(42)
        mock_client.remove_label.assert_called_once_with(1, "locked")


# ── run_ci ─────────────────────────────────────────────────────


class TestRunCi:
    def test_dispatches_and_polls(self, mock_client):
        batch = _batch(_stack(make_pr(1, "feat-a")))
        mock_client.poll_ci.return_value = True
        assert run_ci(mock_client, batch) is True
        mock_client.dispatch_ci.assert_called_once_with("mq/123")

    def test_returns_false_on_failure(self, mock_client):
        batch = _batch(_stack(make_pr(1, "feat-a")))
        mock_client.poll_ci.return_value = False
        assert run_ci(mock_client, batch) is False


# ── complete_batch ─────────────────────────────────────────────


class TestCompleteBatch:
    def test_happy_path(self, mock_client):
        pr = make_pr(1, "feat-a", head_sha="sha-1")
        batch = _batch(_stack(pr))
        mock_client.get_pr.return_value = {"head": {"sha": "sha-1"}}
        mock_client.get_ruleset.side_effect = RuntimeError("404")

        complete_batch(mock_client, batch)

        assert batch.status == BatchStatus.PASSED
        mock_client.update_pr_base.assert_called_once_with(1, "main")
        mock_client.update_ref.assert_called_once_with("main", "abc123")
        mock_client.delete_ruleset.assert_called_once_with(42)

    def test_sha_mismatch_raises(self, mock_client):
        pr = make_pr(1, "feat-a", head_sha="sha-1")
        batch = _batch(_stack(pr))
        mock_client.get_pr.return_value = {"head": {"sha": "different"}}

        with pytest.raises(BatchError, match="head changed"):
            complete_batch(mock_client, batch)

    def test_main_diverged_raises(self, mock_client):
        pr = make_pr(1, "feat-a", head_sha="sha-1")
        batch = _batch(_stack(pr))
        mock_client.get_pr.return_value = {"head": {"sha": "sha-1"}}
        mock_client.compare_commits.return_value = "diverged"

        with pytest.raises(BatchError, match="diverged"):
            complete_batch(mock_client, batch)

    def test_multi_pr_stack(self, mock_client):
        a = make_pr(1, "feat-a", head_sha="sha-1")
        b = make_pr(2, "feat-b", "feat-a", head_sha="sha-2")
        batch = _batch(_stack(a, b))
        mock_client.get_pr.side_effect = [
            {"head": {"sha": "sha-1"}},
            {"head": {"sha": "sha-2"}},
        ]
        mock_client.get_ruleset.side_effect = RuntimeError("404")

        complete_batch(mock_client, batch)

        assert mock_client.update_pr_base.call_count == 2
        assert mock_client.delete_branch.call_count == 3

    def test_no_ruleset_skips_unlock(self, mock_client):
        pr = make_pr(1, "feat-a", head_sha="sha-1")
        batch = _batch(_stack(pr), ruleset_id=None)
        mock_client.get_pr.return_value = {"head": {"sha": "sha-1"}}

        complete_batch(mock_client, batch)

        mock_client.delete_ruleset.assert_not_called()

    def test_retarget_failure_continues(self, mock_client):
        pr = make_pr(1, "feat-a", head_sha="sha-1")
        batch = _batch(_stack(pr))
        mock_client.get_pr.return_value = {"head": {"sha": "sha-1"}}
        mock_client.update_pr_base.side_effect = RuntimeError("no new commits")
        mock_client.get_ruleset.side_effect = RuntimeError("404")

        complete_batch(mock_client, batch)
        assert batch.status == BatchStatus.PASSED

    def test_unlock_failure_logs_but_continues(self, mock_client):
        pr = make_pr(1, "feat-a", head_sha="sha-1")
        batch = _batch(_stack(pr))
        mock_client.get_pr.return_value = {"head": {"sha": "sha-1"}}
        # delete succeeds but verification says ruleset still exists
        mock_client.get_ruleset.return_value = _good_ruleset()

        # Should not raise — unlock failure is logged but doesn't block merge
        complete_batch(mock_client, batch)
        assert batch.status == BatchStatus.PASSED


# ── fail_batch ─────────────────────────────────────────────────


class TestFailBatch:
    def test_cleans_up(self, mock_client):
        pr = make_pr(1, "feat-a")
        batch = _batch(_stack(pr))
        mock_client.get_ruleset.side_effect = RuntimeError("404")

        fail_batch(mock_client, batch, "CI failed")

        assert batch.status == BatchStatus.FAILED
        mock_client.delete_ruleset.assert_called_once_with(42)
        mock_client.remove_label.assert_any_call(1, "locked")
        mock_client.remove_label.assert_any_call(1, "queue")
        assert "CI failed" in mock_client.create_comment.call_args[0][1]
        mock_client.delete_branch.assert_called_once_with("mq/123")

    def test_unlock_failure_continues(self, mock_client):
        pr = make_pr(1, "feat-a")
        batch = _batch(_stack(pr))
        mock_client.delete_ruleset.side_effect = RuntimeError("always fails")

        fail_batch(mock_client, batch, "CI failed")

        assert batch.status == BatchStatus.FAILED
        mock_client.delete_branch.assert_called_once()


# ── abort_batch ────────────────────────────────────────────────


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
        mock_client.get_ruleset.side_effect = RuntimeError("404")

        abort_batch(mock_client)

        mock_client.delete_ruleset.assert_called_once_with(42)
        mock_client.remove_label.assert_called_once_with(1, "locked")
        mock_client.delete_branch.assert_called_once_with("mq/123")

    def test_no_active_batch(self, mock_client):
        abort_batch(mock_client)
        mock_client.delete_ruleset.assert_not_called()

    def test_ruleset_delete_failure_continues(self, mock_client):
        mock_client.list_rulesets.return_value = [{"id": 42, "name": "mq-lock-123"}]
        mock_client.delete_ruleset.side_effect = RuntimeError("always fails")
        mock_client.list_open_prs.return_value = []
        mock_client.list_mq_branches.return_value = ["mq/123"]

        abort_batch(mock_client)

        mock_client.delete_branch.assert_called_once_with("mq/123")
