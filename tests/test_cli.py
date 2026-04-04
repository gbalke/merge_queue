"""Tests for cli.py — core logic functions and command routing."""

from __future__ import annotations

import datetime
from unittest.mock import MagicMock, patch

import pytest

from merge_queue.cli import (
    _dispatch_next_if_queued,
    _make_client,
    do_abort,
    do_check_rules,
    do_enqueue,
    do_process,
    cmd_enqueue,
    cmd_process,
    cmd_abort,
    cmd_check_rules,
    main,
)
from merge_queue.state import QueueState
from merge_queue.types import Batch, BatchStatus, PullRequest, Stack

from tests.conftest import make_pr, make_pr_data

T0 = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
T1 = datetime.datetime(2026, 1, 1, 0, 1, tzinfo=datetime.timezone.utc)


def _state(prs=None, mq_branches=None, rulesets=None, default_branch="main"):
    return QueueState(
        default_branch=default_branch,
        mq_branches=mq_branches or [],
        rulesets=rulesets or [],
        prs=prs or [],
        all_pr_data=[],
    )


def _queued_pr(number, head_ref, base_ref="main", queued_at=T0):
    return PullRequest(number, f"sha-{number}", head_ref, base_ref, ("queue",), queued_at)


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


# --- do_process ---


class TestDoProcess:
    def test_batch_active_skips(self, mock_client):
        state = _state(mq_branches=["mq/123"])
        assert do_process(mock_client, state=state) == "batch_active"

    def test_no_stacks_returns(self, mock_client):
        assert do_process(mock_client, state=_state()) == "no_stacks"

    @patch("merge_queue.cli.batch_mod")
    def test_happy_path_merged(self, batch_mod, mock_client):
        state = _state(prs=[_queued_pr(1, "feat-a")])
        batch = Batch("123", "mq/123", Stack(prs=(), queued_at=T0))
        batch_mod.create_batch.return_value = batch
        batch_mod.run_ci.return_value = True
        batch_mod.BatchError = Exception

        result = do_process(mock_client, state=state)

        assert result == "merged"
        batch_mod.create_batch.assert_called_once()
        batch_mod.run_ci.assert_called_once()
        batch_mod.complete_batch.assert_called_once()

    @patch("merge_queue.cli.batch_mod")
    def test_ci_failed(self, batch_mod, mock_client):
        state = _state(prs=[_queued_pr(1, "feat-a")])
        batch = Batch("123", "mq/123", Stack(prs=(), queued_at=T0))
        batch_mod.create_batch.return_value = batch
        batch_mod.run_ci.return_value = False
        batch_mod.BatchError = Exception

        assert do_process(mock_client, state=state) == "ci_failed"
        batch_mod.fail_batch.assert_called_once()

    @patch("merge_queue.cli.batch_mod")
    def test_batch_creation_error(self, batch_mod, mock_client):
        state = _state(prs=[_queued_pr(1, "feat-a")])
        batch_mod.create_batch.side_effect = Exception("merge conflict")
        batch_mod.BatchError = Exception

        assert do_process(mock_client, state=state) == "batch_error"
        mock_client.create_comment.assert_called()
        mock_client.remove_label.assert_called_with(1, "queue")

    @patch("merge_queue.cli.batch_mod")
    def test_complete_error_falls_back_to_fail(self, batch_mod, mock_client):
        state = _state(prs=[_queued_pr(1, "feat-a")])
        batch = Batch("123", "mq/123", Stack(prs=(), queued_at=T0))
        batch_mod.create_batch.return_value = batch
        batch_mod.run_ci.return_value = True
        batch_mod.complete_batch.side_effect = Exception("SHA changed")
        batch_mod.BatchError = Exception

        assert do_process(mock_client, state=state) == "complete_error"
        batch_mod.fail_batch.assert_called_once()

    def test_rules_failure(self, mock_client):
        # Two mq branches = single_active_batch rule fails
        state = _state(mq_branches=["mq/1", "mq/2"])
        assert do_process(mock_client, state=state) == "batch_active"

        # No mq branches but orphaned locks
        state2 = _state(prs=[
            PullRequest(1, "sha-1", "feat-a", "main", ("locked",), T0),
        ])
        assert do_process(mock_client, state=state2) == "rules_failed"

    @patch("merge_queue.cli.batch_mod")
    def test_filters_out_locked_stacks(self, batch_mod, mock_client):
        state = _state(prs=[
            PullRequest(1, "sha-1", "feat-a", "main", ("locked",), T0),
        ])
        # Rules will fail (orphaned lock) before we get to stack selection
        assert do_process(mock_client, state=state) == "rules_failed"

    @patch("merge_queue.cli.batch_mod")
    def test_posts_queued_comments(self, batch_mod, mock_client):
        state = _state(prs=[_queued_pr(1, "feat-a")])
        batch = Batch("123", "mq/123", Stack(prs=(), queued_at=T0))
        batch_mod.create_batch.return_value = batch
        batch_mod.run_ci.return_value = True
        batch_mod.BatchError = Exception

        do_process(mock_client, state=state)

        comments = [c[0][1] for c in mock_client.create_comment.call_args_list]
        assert any("Queued in batch" in c for c in comments)

    @patch("merge_queue.cli.batch_mod")
    def test_fetches_state_if_not_provided(self, batch_mod, mock_client):
        """If no state passed, fetches it from client."""
        with patch("merge_queue.cli.QueueState") as QS:
            QS.fetch.return_value = _state()
            do_process(mock_client)
            QS.fetch.assert_called_once_with(mock_client)


# --- do_enqueue ---


class TestDoEnqueue:
    def test_queued_waiting_when_batch_active(self, mock_client):
        with patch("merge_queue.cli.QueueState") as QS:
            QS.fetch.return_value = _state(mq_branches=["mq/123"])
            result = do_enqueue(mock_client, 1)
        assert result == "queued_waiting"
        mock_client.create_comment.assert_called_once()

    @patch("merge_queue.cli.do_process")
    def test_triggers_processing_when_idle(self, do_process_mock, mock_client):
        do_process_mock.return_value = "no_stacks"
        with patch("merge_queue.cli.QueueState") as QS:
            state = _state()
            QS.fetch.return_value = state
            result = do_enqueue(mock_client, 1)
        assert result == "no_stacks"
        do_process_mock.assert_called_once_with(mock_client, state=state)


# --- do_abort ---


class TestDoAbort:
    def test_not_locked_noop(self, mock_client):
        mock_client.get_pr.return_value = {"labels": [{"name": "queue"}]}
        assert do_abort(mock_client, 1) == "not_locked"

    @patch("merge_queue.cli.batch_mod")
    def test_aborts_when_locked(self, batch_mod, mock_client):
        mock_client.get_pr.return_value = {"labels": [{"name": "locked"}, {"name": "queue"}]}
        assert do_abort(mock_client, 1) == "aborted"
        batch_mod.abort_batch.assert_called_once_with(mock_client)


# --- do_check_rules ---


class TestDoCheckRules:
    def test_returns_results(self, mock_client):
        with patch("merge_queue.cli.QueueState") as QS:
            QS.fetch.return_value = _state()
            results = do_check_rules(mock_client)
        assert len(results) == 5
        assert all(r.passed for r in results)


# --- _dispatch_next_if_queued ---


class TestDispatchNext:
    def test_no_remaining_stacks(self, mock_client):
        with patch("merge_queue.cli.QueueState") as QS:
            QS.fetch.return_value = _state()
            assert _dispatch_next_if_queued(mock_client, "main") is False

    def test_dispatches_when_stacks_remain(self, mock_client):
        mock_session = MagicMock()
        mock_session.post.return_value = MagicMock(status_code=204)
        mock_session.post.return_value.raise_for_status = MagicMock()
        mock_client._base_url = "https://api.github.com/repos/test/repo"
        mock_client._session = mock_session

        with patch("merge_queue.cli.QueueState") as QS:
            QS.fetch.return_value = _state(prs=[_queued_pr(1, "feat-a")])
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
        with patch("merge_queue.cli._make_client"), \
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
        with patch("merge_queue.cli._make_client"), \
             patch("merge_queue.cli.do_check_rules") as dcr:
            from merge_queue.types import RuleResult
            dcr.return_value = [RuleResult("test", True, "ok")]
            cmd_check_rules(args)
        assert "PASS" in capsys.readouterr().out

    def test_cmd_check_rules_exits_on_failure(self, mock_env, capsys):
        args = MagicMock()
        with patch("merge_queue.cli._make_client"), \
             patch("merge_queue.cli.do_check_rules") as dcr:
            from merge_queue.types import RuleResult
            dcr.return_value = [RuleResult("test", False, "bad")]
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
        with patch("merge_queue.cli._make_client"), \
             patch("merge_queue.cli.do_check_rules") as dcr:
            from merge_queue.types import RuleResult
            dcr.return_value = [RuleResult("test", True, "ok")]
            monkeypatch.setattr("sys.argv", ["merge-queue", "check-rules"])
            main()
