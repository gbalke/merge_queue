"""Tests for cli.py — core logic functions and command routing."""

from __future__ import annotations

import datetime
from unittest.mock import MagicMock, call, patch

import pytest

from merge_queue.cli import (
    _dispatch_next_if_queued,
    _make_client,
    do_abort,
    do_check_rules,
    do_enqueue,
    do_process,
    fetch_queued_prs,
    cmd_enqueue,
    cmd_process,
    cmd_abort,
    cmd_check_rules,
    main,
)
from merge_queue.types import Batch, BatchStatus, Stack

from tests.conftest import make_pr, make_pr_data

T0 = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
T1 = datetime.datetime(2026, 1, 1, 0, 1, tzinfo=datetime.timezone.utc)


# --- _make_client ---


class TestMakeClient:
    def test_from_github_repository(self, monkeypatch):
        monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
        monkeypatch.setenv("GITHUB_TOKEN", "tok")
        with patch("merge_queue.cli.GitHubClient") as cls:
            _make_client()
            cls.assert_called_once_with("owner", "repo")

    def test_from_separate_env_vars(self, monkeypatch):
        monkeypatch.setenv("GITHUB_OWNER", "myowner")
        monkeypatch.setenv("GITHUB_REPO", "myrepo")
        monkeypatch.setenv("GITHUB_TOKEN", "tok")
        monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
        with patch("merge_queue.cli.GitHubClient") as cls:
            _make_client()
            cls.assert_called_once_with("myowner", "myrepo")

    def test_missing_env_exits(self, monkeypatch):
        monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
        monkeypatch.delenv("GITHUB_OWNER", raising=False)
        monkeypatch.delenv("GITHUB_REPO", raising=False)
        with pytest.raises(SystemExit):
            _make_client()


# --- fetch_queued_prs ---


class TestFetchQueuedPrs:
    def test_filters_to_queued_and_locked(self, mock_client):
        mock_client.list_open_prs.return_value = [
            make_pr_data(1, "feat-a", labels=["queue"]),
            make_pr_data(2, "feat-b", labels=["locked"]),
            make_pr_data(3, "fix-c", labels=[]),  # excluded
        ]
        mock_client.get_label_timestamp.return_value = T0

        prs = fetch_queued_prs(mock_client)
        assert len(prs) == 2
        assert prs[0].number == 1
        assert prs[1].number == 2

    def test_skips_unlabeled_prs(self, mock_client):
        mock_client.list_open_prs.return_value = [
            make_pr_data(1, "feat-a", labels=[]),
        ]
        assert fetch_queued_prs(mock_client) == []

    def test_uses_timestamp_from_api(self, mock_client):
        mock_client.list_open_prs.return_value = [
            make_pr_data(1, "feat-a", labels=["queue"]),
        ]
        mock_client.get_label_timestamp.return_value = T0

        prs = fetch_queued_prs(mock_client)
        assert prs[0].queued_at == T0

    def test_fallback_timestamp_when_none(self, mock_client):
        mock_client.list_open_prs.return_value = [
            make_pr_data(1, "feat-a", labels=["queue"]),
        ]
        mock_client.get_label_timestamp.return_value = None

        prs = fetch_queued_prs(mock_client)
        assert prs[0].queued_at is not None  # falls back to now()


# --- do_process ---


class TestDoProcess:
    def _setup_queued_stack(self, mock_client):
        """Set up a mock client with one queued stack ready to process."""
        mock_client.list_open_prs.return_value = [
            make_pr_data(1, "feat-a", "main", labels=["queue"], head_sha="sha-1"),
        ]
        mock_client.get_label_timestamp.return_value = T0

    def test_batch_active_skips(self, mock_client):
        mock_client.list_mq_branches.return_value = ["mq/123"]
        assert do_process(mock_client) == "batch_active"

    def test_no_stacks_returns(self, mock_client):
        assert do_process(mock_client) == "no_stacks"

    @patch("merge_queue.cli.batch_mod")
    def test_happy_path_merged(self, batch_mod, mock_client):
        self._setup_queued_stack(mock_client)
        batch = Batch("123", "mq/123", Stack(prs=(), queued_at=T0))
        batch_mod.create_batch.return_value = batch
        batch_mod.run_ci.return_value = True
        batch_mod.BatchError = Exception

        result = do_process(mock_client)

        assert result == "merged"
        batch_mod.create_batch.assert_called_once()
        batch_mod.run_ci.assert_called_once_with(mock_client, batch)
        batch_mod.complete_batch.assert_called_once_with(mock_client, batch)
        batch_mod.fail_batch.assert_not_called()

    @patch("merge_queue.cli.batch_mod")
    def test_ci_failed(self, batch_mod, mock_client):
        self._setup_queued_stack(mock_client)
        batch = Batch("123", "mq/123", Stack(prs=(), queued_at=T0))
        batch_mod.create_batch.return_value = batch
        batch_mod.run_ci.return_value = False
        batch_mod.BatchError = Exception

        result = do_process(mock_client)

        assert result == "ci_failed"
        batch_mod.fail_batch.assert_called_once()
        assert "CI failed" in batch_mod.fail_batch.call_args[0][2]

    @patch("merge_queue.cli.batch_mod")
    def test_batch_creation_error(self, batch_mod, mock_client):
        self._setup_queued_stack(mock_client)
        batch_mod.create_batch.side_effect = Exception("merge conflict")
        batch_mod.BatchError = Exception

        result = do_process(mock_client)

        assert result == "batch_error"
        mock_client.create_comment.assert_called()
        mock_client.remove_label.assert_called_with(1, "queue")

    @patch("merge_queue.cli.batch_mod")
    def test_complete_error_falls_back_to_fail(self, batch_mod, mock_client):
        self._setup_queued_stack(mock_client)
        batch = Batch("123", "mq/123", Stack(prs=(), queued_at=T0))
        batch_mod.create_batch.return_value = batch
        batch_mod.run_ci.return_value = True
        batch_mod.complete_batch.side_effect = Exception("SHA changed")
        batch_mod.BatchError = Exception

        result = do_process(mock_client)

        assert result == "complete_error"
        batch_mod.fail_batch.assert_called_once()

    @patch("merge_queue.cli.batch_mod")
    @patch("merge_queue.cli.rules_mod")
    def test_rules_failure(self, rules_mod, batch_mod, mock_client):
        from merge_queue.types import RuleResult
        rules_mod.check_all.return_value = [
            RuleResult("test_rule", False, "something is wrong"),
        ]
        result = do_process(mock_client)
        assert result == "rules_failed"
        batch_mod.create_batch.assert_not_called()

    @patch("merge_queue.cli.batch_mod")
    def test_posts_queued_comments(self, batch_mod, mock_client):
        self._setup_queued_stack(mock_client)
        batch = Batch("123", "mq/123", Stack(prs=(), queued_at=T0))
        batch_mod.create_batch.return_value = batch
        batch_mod.run_ci.return_value = True
        batch_mod.BatchError = Exception

        do_process(mock_client)

        # First call is from fetch_queued_prs flow; check for "Queued in batch" comment
        comments = [c[0][1] for c in mock_client.create_comment.call_args_list]
        assert any("Queued in batch" in c for c in comments)

    @patch("merge_queue.cli.batch_mod")
    @patch("merge_queue.cli.rules_mod")
    def test_filters_out_locked_stacks(self, rules_mod, batch_mod, mock_client):
        """Locked PRs should be filtered from stack selection (not picked for new batch)."""
        from merge_queue.types import RuleResult
        rules_mod.check_all.return_value = [RuleResult("ok", True, "")]

        mock_client.list_open_prs.return_value = [
            make_pr_data(1, "feat-a", "main", labels=["locked"], head_sha="sha-1"),
        ]
        mock_client.get_label_timestamp.return_value = T0

        result = do_process(mock_client)

        assert result == "no_stacks"
        batch_mod.create_batch.assert_not_called()


# --- do_enqueue ---


class TestDoEnqueue:
    def test_queued_waiting_when_batch_active(self, mock_client):
        mock_client.list_mq_branches.return_value = ["mq/123"]
        result = do_enqueue(mock_client, 1)
        assert result == "queued_waiting"
        mock_client.create_comment.assert_called_once()

    @patch("merge_queue.cli.do_process")
    def test_triggers_processing_when_idle(self, do_process_mock, mock_client):
        do_process_mock.return_value = "no_stacks"
        result = do_enqueue(mock_client, 1)
        assert result == "no_stacks"
        do_process_mock.assert_called_once_with(mock_client)


# --- do_abort ---


class TestDoAbort:
    def test_not_locked_noop(self, mock_client):
        mock_client.get_pr.return_value = {"labels": [{"name": "queue"}]}
        result = do_abort(mock_client, 1)
        assert result == "not_locked"

    @patch("merge_queue.cli.batch_mod")
    def test_aborts_when_locked(self, batch_mod, mock_client):
        mock_client.get_pr.return_value = {"labels": [{"name": "locked"}, {"name": "queue"}]}
        result = do_abort(mock_client, 1)
        assert result == "aborted"
        batch_mod.abort_batch.assert_called_once_with(mock_client)
        mock_client.create_comment.assert_called_once()


# --- do_check_rules ---


class TestDoCheckRules:
    def test_returns_results(self, mock_client):
        results = do_check_rules(mock_client)
        assert len(results) == 5
        assert all(r.passed for r in results)


# --- _dispatch_next_if_queued ---


class TestDispatchNext:
    def test_no_remaining_stacks(self, mock_client):
        assert _dispatch_next_if_queued(mock_client, "main") is False

    def test_dispatches_when_stacks_remain(self, mock_client):
        mock_client.list_open_prs.return_value = [
            make_pr_data(1, "feat-a", "main", labels=["queue"], head_sha="sha-1"),
        ]
        mock_client.get_label_timestamp.return_value = T0
        # Mock the session for dispatch
        mock_session = MagicMock()
        mock_session.post.return_value = MagicMock(status_code=204)
        mock_session.post.return_value.raise_for_status = MagicMock()
        mock_client._base_url = "https://api.github.com/repos/test/repo"
        mock_client._session = mock_session

        result = _dispatch_next_if_queued(mock_client, "main")
        assert result is True
        mock_session.post.assert_called_once()


# --- CLI thin wrappers ---


class TestCmdWrappers:
    @pytest.fixture
    def mock_env(self, monkeypatch):
        monkeypatch.setenv("GITHUB_REPOSITORY", "test/repo")
        monkeypatch.setenv("GITHUB_TOKEN", "fake-token")

    def test_cmd_enqueue(self, mock_env):
        args = MagicMock()
        args.pr_number = 1
        with patch("merge_queue.cli._make_client") as mc, \
             patch("merge_queue.cli.do_enqueue") as de:
            cmd_enqueue(args)
            de.assert_called_once_with(mc.return_value, 1)

    def test_cmd_process(self, mock_env):
        args = MagicMock()
        with patch("merge_queue.cli._make_client") as mc, \
             patch("merge_queue.cli.do_process", return_value="merged"):
            cmd_process(args)

    def test_cmd_process_exits_on_rules_failure(self, mock_env):
        args = MagicMock()
        with patch("merge_queue.cli._make_client"), \
             patch("merge_queue.cli.do_process", return_value="rules_failed"):
            with pytest.raises(SystemExit):
                cmd_process(args)

    def test_cmd_abort(self, mock_env):
        args = MagicMock()
        args.pr_number = 1
        with patch("merge_queue.cli._make_client") as mc, \
             patch("merge_queue.cli.do_abort") as da:
            cmd_abort(args)
            da.assert_called_once_with(mc.return_value, 1)

    def test_cmd_check_rules_passes(self, mock_env, capsys):
        args = MagicMock()
        with patch("merge_queue.cli._make_client") as mc:
            mc.return_value = MagicMock()
            mc.return_value.list_mq_branches.return_value = []
            mc.return_value.list_rulesets.return_value = []
            mc.return_value.list_open_prs.return_value = []
            mc.return_value.get_default_branch.return_value = "main"
            cmd_check_rules(args)
        output = capsys.readouterr().out
        assert "PASS" in output

    def test_cmd_check_rules_exits_on_failure(self, mock_env, capsys):
        args = MagicMock()
        with patch("merge_queue.cli._make_client") as mc:
            mc.return_value = MagicMock()
            mc.return_value.list_mq_branches.return_value = ["mq/1", "mq/2"]
            mc.return_value.list_rulesets.return_value = []
            mc.return_value.list_open_prs.return_value = []
            mc.return_value.get_default_branch.return_value = "main"
            with pytest.raises(SystemExit):
                cmd_check_rules(args)


class TestMain:
    def test_parses_enqueue(self, monkeypatch):
        monkeypatch.setenv("GITHUB_REPOSITORY", "test/repo")
        monkeypatch.setenv("GITHUB_TOKEN", "tok")
        with patch("merge_queue.cli._make_client"), \
             patch("merge_queue.cli.do_enqueue") as de:
            monkeypatch.setattr("sys.argv", ["merge-queue", "enqueue", "42"])
            main()
            de.assert_called_once()
            assert de.call_args[0][1] == 42

    def test_parses_process(self, monkeypatch):
        monkeypatch.setenv("GITHUB_REPOSITORY", "test/repo")
        monkeypatch.setenv("GITHUB_TOKEN", "tok")
        with patch("merge_queue.cli._make_client"), \
             patch("merge_queue.cli.do_process", return_value="no_stacks"):
            monkeypatch.setattr("sys.argv", ["merge-queue", "process"])
            main()

    def test_parses_abort(self, monkeypatch):
        monkeypatch.setenv("GITHUB_REPOSITORY", "test/repo")
        monkeypatch.setenv("GITHUB_TOKEN", "tok")
        with patch("merge_queue.cli._make_client"), \
             patch("merge_queue.cli.do_abort") as da:
            monkeypatch.setattr("sys.argv", ["merge-queue", "abort", "5"])
            main()
            assert da.call_args[0][1] == 5

    def test_parses_check_rules(self, monkeypatch):
        monkeypatch.setenv("GITHUB_REPOSITORY", "test/repo")
        monkeypatch.setenv("GITHUB_TOKEN", "tok")
        with patch("merge_queue.cli._make_client") as mc:
            mc.return_value = MagicMock()
            mc.return_value.list_mq_branches.return_value = []
            mc.return_value.list_rulesets.return_value = []
            mc.return_value.list_open_prs.return_value = []
            mc.return_value.get_default_branch.return_value = "main"
            monkeypatch.setattr("sys.argv", ["merge-queue", "check-rules"])
            main()
