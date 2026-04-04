"""Additional tests for cli.py — covering missing branches and guards."""

from __future__ import annotations

import datetime
from unittest.mock import MagicMock, patch

import pytest

from merge_queue.cli import (
    _comment,
    _stack_to_dicts,
    cmd_abort,
    cmd_check_rules,
    cmd_process,
    do_enqueue,
    do_process,
)
from merge_queue.state import QueueState
from merge_queue.types import PullRequest, Stack, empty_state

T0 = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)


def _api_state(prs: list | None = None) -> QueueState:
    return QueueState(
        default_branch="main",
        mq_branches=[],
        rulesets=[],
        prs=prs or [],
        all_pr_data=[],
    )


@pytest.fixture
def mock_store() -> MagicMock:
    with patch("merge_queue.cli.StateStore") as cls:
        store = MagicMock()
        store.read.return_value = empty_state()
        cls.return_value = store
        yield store


# --- _comment helper ---


def test_comment_creates_new_when_no_existing(mock_client: MagicMock) -> None:
    mock_client.create_comment.return_value = 101
    cid = _comment(mock_client, 5, "hello")
    mock_client.create_comment.assert_called_once_with(5, "hello")
    assert cid == 101


def test_comment_updates_existing_when_id_known(mock_client: MagicMock) -> None:
    mock_client.update_comment.return_value = None
    cid = _comment(mock_client, 5, "hello", comment_ids={5: 42})
    mock_client.update_comment.assert_called_once_with(42, "hello")
    assert cid == 42


def test_comment_updates_when_id_stored_as_string_key(mock_client: MagicMock) -> None:
    """comment_ids keys may be strings when deserialized from JSON."""
    mock_client.update_comment.return_value = None
    cid = _comment(mock_client, 5, "hello", comment_ids={"5": 99})
    mock_client.update_comment.assert_called_once_with(99, "hello")
    assert cid == 99


def test_comment_logs_warning_on_exception(mock_client: MagicMock) -> None:
    """Exception during comment should not propagate — returns None."""
    mock_client.create_comment.side_effect = RuntimeError("API down")
    cid = _comment(mock_client, 5, "hello")
    assert cid is None


# --- _stack_to_dicts ---


def test_stack_to_dicts_fetches_titles(mock_client: MagicMock) -> None:
    mock_client.get_pr.return_value = {"title": "My Feature", "head": {}, "base": {}}
    pr = PullRequest(
        number=7,
        head_sha="sha7",
        head_ref="feat-x",
        base_ref="main",
        labels=("queue",),
        queued_at=T0,
    )
    stack = Stack(prs=(pr,), queued_at=T0)

    result = _stack_to_dicts(stack, mock_client)

    assert len(result) == 1
    assert result[0]["number"] == 7
    assert result[0]["title"] == "My Feature"
    assert result[0]["head_sha"] == "sha7"


def test_stack_to_dicts_tolerates_api_error(mock_client: MagicMock) -> None:
    """If get_pr raises, title should be empty string and no exception raised."""
    mock_client.get_pr.side_effect = RuntimeError("not found")
    pr = PullRequest(
        number=8,
        head_sha="sha8",
        head_ref="feat-y",
        base_ref="main",
        labels=("queue",),
        queued_at=T0,
    )
    stack = Stack(prs=(pr,), queued_at=T0)

    result = _stack_to_dicts(stack, mock_client)

    assert result[0]["title"] == ""


# --- do_enqueue guards ---


def test_enqueue_pr_not_open_returns_pr_not_open(
    mock_client: MagicMock, mock_store: MagicMock
) -> None:
    mock_client.get_pr.return_value = {"state": "closed"}
    result = do_enqueue(mock_client, 1)
    assert result == "pr_not_open"
    mock_store.write.assert_not_called()


def test_enqueue_already_in_active_batch(
    mock_client: MagicMock, mock_store: MagicMock
) -> None:
    mock_client.get_pr.return_value = {"state": "open"}
    mock_store.read.return_value = {
        **empty_state(),
        "active_batch": {
            "stack": [{"number": 42}],
        },
    }
    result = do_enqueue(mock_client, 42)
    assert result == "already_active"
    mock_store.write.assert_not_called()


def test_enqueue_recently_processed_within_5_minutes(
    mock_client: MagicMock, mock_store: MagicMock
) -> None:
    """PR processed 30 seconds ago should be skipped as recently_processed."""
    mock_client.get_pr.return_value = {"state": "open"}
    recent = (
        datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=30)
    ).isoformat()
    mock_store.read.return_value = {
        **empty_state(),
        "history": [{"prs": [7], "completed_at": recent}],
    }
    result = do_enqueue(mock_client, 7)
    assert result == "recently_processed"
    mock_store.write.assert_not_called()


def test_enqueue_not_recently_processed_older_than_5_minutes(
    mock_client: MagicMock, mock_store: MagicMock
) -> None:
    """PR processed 10 minutes ago should NOT be considered recently_processed."""
    mock_client.get_pr.return_value = {
        "state": "open",
        "head": {"sha": "sha-7", "ref": "feat-z"},
        "base": {"ref": "main"},
        "title": "Old",
    }
    mock_client.create_comment.return_value = 1
    old_time = (
        datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=10)
    ).isoformat()
    mock_store.read.return_value = {
        **empty_state(),
        "history": [{"prs": [7], "completed_at": old_time}],
    }

    with (
        patch("merge_queue.cli.QueueState") as QS,
        patch("merge_queue.cli.do_process", return_value="queued_waiting"),
    ):
        QS.fetch.return_value = _api_state()
        result = do_enqueue(mock_client, 7)

    assert result != "recently_processed"


def test_enqueue_pr_get_raises_does_not_skip(
    mock_client: MagicMock, mock_store: MagicMock
) -> None:
    """If get_pr raises an exception, enqueue should proceed (not skip)."""
    mock_client.get_pr.side_effect = [
        RuntimeError("API error"),  # first call (state check) — raises → continues
        {  # second call (stack building fallback)
            "state": "open",
            "head": {"sha": "sha-9", "ref": "feat-w"},
            "base": {"ref": "main"},
            "title": "W",
        },
    ]
    mock_client.create_comment.return_value = 1

    with (
        patch("merge_queue.cli.QueueState") as QS,
        patch("merge_queue.cli.do_process", return_value="queued_waiting"),
    ):
        QS.fetch.return_value = _api_state()
        result = do_enqueue(mock_client, 9)

    assert result != "pr_not_open"


# --- do_process: stale batch recovery ---


def test_process_stale_batch_is_recovered(
    mock_client: MagicMock, mock_store: MagicMock
) -> None:
    """A batch older than 30 minutes is aborted and the queue is processed.

    The PR must be "open" so the code does NOT take the all_merged fast-path
    and instead checks the 30-minute stale threshold.
    """
    stale_time = (
        datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=35)
    ).isoformat()
    mock_store.read.return_value = {
        **empty_state(),
        "active_batch": {
            "batch_id": "stale",
            "started_at": stale_time,
            "stack": [{"number": 1}],
        },
        # No queue items → after recovering stale batch, returns "no_stacks"
    }
    # PR is still open → goes to stale-time check, not the all_merged fast-path
    mock_client.get_pr.return_value = {"state": "open"}

    with patch("merge_queue.cli.batch_mod") as bm:
        bm.abort_batch.return_value = None
        result = do_process(mock_client)

    bm.abort_batch.assert_called_once_with(mock_client)
    assert result == "no_stacks"


# --- cmd_process ---


def test_cmd_process_exits_1_on_rules_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    with (
        patch("merge_queue.cli._make_client"),
        patch("merge_queue.cli.do_process", return_value="rules_failed"),
    ):
        args = MagicMock()
        with pytest.raises(SystemExit) as exc_info:
            cmd_process(args)
        assert exc_info.value.code == 1


def test_cmd_process_no_exit_on_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    with (
        patch("merge_queue.cli._make_client"),
        patch("merge_queue.cli.do_process", return_value="merged"),
    ):
        args = MagicMock()
        cmd_process(args)  # should not raise


# --- cmd_abort ---


def test_cmd_abort_calls_do_abort(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    with (
        patch("merge_queue.cli._make_client"),
        patch("merge_queue.cli.do_abort", return_value="aborted") as da,
    ):
        args = MagicMock()
        args.pr_number = 7
        cmd_abort(args)
        da.assert_called_once()


# --- cmd_check_rules ---


def test_cmd_check_rules_exits_1_on_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    failing_result = MagicMock()
    failing_result.passed = False
    failing_result.name = "no_open_prs"
    failing_result.message = "Too many open PRs"
    with (
        patch("merge_queue.cli._make_client"),
        patch("merge_queue.cli.do_check_rules", return_value=[failing_result]),
    ):
        args = MagicMock()
        with pytest.raises(SystemExit) as exc_info:
            cmd_check_rules(args)
        assert exc_info.value.code == 1

    captured = capsys.readouterr()
    assert "FAIL" in captured.out
    assert "no_open_prs" in captured.out


def test_cmd_check_rules_no_exit_when_all_pass(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    passing_result = MagicMock()
    passing_result.passed = True
    passing_result.name = "some_rule"
    passing_result.message = "All good"
    with (
        patch("merge_queue.cli._make_client"),
        patch("merge_queue.cli.do_check_rules", return_value=[passing_result]),
    ):
        args = MagicMock()
        cmd_check_rules(args)  # should not raise

    captured = capsys.readouterr()
    assert "PASS" in captured.out
