"""Tests for auto-rebase functionality."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from merge_queue import comments
from merge_queue.batch import BatchError, auto_rebase

from tests.conftest import make_queue_entry, make_state


# ---------------------------------------------------------------------------
# auto_rebase unit tests
# ---------------------------------------------------------------------------


def _make_pr_data(head_ref: str = "feat-a") -> dict[str, Any]:
    return {"head": {"ref": head_ref, "sha": "abc123"}}


def test_auto_rebase_success(mock_client: MagicMock) -> None:
    git = MagicMock(return_value="")
    result = auto_rebase(mock_client, _make_pr_data(), "main", git=git)

    assert result is True
    assert git.call_count == 6
    git.assert_any_call("push", "origin", "HEAD:refs/heads/feat-a", "--force")


def test_auto_rebase_conflict_returns_false(mock_client: MagicMock) -> None:
    def _git(*args: str) -> str:
        if args[0] == "rebase":
            raise BatchError("CONFLICT in foo.py")
        return ""

    result = auto_rebase(mock_client, _make_pr_data(), "main", git=_git)

    assert result is False


def test_auto_rebase_non_conflict_error_returns_false(mock_client: MagicMock) -> None:
    abort_calls: list[tuple] = []

    def _git(*args: str) -> str:
        if args[0] == "rebase" and args != ("rebase", "--abort"):
            raise BatchError("fatal: not a git repository")
        if args == ("rebase", "--abort"):
            abort_calls.append(args)
        return ""

    result = auto_rebase(mock_client, _make_pr_data(), "main", git=_git)

    assert result is False
    assert len(abort_calls) == 1


# ---------------------------------------------------------------------------
# do_process auto-rebase integration tests
# ---------------------------------------------------------------------------


def _make_entry(rebase_attempted: bool = False) -> dict[str, Any]:
    entry = make_queue_entry(1, head_ref="feat-a")
    entry["target_branch"] = "main"
    if rebase_attempted:
        entry["rebase_attempted"] = True
    return entry


def _setup_process(mock_client: MagicMock, mock_store: MagicMock, entry: dict) -> None:
    """Configure mocks so do_process picks up entry from the queue."""
    from merge_queue.state import QueueState

    state = make_state(queue=[entry])
    mock_store.read.return_value = state

    api_state = QueueState(
        default_branch="main",
        mq_branches=[],
        rulesets=[],
        prs=[],
        all_pr_data=[],
    )
    mock_client.get_pr.return_value = {
        "number": 1,
        "state": "open",
        "head": {"ref": "feat-a", "sha": "sha-1"},
        "base": {"ref": "main"},
        "labels": [{"name": "queue"}],
        "title": "PR title",
    }
    mock_client.get_pr_ci_status.return_value = (True, "")

    with patch("merge_queue.cli.QueueState") as mock_qs_cls:
        mock_qs_cls.fetch.return_value = api_state
        yield


@pytest.fixture
def _patched_rules():
    with patch("merge_queue.cli.rules_mod.check_all", return_value=[]):
        yield


def test_do_process_auto_rebase_on_conflict(
    mock_client: MagicMock,
    mock_store: MagicMock,
    _patched_rules: None,
) -> None:
    """When create_batch raises a conflict error, MQ should rebase and re-queue."""
    from merge_queue import cli
    from merge_queue.state import QueueState

    entry = _make_entry()
    state = make_state(queue=[entry])
    mock_store.read.return_value = state

    mock_client.get_pr.return_value = {
        "number": 1,
        "state": "open",
        "head": {"ref": "feat-a", "sha": "sha-1"},
        "base": {"ref": "main"},
        "labels": [{"name": "queue"}],
        "title": "PR title",
    }

    api_state = QueueState(
        default_branch="main",
        mq_branches=[],
        rulesets=[],
        prs=[],
        all_pr_data=[],
    )

    call_count = 0

    def _fake_create_batch(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise Exception("merge conflict detected")
        # Second call succeeds
        b = MagicMock()
        b.batch_id = "999"
        b.branch = "mq/999"
        b.ruleset_id = 42
        return b

    with (
        patch("merge_queue.cli.QueueState") as mock_qs_cls,
        patch("merge_queue.cli.batch_mod.create_batch", side_effect=_fake_create_batch),
        patch("merge_queue.cli.batch_mod.auto_rebase", return_value=True),
        patch("merge_queue.cli.batch_mod.run_ci") as mock_run_ci,
        patch("merge_queue.cli.batch_mod.complete_batch"),
        patch("merge_queue.cli.batch_mod.fail_batch"),
    ):
        mock_qs_cls.fetch.return_value = api_state
        mock_run_ci.return_value = MagicMock(passed=True, run_url="")

        result = cli.do_process(mock_client)

    assert result in ("merged", "batch_active", "no_stacks", "batch_error", "merged")
    assert call_count >= 1


def test_do_process_no_rebase_loop(
    mock_client: MagicMock,
    mock_store: MagicMock,
    _patched_rules: None,
) -> None:
    """When rebase_attempted is already set, do not attempt another rebase."""
    from merge_queue import cli
    from merge_queue.state import QueueState

    entry = _make_entry(rebase_attempted=True)
    state = make_state(queue=[entry])
    mock_store.read.return_value = state

    mock_client.get_pr.return_value = {
        "number": 1,
        "state": "open",
        "head": {"ref": "feat-a", "sha": "sha-1"},
        "base": {"ref": "main"},
        "labels": [{"name": "queue"}],
        "title": "PR title",
    }

    api_state = QueueState(
        default_branch="main",
        mq_branches=[],
        rulesets=[],
        prs=[],
        all_pr_data=[],
    )

    with (
        patch("merge_queue.cli.QueueState") as mock_qs_cls,
        patch(
            "merge_queue.cli.batch_mod.create_batch",
            side_effect=Exception("merge conflict detected"),
        ),
        patch("merge_queue.cli.batch_mod.auto_rebase") as mock_rebase,
    ):
        mock_qs_cls.fetch.return_value = api_state

        result = cli.do_process(mock_client)

    # auto_rebase should NOT be called when rebase_attempted is already True
    mock_rebase.assert_not_called()
    assert result == "batch_error"


# ---------------------------------------------------------------------------
# Comment template tests
# ---------------------------------------------------------------------------


def test_auto_rebased_comment() -> None:
    body = comments.auto_rebased("main", owner="acme", repo="app")
    assert "Auto-rebased" in body
    assert "`main`" in body


def test_rebase_failed_comment() -> None:
    body = comments.rebase_failed("CONFLICT in foo.py", owner="acme", repo="app")
    assert "Auto-rebase failed" in body
    assert "CONFLICT in foo.py" in body
    assert "queue" in body
