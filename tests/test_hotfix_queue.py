"""Tests for hotfix queue priority — hotfixes jump to front of queue."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from merge_queue.cli import do_hotfix
from tests.conftest import (
    make_pr_data,
    make_queue_entry,
    make_v2_state,
    now_iso,
)


def _mock_client(pr_number: int = 99, labels: list[str] | None = None) -> MagicMock:
    """Return a mock client pre-configured for hotfix tests."""
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
    client.poll_ci.return_value = True
    client.get_pr.return_value = make_pr_data(
        pr_number, f"hotfix-{pr_number}", "main", labels=labels or ["hotfix"]
    )
    return client


class TestHotfixInsertsAtFrontOfQueue:
    """Hotfix PR should be inserted at position 0, shifting existing entries."""

    @patch("merge_queue.cli.do_process")
    @patch("merge_queue.cli.StateStore")
    @patch("merge_queue.cli._is_break_glass_authorized", return_value=True)
    @patch("merge_queue.config.get_target_branches", return_value=["main"])
    def test_hotfix_inserts_at_front_of_queue(
        self, _cfg, _auth, store_cls, mock_do_process, monkeypatch
    ):
        monkeypatch.setenv("MQ_SENDER", "admin")
        client = _mock_client(pr_number=99)
        mock_do_process.return_value = "processed"

        # Pre-existing queue with 2 entries
        state = make_v2_state(
            branch="main",
            queue=[
                make_queue_entry(1, head_ref="feat-a", position=1),
                make_queue_entry(2, head_ref="feat-b", position=2),
            ],
        )
        store = MagicMock()
        store.read.return_value = state
        store_cls.return_value = store

        do_hotfix(client, 99)

        # The state should have been written with hotfix at position 1, others shifted
        written_state = store.write.call_args[0][0]
        queue = written_state["branches"]["main"]["queue"]

        assert len(queue) == 3
        # Hotfix is at front
        assert queue[0]["stack"][0]["number"] == 99
        assert queue[0]["position"] == 1
        # Original entries shifted
        assert queue[1]["stack"][0]["number"] == 1
        assert queue[1]["position"] == 2
        assert queue[2]["stack"][0]["number"] == 2
        assert queue[2]["position"] == 3

        # do_process should be called
        mock_do_process.assert_called_once_with(client)


class TestHotfixAbortsBatchAndRequeues:
    """If there's an active batch, hotfix should abort it and requeue its PRs."""

    @patch("merge_queue.cli.do_process")
    @patch("merge_queue.cli.batch_mod.abort_batch")
    @patch("merge_queue.cli.StateStore")
    @patch("merge_queue.cli._is_break_glass_authorized", return_value=True)
    @patch("merge_queue.config.get_target_branches", return_value=["main"])
    def test_hotfix_aborts_active_batch_and_requeues(
        self, _cfg, _auth, store_cls, mock_abort, mock_do_process, monkeypatch
    ):
        monkeypatch.setenv("MQ_SENDER", "admin")
        client = _mock_client(pr_number=99)
        mock_do_process.return_value = "processed"

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

        do_hotfix(client, 99)

        # abort_batch should have been called
        mock_abort.assert_called_once_with(client)

        # The state should have been written
        written_state = store.write.call_args[0][0]
        branch_state = written_state["branches"]["main"]

        # active_batch should be cleared
        assert branch_state["active_batch"] is None

        # Queue: hotfix first, then batch PRs re-queued, then original queue
        queue = branch_state["queue"]
        numbers = [entry["stack"][0]["number"] for entry in queue]
        assert numbers[0] == 99  # hotfix at front
        assert 10 in numbers  # batch PR re-queued
        assert 20 in numbers  # original queue entry still there
        # Hotfix before batch PR
        assert numbers.index(99) < numbers.index(10)

        # Positions should be renumbered
        for i, entry in enumerate(queue):
            assert entry["position"] == i + 1

        mock_do_process.assert_called_once_with(client)


class TestHotfixWithEmptyQueue:
    """Hotfix on empty queue should be queued at position 0 and processed."""

    @patch("merge_queue.cli.do_process")
    @patch("merge_queue.cli.StateStore")
    @patch("merge_queue.cli._is_break_glass_authorized", return_value=True)
    @patch("merge_queue.config.get_target_branches", return_value=["main"])
    def test_hotfix_with_empty_queue(
        self, _cfg, _auth, store_cls, mock_do_process, monkeypatch
    ):
        monkeypatch.setenv("MQ_SENDER", "admin")
        client = _mock_client(pr_number=99)
        mock_do_process.return_value = "processed"

        state = make_v2_state(branch="main", queue=[])
        store = MagicMock()
        store.read.return_value = state
        store_cls.return_value = store

        do_hotfix(client, 99)

        written_state = store.write.call_args[0][0]
        queue = written_state["branches"]["main"]["queue"]

        assert len(queue) == 1
        assert queue[0]["stack"][0]["number"] == 99
        assert queue[0]["position"] == 1

        mock_do_process.assert_called_once_with(client)
