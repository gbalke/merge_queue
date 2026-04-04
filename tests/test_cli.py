"""Tests for cli.py — command routing and argument parsing."""

from __future__ import annotations

import datetime
from unittest.mock import MagicMock, patch

import pytest

from merge_queue.cli import cmd_abort, cmd_check_rules, cmd_enqueue, cmd_process

T0 = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)


@pytest.fixture
def mock_env(monkeypatch):
    monkeypatch.setenv("GITHUB_REPOSITORY", "test/repo")
    monkeypatch.setenv("GITHUB_TOKEN", "fake-token")


@pytest.fixture
def client_mock(mock_env):
    with patch("merge_queue.cli.GitHubClient") as cls:
        client = MagicMock()
        client.get_default_branch.return_value = "main"
        client.list_mq_branches.return_value = []
        client.list_rulesets.return_value = []
        client.list_open_prs.return_value = []
        client.get_branch_sha.return_value = "abc123"
        client.compare_commits.return_value = "ahead"
        client.create_ruleset.return_value = 42
        client.poll_ci.return_value = True
        cls.return_value = client
        yield client


class TestCmdEnqueue:
    def test_comments_and_triggers_process(self, client_mock):
        args = MagicMock()
        args.pr_number = 1

        cmd_enqueue(args)

        client_mock.create_comment.assert_called_once()
        assert "queued" in client_mock.create_comment.call_args[0][1].lower()

    def test_skips_processing_when_batch_active(self, client_mock):
        args = MagicMock()
        args.pr_number = 1
        client_mock.list_mq_branches.return_value = ["mq/123"]

        cmd_enqueue(args)

        # Should comment but not try to process
        client_mock.create_comment.assert_called_once()


class TestCmdProcess:
    def test_no_queued_stacks(self, client_mock):
        args = MagicMock()
        cmd_process(args)
        # Should not create any batch
        client_mock.create_ruleset.assert_not_called()

    def test_skips_when_batch_active(self, client_mock):
        args = MagicMock()
        client_mock.list_mq_branches.return_value = ["mq/123"]

        cmd_process(args)

        client_mock.create_ruleset.assert_not_called()


class TestCmdAbort:
    def test_noop_when_not_locked(self, client_mock):
        args = MagicMock()
        args.pr_number = 1
        client_mock.get_pr.return_value = {"labels": [{"name": "queue"}]}

        cmd_abort(args)

        # Should not call abort_batch
        client_mock.list_rulesets.assert_not_called()

    def test_aborts_when_locked(self, client_mock):
        args = MagicMock()
        args.pr_number = 1
        client_mock.get_pr.return_value = {"labels": [{"name": "locked"}, {"name": "queue"}]}

        with patch("merge_queue.cli.batch_mod") as batch_mod:
            cmd_abort(args)
            batch_mod.abort_batch.assert_called_once_with(client_mock)

        client_mock.create_comment.assert_called_once()


class TestCmdCheckRules:
    def test_passes_clean_state(self, client_mock, capsys):
        args = MagicMock()
        cmd_check_rules(args)
        output = capsys.readouterr().out
        assert "PASS" in output

    def test_exits_on_failure(self, client_mock, capsys):
        args = MagicMock()
        client_mock.list_mq_branches.return_value = ["mq/1", "mq/2"]

        with pytest.raises(SystemExit):
            cmd_check_rules(args)

        output = capsys.readouterr().out
        assert "FAIL" in output
