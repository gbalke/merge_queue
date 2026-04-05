"""Tests for stuck 'completing' batch recovery.

Covers the scenario where a GitHub Actions run sets progress='completing'
on the active batch but is cancelled before finishing the merge. The next
do_process call must detect this and either resume or clear the batch
instead of leaving it stuck.
"""

from __future__ import annotations

import datetime
from unittest.mock import patch

from merge_queue.cli import do_process
from tests.conftest import make_v2_state


def _minutes_ago(minutes: int) -> str:
    """Return ISO timestamp for N minutes ago."""
    t = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
        minutes=minutes
    )
    return t.isoformat()


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _active_batch(*, progress: str = "completing", age_minutes: int = 5) -> dict:
    """Build an active_batch dict with given progress and age."""
    return {
        "batch_id": "123",
        "branch": "mq/main/123",
        "started_at": _minutes_ago(age_minutes),
        "progress": progress,
        "target_branch": "main",
        "stack": [
            {
                "number": 1,
                "head_sha": "sha-1",
                "head_ref": "feat-a",
                "base_ref": "main",
            }
        ],
        "comment_ids": {1: 1001},
        "deployment_id": 99,
        "ruleset_id": 42,
    }


class TestStuckCompleting:
    """Tests for batches stuck in progress='completing' state."""

    def test_completing_batch_is_resumed_and_cleared(self, mock_client, mock_store):
        """A batch stuck in 'completing' should be resumed and cleared.

        This is the core bug scenario: CI passed, progress was set to
        'completing', then the GHA run was cancelled. The next do_process
        must not leave the batch stuck --- it should resume completion
        and clear the active_batch.
        """
        mock_store.read.return_value = make_v2_state(
            active_batch=_active_batch(progress="completing", age_minutes=5),
        )
        # PR is still open (not yet merged by the cancelled run)
        mock_client.get_pr.return_value = {"state": "open"}
        # complete_batch needs these
        mock_client.get_branch_sha.return_value = "batch-sha"
        mock_client.compare_commits.return_value = "ahead"

        with patch("merge_queue.cli.batch_mod") as batch_mod:
            batch_mod.BatchError = Exception
            result = do_process(mock_client)

        # The active batch must be cleared (not left stuck returning batch_active)
        assert result != "batch_active", (
            "Batch was left stuck in 'completing' state instead of being cleared"
        )
        final_state = mock_store.write.call_args_list[-1][0][0]
        assert final_state["branches"]["main"]["active_batch"] is None

    def test_completing_batch_under_2min_also_resumes(self, mock_client, mock_store):
        """A batch in 'completing' even < 2 min old should be resumed immediately."""
        mock_store.read.return_value = make_v2_state(
            active_batch=_active_batch(progress="completing", age_minutes=1),
        )
        mock_client.get_pr.return_value = {"state": "open"}
        mock_client.get_branch_sha.return_value = "batch-sha"
        mock_client.compare_commits.return_value = "ahead"

        with patch("merge_queue.cli.batch_mod") as batch_mod:
            batch_mod.BatchError = Exception
            result = do_process(mock_client)

        assert result != "batch_active"
        final_state = mock_store.write.call_args_list[-1][0][0]
        assert final_state["branches"]["main"]["active_batch"] is None

    def test_completing_batch_prs_all_merged_cleared_immediately(
        self, mock_client, mock_store
    ):
        """If PRs are already merged/closed, batch is cleared before age check."""
        mock_store.read.return_value = make_v2_state(
            active_batch=_active_batch(progress="completing", age_minutes=1),
        )
        # PR was successfully merged by the original run
        mock_client.get_pr.return_value = {"state": "closed"}

        result = do_process(mock_client)

        # All-merged check fires first, clears batch
        final_state = mock_store.write.call_args_list[-1][0][0]
        assert final_state["branches"]["main"]["active_batch"] is None
        assert result == "no_stacks"

    def test_running_ci_batch_under_30min_still_skips(self, mock_client, mock_store):
        """A normal 'running_ci' batch under 30 min should be skipped (existing behavior)."""
        mock_store.read.return_value = make_v2_state(
            active_batch=_active_batch(progress="running_ci", age_minutes=10),
        )
        mock_client.get_pr.return_value = {"state": "open"}

        result = do_process(mock_client)

        assert result == "batch_active"

    def test_completing_resume_failure_still_clears_batch(
        self, mock_client, mock_store
    ):
        """Even if resume/complete_batch fails, the batch should be cleared."""
        mock_store.read.return_value = make_v2_state(
            active_batch=_active_batch(progress="completing", age_minutes=5),
        )
        mock_client.get_pr.return_value = {"state": "open"}

        with patch("merge_queue.cli.batch_mod") as batch_mod:
            batch_mod.BatchError = Exception
            # The resume attempt fails (e.g. batch branch was deleted)
            batch_mod.complete_batch.side_effect = Exception("branch deleted")
            batch_mod.fail_batch.return_value = None
            result = do_process(mock_client)

        # Batch must still be cleared even on failure
        assert result != "batch_active"
        final_state = mock_store.write.call_args_list[-1][0][0]
        assert final_state["branches"]["main"]["active_batch"] is None

    def test_stale_30min_batch_cleared_regardless_of_progress(
        self, mock_client, mock_store
    ):
        """A batch older than 30 min is always cleared, regardless of progress."""
        mock_store.read.return_value = make_v2_state(
            active_batch=_active_batch(progress="running_ci", age_minutes=35),
        )
        mock_client.get_pr.return_value = {"state": "open"}

        with patch("merge_queue.cli.batch_mod") as batch_mod:
            batch_mod.BatchError = Exception
            result = do_process(mock_client)

        # 30-min stale timeout should clear it
        final_state = mock_store.write.call_args_list[-1][0][0]
        assert final_state["branches"]["main"]["active_batch"] is None
        assert result == "no_stacks"
