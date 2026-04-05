"""Tests for break-glass immediate merge — skips CI entirely."""

from __future__ import annotations

from unittest.mock import MagicMock, patch


from tests.conftest import (
    T0,
    make_pr_data,
    make_queue_entry,
    make_v2_state,
    now_iso,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_client(
    pr_number: int = 99,
    labels: list[str] | None = None,
    permission: str = "admin",
) -> MagicMock:
    """Return a mock client for break-glass tests."""
    client = MagicMock()
    client.owner = "testowner"
    client.repo = "testrepo"
    client.get_default_branch.return_value = "main"
    client.list_open_prs.return_value = []
    client.list_mq_branches.return_value = []
    client.list_rulesets.return_value = []
    client.get_pr_ci_status.return_value = (True, "")
    client.get_branch_sha.return_value = "abc123"
    client.compare_commits.return_value = "ahead"
    client.create_ruleset.return_value = 42
    client.get_ruleset.return_value = {
        "enforcement": "active",
        "conditions": {
            "ref_name": {"include": [f"refs/heads/break-glass-{pr_number}"]}
        },
    }
    client.poll_ci.return_value = True
    client.create_comment.return_value = 500
    client.create_deployment.return_value = 99
    client.get_user_permission.return_value = permission
    # get_file_content for config — no break_glass_users file
    client.get_file_content.side_effect = Exception("404 not found")
    client.get_pr.return_value = make_pr_data(
        pr_number,
        f"break-glass-{pr_number}",
        "main",
        labels=labels or ["break-glass"],
    )
    return client


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestBreakGlassSkipsCIAndMerges:
    """Break-glass PR with empty queue: batch created, CI NOT called, merged."""

    @patch("merge_queue.cli.batch_mod.complete_batch")
    @patch("merge_queue.cli.batch_mod.run_ci")
    @patch("merge_queue.cli.batch_mod.create_batch")
    @patch("merge_queue.cli.StateStore")
    @patch("merge_queue.cli._is_break_glass_authorized", return_value=True)
    @patch("merge_queue.config.get_target_branches", return_value=["main"])
    def test_break_glass_skips_ci_and_merges(
        self,
        _cfg,
        _auth,
        store_cls,
        mock_create_batch,
        mock_run_ci,
        mock_complete_batch,
        monkeypatch,
    ):
        from merge_queue.cli import do_break_glass
        from merge_queue.types import Batch, BatchStatus, PullRequest, Stack

        monkeypatch.setenv("MQ_SENDER", "admin")
        client = _mock_client(pr_number=99)

        state = make_v2_state(branch="main", queue=[])
        store = MagicMock()
        store.read.return_value = state
        store_cls.return_value = store

        # create_batch returns a Batch object
        pr_obj = PullRequest(
            number=99,
            head_sha="sha-99",
            head_ref="break-glass-99",
            base_ref="main",
            labels=("break-glass",),
        )
        batch = Batch(
            batch_id="123",
            branch="mq/main/123",
            stack=Stack(prs=(pr_obj,), queued_at=T0),
            status=BatchStatus.RUNNING,
            ruleset_id=42,
        )
        mock_create_batch.return_value = batch

        result = do_break_glass(client, 99)

        # Batch created
        mock_create_batch.assert_called_once()
        # CI NOT called
        mock_run_ci.assert_not_called()
        # complete_batch called
        mock_complete_batch.assert_called_once()
        # History should show "merged"
        written = store.write.call_args_list[-1][0][0]
        history = written.get("history", [])
        assert any(h["status"] == "merged" and 99 in h.get("prs", []) for h in history)
        # Active batch should be cleared
        assert written["branches"]["main"]["active_batch"] is None
        assert result == "merged"


class TestBreakGlassAbortsActiveBatch:
    """Active batch running CI. Break-glass aborts it and merges."""

    @patch("merge_queue.cli.batch_mod.complete_batch")
    @patch("merge_queue.cli.batch_mod.run_ci")
    @patch("merge_queue.cli.batch_mod.create_batch")
    @patch("merge_queue.cli.batch_mod.abort_batch")
    @patch("merge_queue.cli.StateStore")
    @patch("merge_queue.cli._is_break_glass_authorized", return_value=True)
    @patch("merge_queue.config.get_target_branches", return_value=["main"])
    def test_break_glass_aborts_active_batch(
        self,
        _cfg,
        _auth,
        store_cls,
        mock_abort,
        mock_create_batch,
        mock_run_ci,
        mock_complete_batch,
        monkeypatch,
    ):
        from merge_queue.cli import do_break_glass
        from merge_queue.types import Batch, BatchStatus, PullRequest, Stack

        monkeypatch.setenv("MQ_SENDER", "admin")
        client = _mock_client(pr_number=99)

        active_batch = {
            "batch_id": "batch-42",
            "branch": "mq/main/batch-42",
            "ruleset_id": 42,
            "started_at": now_iso(),
            "progress": "running_ci",
            "stack": [
                {
                    "number": 10,
                    "head_sha": "sha-10",
                    "head_ref": "feat-10",
                    "base_ref": "main",
                    "title": "Batch PR",
                }
            ],
        }
        state = make_v2_state(
            branch="main",
            queue=[make_queue_entry(20, head_ref="feat-20", position=1)],
            active_batch=active_batch,
        )
        store = MagicMock()
        store.read.return_value = state
        store_cls.return_value = store

        pr_obj = PullRequest(
            number=99,
            head_sha="sha-99",
            head_ref="break-glass-99",
            base_ref="main",
            labels=("break-glass",),
        )
        batch = Batch(
            batch_id="123",
            branch="mq/main/123",
            stack=Stack(prs=(pr_obj,), queued_at=T0),
            status=BatchStatus.RUNNING,
            ruleset_id=42,
        )
        mock_create_batch.return_value = batch

        result = do_break_glass(client, 99)

        # abort_batch called to clear active batch
        mock_abort.assert_called_once_with(client)
        # Active batch's PRs re-queued
        written = store.write.call_args_list[-1][0][0]
        queue = written["branches"]["main"]["queue"]
        queue_numbers = [e["stack"][0]["number"] for e in queue]
        assert 10 in queue_numbers  # batch PR re-queued
        assert 20 in queue_numbers  # original queue entry still there
        # CI NOT called
        mock_run_ci.assert_not_called()
        # Merged
        mock_complete_batch.assert_called_once()
        assert result == "merged"


class TestBreakGlassDuringCompletingPhase:
    """Active batch in 'completing' state. Break-glass still aborts and merges."""

    @patch("merge_queue.cli.batch_mod.complete_batch")
    @patch("merge_queue.cli.batch_mod.run_ci")
    @patch("merge_queue.cli.batch_mod.create_batch")
    @patch("merge_queue.cli.batch_mod.abort_batch")
    @patch("merge_queue.cli.StateStore")
    @patch("merge_queue.cli._is_break_glass_authorized", return_value=True)
    @patch("merge_queue.config.get_target_branches", return_value=["main"])
    def test_break_glass_during_completing_phase(
        self,
        _cfg,
        _auth,
        store_cls,
        mock_abort,
        mock_create_batch,
        mock_run_ci,
        mock_complete_batch,
        monkeypatch,
    ):
        from merge_queue.cli import do_break_glass
        from merge_queue.types import Batch, BatchStatus, PullRequest, Stack

        monkeypatch.setenv("MQ_SENDER", "admin")
        client = _mock_client(pr_number=99)

        active_batch = {
            "batch_id": "batch-55",
            "branch": "mq/main/batch-55",
            "ruleset_id": 55,
            "started_at": now_iso(),
            "progress": "completing",
            "stack": [
                {
                    "number": 15,
                    "head_sha": "sha-15",
                    "head_ref": "feat-15",
                    "base_ref": "main",
                    "title": "Completing PR",
                }
            ],
        }
        state = make_v2_state(
            branch="main",
            queue=[],
            active_batch=active_batch,
        )
        store = MagicMock()
        store.read.return_value = state
        store_cls.return_value = store

        pr_obj = PullRequest(
            number=99,
            head_sha="sha-99",
            head_ref="break-glass-99",
            base_ref="main",
            labels=("break-glass",),
        )
        batch = Batch(
            batch_id="123",
            branch="mq/main/123",
            stack=Stack(prs=(pr_obj,), queued_at=T0),
            status=BatchStatus.RUNNING,
            ruleset_id=42,
        )
        mock_create_batch.return_value = batch

        result = do_break_glass(client, 99)

        # abort called even during completing phase
        mock_abort.assert_called_once_with(client)
        # PRs from completing batch re-queued
        written = store.write.call_args_list[-1][0][0]
        queue = written["branches"]["main"]["queue"]
        queue_numbers = [e["stack"][0]["number"] for e in queue]
        assert 15 in queue_numbers
        # CI not called
        mock_run_ci.assert_not_called()
        assert result == "merged"


class TestBreakGlassUnauthorizedRejected:
    """Non-admin user tries break-glass — rejected with comment."""

    @patch("merge_queue.cli.batch_mod.complete_batch")
    @patch("merge_queue.cli.batch_mod.run_ci")
    @patch("merge_queue.cli.batch_mod.create_batch")
    @patch("merge_queue.cli.StateStore")
    @patch("merge_queue.cli._is_break_glass_authorized", return_value=False)
    @patch("merge_queue.config.get_target_branches", return_value=["main"])
    def test_break_glass_unauthorized_rejected(
        self,
        _cfg,
        _auth,
        store_cls,
        mock_create_batch,
        mock_run_ci,
        mock_complete_batch,
        monkeypatch,
    ):
        from merge_queue.cli import do_break_glass

        monkeypatch.setenv("MQ_SENDER", "random-user")
        client = _mock_client(pr_number=99, permission="write")

        state = make_v2_state(branch="main", queue=[])
        store = MagicMock()
        store.read.return_value = state
        store_cls.return_value = store

        result = do_break_glass(client, 99)

        # No batch created
        mock_create_batch.assert_not_called()
        # No CI
        mock_run_ci.assert_not_called()
        # No merge
        mock_complete_batch.assert_not_called()
        # Denial comment posted
        comment_bodies = [str(c) for c in client.create_comment.call_args_list]
        assert any("break-glass denied" in b for b in comment_bodies)
        # Label removed
        client.remove_label.assert_any_call(99, "break-glass")
        assert result == "denied"


class TestConcurrentBreakGlassSerialized:
    """Two break-glass PRs — first merges, second sees updated state."""

    @patch("merge_queue.cli.batch_mod.complete_batch")
    @patch("merge_queue.cli.batch_mod.run_ci")
    @patch("merge_queue.cli.batch_mod.create_batch")
    @patch("merge_queue.cli.StateStore")
    @patch("merge_queue.cli._is_break_glass_authorized", return_value=True)
    @patch("merge_queue.config.get_target_branches", return_value=["main"])
    def test_concurrent_break_glass_serialized(
        self,
        _cfg,
        _auth,
        store_cls,
        mock_create_batch,
        mock_run_ci,
        mock_complete_batch,
        monkeypatch,
    ):
        from merge_queue.cli import do_break_glass
        from merge_queue.types import Batch, BatchStatus, PullRequest, Stack

        monkeypatch.setenv("MQ_SENDER", "admin")

        # First break-glass PR
        client1 = _mock_client(pr_number=50)
        state1 = make_v2_state(branch="main", queue=[])
        store = MagicMock()
        store.read.return_value = state1
        store_cls.return_value = store

        pr1 = PullRequest(
            number=50,
            head_sha="sha-50",
            head_ref="break-glass-50",
            base_ref="main",
            labels=("break-glass",),
        )
        batch1 = Batch(
            batch_id="b1",
            branch="mq/main/b1",
            stack=Stack(prs=(pr1,), queued_at=T0),
            status=BatchStatus.RUNNING,
            ruleset_id=42,
        )
        mock_create_batch.return_value = batch1

        result1 = do_break_glass(client1, 50)
        assert result1 == "merged"

        # Capture state after first merge
        written_state = store.write.call_args_list[-1][0][0]
        assert any(
            h["status"] == "merged" and 50 in h.get("prs", [])
            for h in written_state.get("history", [])
        )

        # Second break-glass PR sees the updated state
        client2 = _mock_client(pr_number=51)
        store.read.return_value = written_state  # sees first's changes
        mock_create_batch.reset_mock()
        mock_complete_batch.reset_mock()

        pr2 = PullRequest(
            number=51,
            head_sha="sha-51",
            head_ref="break-glass-51",
            base_ref="main",
            labels=("break-glass",),
        )
        batch2 = Batch(
            batch_id="b2",
            branch="mq/main/b2",
            stack=Stack(prs=(pr2,), queued_at=T0),
            status=BatchStatus.RUNNING,
            ruleset_id=42,
        )
        mock_create_batch.return_value = batch2

        result2 = do_break_glass(client2, 51)
        assert result2 == "merged"

        # Both should be in history
        final_state = store.write.call_args_list[-1][0][0]
        merged_prs = [
            pr_num
            for h in final_state.get("history", [])
            if h["status"] == "merged"
            for pr_num in h.get("prs", [])
        ]
        assert 50 in merged_prs
        assert 51 in merged_prs
