"""Tests for aborting merge when a PR loses its queue label during CI."""

from __future__ import annotations

import datetime
from unittest.mock import MagicMock, patch

from merge_queue.cli import do_process
from merge_queue.types import Stack
from tests.conftest import make_v2_state

T0 = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)


def _pr_data(number: int = 1, labels: list[str] | None = None) -> dict:
    """Return a minimal PR dict as returned by the GitHub API."""
    if labels is None:
        labels = ["queue"]
    return {
        "number": number,
        "head": {"ref": f"feat-{number}", "sha": f"sha-{number}"},
        "base": {"ref": "main"},
        "labels": [{"name": lbl} for lbl in labels],
        "title": "PR title",
    }


def _queue_entry(number: int = 1) -> dict:
    return {
        "position": 1,
        "queued_at": T0.isoformat(),
        "stack": [
            {
                "number": number,
                "head_sha": f"sha-{number}",
                "head_ref": f"feat-{number}",
                "base_ref": "main",
            }
        ],
        "deployment_id": 99,
        "comment_ids": {number: 1000 + number},
    }


class TestAbortOnDequeue:
    """Verify batch is aborted if a PR loses its queue label."""

    @patch("merge_queue.cli.QueueState")
    @patch("merge_queue.cli.batch_mod")
    def test_merge_aborted_if_label_removed_before_complete(
        self, batch_mod, QS, mock_client, mock_store
    ):
        """CI passes but PR lost queue label -> complete_batch NOT called."""
        from merge_queue.types import Batch

        mock_store.read.return_value = make_v2_state(queue=[_queue_entry()])
        mock_client.list_open_prs.return_value = [_pr_data(1)]
        QS.fetch.return_value = MagicMock(default_branch="main")

        batch = Batch("123", "mq/main/123", Stack(prs=(), queued_at=T0))
        batch_mod.create_batch.return_value = batch
        ci_result = MagicMock()
        ci_result.passed = True
        ci_result.run_url = "https://example.com/run/1"
        batch_mod.run_ci.return_value = ci_result
        batch_mod.BatchError = Exception

        # After CI passes, get_pr returns PR without queue label
        mock_client.get_pr.return_value = _pr_data(1, labels=["locked"])

        result = do_process(mock_client)

        # complete_batch should NOT be called — batch should be aborted
        batch_mod.complete_batch.assert_not_called()
        batch_mod.fail_batch.assert_called_once()
        assert result != "merged"

    @patch("merge_queue.cli.QueueState")
    @patch("merge_queue.cli.batch_mod")
    def test_merge_proceeds_if_label_still_present(
        self, batch_mod, QS, mock_client, mock_store
    ):
        """CI passes and PR still has queue label -> complete_batch IS called."""
        from merge_queue.types import Batch

        mock_store.read.return_value = make_v2_state(queue=[_queue_entry()])
        mock_client.list_open_prs.return_value = [_pr_data(1)]
        QS.fetch.return_value = MagicMock(default_branch="main")

        batch = Batch("123", "mq/main/123", Stack(prs=(), queued_at=T0))
        batch_mod.create_batch.return_value = batch
        ci_result = MagicMock()
        ci_result.passed = True
        ci_result.run_url = ""
        batch_mod.run_ci.return_value = ci_result
        batch_mod.BatchError = Exception

        # After CI passes, get_pr returns PR WITH queue label
        mock_client.get_pr.return_value = _pr_data(1, labels=["queue", "locked"])

        result = do_process(mock_client)

        batch_mod.complete_batch.assert_called_once()
        assert result == "merged"

    @patch("merge_queue.cli.QueueState")
    @patch("merge_queue.cli.batch_mod")
    def test_multi_pr_batch_aborted_if_any_loses_label(
        self, batch_mod, QS, mock_client, mock_store
    ):
        """In a multi-PR batch, if any PR loses queue label, abort the batch."""
        from merge_queue.types import Batch

        entry = {
            "position": 1,
            "queued_at": T0.isoformat(),
            "stack": [
                {
                    "number": 1,
                    "head_sha": "sha-1",
                    "head_ref": "feat-1",
                    "base_ref": "main",
                },
                {
                    "number": 2,
                    "head_sha": "sha-2",
                    "head_ref": "feat-2",
                    "base_ref": "main",
                },
            ],
            "deployment_id": 99,
            "comment_ids": {1: 1001, 2: 1002},
        }
        mock_store.read.return_value = make_v2_state(queue=[entry])
        mock_client.list_open_prs.return_value = [
            _pr_data(1),
            _pr_data(2),
        ]
        QS.fetch.return_value = MagicMock(default_branch="main")

        batch = Batch("123", "mq/main/123", Stack(prs=(), queued_at=T0))
        batch_mod.create_batch.return_value = batch
        ci_result = MagicMock()
        ci_result.passed = True
        ci_result.run_url = ""
        batch_mod.run_ci.return_value = ci_result
        batch_mod.BatchError = Exception

        # PR #1 still has label, PR #2 lost it
        def fake_get_pr(number):
            if number == 1:
                return _pr_data(1, labels=["queue", "locked"])
            return _pr_data(2, labels=["locked"])  # no queue label

        mock_client.get_pr.side_effect = fake_get_pr

        result = do_process(mock_client)

        batch_mod.complete_batch.assert_not_called()
        batch_mod.fail_batch.assert_called_once()
        assert result != "merged"
