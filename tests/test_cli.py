"""Tests for cli.py — core logic with mocked store and client."""

from __future__ import annotations

import datetime
from unittest.mock import MagicMock, patch

import pytest

from merge_queue.cli import (
    _make_client,
    do_abort,
    do_check_rules,
    do_enqueue,
    do_process,
    do_status,
    main,
)
from merge_queue.state import QueueState
from merge_queue.types import Stack
from tests.conftest import make_v2_state

T0 = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
T1 = datetime.datetime(2026, 1, 1, 0, 1, tzinfo=datetime.timezone.utc)


def _now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _api_state(prs=None, mq_branches=None):
    return QueueState(
        default_branch="main",
        mq_branches=mq_branches or [],
        rulesets=[],
        prs=prs or [],
        all_pr_data=[],
    )


# --- _make_client ---


class TestMakeClient:
    def test_from_github_repository(self, monkeypatch):
        monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
        monkeypatch.setenv("GITHUB_TOKEN", "tok")
        with patch("merge_queue.cli.GitHubClient") as cls:
            _make_client()
            cls.assert_called_once_with("owner", "repo")

    def test_missing_env_exits(self, monkeypatch):
        monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
        monkeypatch.delenv("GITHUB_OWNER", raising=False)
        monkeypatch.delenv("GITHUB_REPO", raising=False)
        with pytest.raises(SystemExit):
            _make_client()


# --- do_process ---


class TestDoProcess:
    def _pr_data(self, number: int = 1) -> dict:
        """Return a minimal PR dict with the queue label, as returned by list_open_prs."""
        return {
            "number": number,
            "head": {"ref": f"feat-{number}", "sha": f"sha-{number}"},
            "base": {"ref": "main"},
            "labels": [{"name": "queue"}],
            "title": "PR title",
        }

    def _queue_entry(self, number: int = 1, deployment_id: int | None = 99) -> dict:
        return {
            "position": 1,
            "queued_at": T0.isoformat(),
            "stack": [
                {
                    "number": number,
                    "head_sha": f"sha-{number}",
                    "head_ref": "feat-a",
                    "base_ref": "main",
                }
            ],
            "deployment_id": deployment_id,
        }

    def test_batch_active_skips(self, mock_client, mock_store):
        mock_store.read.return_value = make_v2_state(
            active_batch={
                "batch_id": "123",
                "started_at": _now_iso(),
                "stack": [{"number": 1}],
            }
        )
        mock_client.get_pr.return_value = {"state": "open"}
        assert do_process(mock_client) == "batch_active"

    def test_stale_batch_auto_cleared(self, mock_client, mock_store):
        """If active batch PRs are all merged, clear it and continue."""
        mock_store.read.return_value = make_v2_state(
            active_batch={
                "batch_id": "123",
                "started_at": _now_iso(),
                "stack": [{"number": 1}],
            }
        )
        mock_client.get_pr.return_value = {"state": "closed"}
        assert do_process(mock_client) == "no_stacks"

    def test_empty_queue(self, mock_client, mock_store):
        assert do_process(mock_client) == "no_stacks"

    @patch("merge_queue.cli.QueueState")
    @patch("merge_queue.cli.batch_mod")
    def test_processes_first_in_queue(self, batch_mod, QS, mock_client, mock_store):
        from merge_queue.types import Batch

        mock_store.read.return_value = make_v2_state(queue=[self._queue_entry()])
        mock_client.list_open_prs.return_value = [self._pr_data(1)]
        QS.fetch.return_value = _api_state()
        batch = Batch("123", "mq/main/123", Stack(prs=(), queued_at=T0))
        batch_mod.create_batch.return_value = batch
        ci_result = MagicMock()
        ci_result.passed = True
        ci_result.run_url = ""
        batch_mod.run_ci.return_value = ci_result
        batch_mod.BatchError = Exception

        # get_pr must return queue label for the dequeue check
        mock_client.get_pr.return_value = self._pr_data(1)

        result = do_process(mock_client)

        assert result == "merged"
        batch_mod.create_batch.assert_called_once()
        batch_mod.complete_batch.assert_called_once()
        assert mock_store.write.call_count >= 3
        mock_client.update_deployment_status.assert_any_call(
            99, "in_progress", "Locking branches..."
        )

    @patch("merge_queue.cli.QueueState")
    @patch("merge_queue.cli.batch_mod")
    def test_ci_failure(self, batch_mod, QS, mock_client, mock_store):
        from merge_queue.types import Batch

        mock_store.read.return_value = make_v2_state(
            queue=[self._queue_entry(deployment_id=None)]
        )
        mock_client.list_open_prs.return_value = [self._pr_data(1)]
        QS.fetch.return_value = _api_state()
        batch = Batch("123", "mq/main/123", Stack(prs=(), queued_at=T0))
        batch_mod.create_batch.return_value = batch
        ci_result = MagicMock()
        ci_result.passed = False
        ci_result.run_url = "https://example.com/run/fail"
        batch_mod.run_ci.return_value = ci_result
        batch_mod.BatchError = Exception

        assert do_process(mock_client) == "ci_failed"
        batch_mod.fail_batch.assert_called_once()
        comment_calls = [c[0][1] for c in mock_client.create_comment.call_args_list]
        assert any("https://example.com/run/fail" in c for c in comment_calls)

    @patch("merge_queue.cli.QueueState")
    @patch("merge_queue.cli.batch_mod")
    def test_batch_error(self, batch_mod, QS, mock_client, mock_store):
        mock_store.read.return_value = make_v2_state(
            queue=[self._queue_entry(deployment_id=88)]
        )
        mock_client.list_open_prs.return_value = [self._pr_data(1)]
        QS.fetch.return_value = _api_state()
        batch_mod.create_batch.side_effect = Exception("merge conflict")
        batch_mod.BatchError = Exception

        assert do_process(mock_client) == "batch_error"
        mock_client.update_deployment_status.assert_any_call(
            88, "failure", "merge conflict"
        )

    @patch("merge_queue.cli.QueueState")
    @patch("merge_queue.cli.batch_mod")
    def test_moves_to_history(self, batch_mod, QS, mock_client, mock_store):
        from merge_queue.types import Batch

        mock_store.read.return_value = make_v2_state(
            queue=[self._queue_entry(deployment_id=None)]
        )
        mock_client.list_open_prs.return_value = [self._pr_data(1)]
        QS.fetch.return_value = _api_state()
        batch = Batch("123", "mq/main/123", Stack(prs=(), queued_at=T0))
        batch_mod.create_batch.return_value = batch
        ci_result = MagicMock()
        ci_result.passed = True
        ci_result.run_url = ""
        batch_mod.run_ci.return_value = ci_result
        batch_mod.BatchError = Exception

        # get_pr must return queue label for the dequeue check
        mock_client.get_pr.return_value = self._pr_data(1)

        do_process(mock_client)

        final_state = mock_store.write.call_args_list[-1][0][0]
        # In v2, active_batch is under branches["main"]
        assert final_state["branches"]["main"]["active_batch"] is None
        assert len(final_state["history"]) == 1
        assert final_state["history"][0]["status"] == "merged"


# --- do_enqueue ---


class TestDoEnqueue:
    @patch("merge_queue.cli.do_process", return_value="merged")
    @patch("merge_queue.cli.QueueState")
    def test_adds_to_queue(self, QS, do_proc, mock_client, mock_store):
        QS.fetch.return_value = _api_state()
        mock_client.get_pr.return_value = {
            "state": "open",
            "head": {"sha": "sha-1", "ref": "feat-a"},
            "base": {"ref": "main"},
            "title": "Add feature A",
        }
        mock_client.create_deployment.return_value = 42
        mock_client.create_comment.return_value = 100

        do_enqueue(mock_client, 1)

        written = mock_store.write.call_args_list[0][0][0]
        branch_queue = written["branches"]["main"]["queue"]
        assert len(branch_queue) == 1
        assert branch_queue[0]["position"] == 1
        mock_client.create_deployment.assert_called_once()
        mock_client.create_comment.assert_called()
        do_proc.assert_called_once_with(mock_client)

    @patch("merge_queue.cli.QueueState")
    def test_already_queued(self, QS, mock_client, mock_store):
        mock_client.get_pr.return_value = {"state": "open"}
        mock_store.read.return_value = make_v2_state(
            queue=[
                {
                    "position": 1,
                    "queued_at": T0.isoformat(),
                    "stack": [
                        {
                            "number": 1,
                            "head_sha": "sha-1",
                            "head_ref": "feat-a",
                            "base_ref": "main",
                        }
                    ],
                }
            ]
        )

        assert do_enqueue(mock_client, 1) == "already_queued"

    def test_skips_merged_pr(self, mock_client, mock_store):
        mock_client.get_pr.return_value = {"state": "closed", "merged_at": "2026-01-01"}
        assert do_enqueue(mock_client, 1) == "pr_not_open"


# --- do_abort ---


class TestDoAbort:
    def test_abort_active_batch(self, mock_client, mock_store):
        mock_store.read.return_value = make_v2_state(
            active_batch={
                "batch_id": "123",
                "stack": [{"number": 1}],
                "deployment_id": 42,
            }
        )

        with patch("merge_queue.cli.batch_mod"):
            result = do_abort(mock_client, 1)

        assert result == "aborted"
        mock_client.update_deployment_status.assert_any_call(42, "inactive", "Aborted")
        final = mock_store.write.call_args[0][0]
        assert final["branches"]["main"]["active_batch"] is None

    def test_remove_from_queue(self, mock_client, mock_store):
        mock_store.read.return_value = make_v2_state(
            queue=[
                {"position": 1, "stack": [{"number": 1}], "deployment_id": 10},
                {"position": 2, "stack": [{"number": 2}], "deployment_id": 20},
            ]
        )

        result = do_abort(mock_client, 1)

        assert result == "removed"
        final = mock_store.write.call_args[0][0]
        remaining = final["branches"]["main"]["queue"]
        assert len(remaining) == 1
        assert remaining[0]["position"] == 1
        assert remaining[0]["stack"][0]["number"] == 2
        mock_client.update_deployment_status.assert_any_call(10, "inactive", "Removed")

    def test_not_found(self, mock_client, mock_store):
        assert do_abort(mock_client, 99) == "not_found"


# --- do_status ---


class TestDoStatus:
    def test_prints_status(self, mock_client, mock_store):
        output = do_status(mock_client)
        # v2 empty state has no branches so falls back to legacy "ACTIVE: none"
        assert "ACTIVE" in output


# --- do_check_rules ---


class TestDoCheckRules:
    @patch("merge_queue.cli.QueueState")
    def test_returns_results(self, QS, mock_client):
        QS.fetch.return_value = _api_state()
        results = do_check_rules(mock_client)
        assert len(results) == 5
        assert all(r.passed for r in results)


# --- main ---


class TestMain:
    def test_parses_status(self, monkeypatch):
        monkeypatch.setenv("GITHUB_REPOSITORY", "test/repo")
        monkeypatch.setenv("GITHUB_TOKEN", "tok")
        with (
            patch("merge_queue.cli._make_client"),
            patch("merge_queue.cli.do_status", return_value="ACTIVE: none"),
        ):
            monkeypatch.setattr("sys.argv", ["merge-queue", "status"])
            main()

    def test_parses_enqueue(self, monkeypatch):
        monkeypatch.setenv("GITHUB_REPOSITORY", "test/repo")
        monkeypatch.setenv("GITHUB_TOKEN", "tok")
        with (
            patch("merge_queue.cli._make_client"),
            patch("merge_queue.cli.do_enqueue") as de,
        ):
            monkeypatch.setattr("sys.argv", ["merge-queue", "enqueue", "42"])
            main()
            assert de.call_args[0][1] == 42
