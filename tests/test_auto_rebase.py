"""Tests for diverged/conflict handling in the merge queue."""

from __future__ import annotations

import copy
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from merge_queue import comments
from merge_queue.batch import BatchError

from tests.conftest import make_queue_entry, make_v2_state


def _make_entry(retry_count: int = 0) -> dict[str, Any]:
    entry = make_queue_entry(1, head_ref="feat-a")
    entry["target_branch"] = "main"
    if retry_count:
        entry["retry_count"] = retry_count
    return entry


def _setup_do_process(
    mock_client: MagicMock, mock_store: MagicMock, entry: dict
) -> None:
    from merge_queue.state import QueueState

    mock_store.read.return_value = make_v2_state(queue=[entry])
    pr_data = {
        "number": 1,
        "state": "open",
        "head": {"ref": "feat-a", "sha": "sha-1"},
        "base": {"ref": "main"},
        "labels": [{"name": "queue"}],
        "title": "PR title",
    }
    mock_client.get_pr.return_value = pr_data
    mock_client.list_open_prs.return_value = [pr_data]
    mock_client.get_pr_ci_status.return_value = (True, "")
    return QueueState(
        default_branch="main",
        mq_branches=[],
        rulesets=[],
        prs=[],
        all_pr_data=[],
    )


@pytest.mark.parametrize("retry_count", [0, 1, 2])
def test_diverged_requeues_with_incremented_retry_count(
    mock_client: MagicMock,
    mock_store: MagicMock,
    retry_count: int,
) -> None:
    """When complete_batch raises 'diverged', entry is re-queued with retry_count+1."""
    from merge_queue import cli

    entry = _make_entry(retry_count=retry_count)
    api_state = _setup_do_process(mock_client, mock_store, entry)

    batch = MagicMock()
    batch.batch_id = "999"
    batch.branch = "mq/999"
    batch.ruleset_id = 42

    with (
        patch("merge_queue.cli.QueueState") as mock_qs_cls,
        patch("merge_queue.cli.batch_mod.check_merge_conflict", return_value=None),
        patch("merge_queue.cli.batch_mod.create_batch", return_value=batch),
        patch(
            "merge_queue.cli.batch_mod.run_ci",
            return_value=MagicMock(passed=True, run_url=""),
        ),
        patch(
            "merge_queue.cli.batch_mod.complete_batch",
            side_effect=BatchError("diverged — another commit landed"),
        ),
        patch("merge_queue.cli.batch_mod.fail_batch"),
    ):
        mock_qs_cls.fetch.return_value = api_state

        written_states: list[dict] = []
        mock_store.write.side_effect = lambda s: written_states.append(copy.deepcopy(s))

        cli.do_process(mock_client)

    # Find the re-queued entry in the branch state
    requeued = None
    for s in written_states:
        branch_q = s.get("branches", {}).get("main", {}).get("queue", [])
        if branch_q:
            requeued = branch_q[0]
            break
    assert requeued is not None, "entry should have been re-queued"
    assert requeued["retry_count"] == retry_count + 1


def test_diverged_fails_permanently_when_retry_count_exhausted(
    mock_client: MagicMock,
    mock_store: MagicMock,
) -> None:
    """When retry_count >= 3 and diverged, fail permanently without re-queuing."""
    from merge_queue import cli

    entry = _make_entry(retry_count=3)
    api_state = _setup_do_process(mock_client, mock_store, entry)

    batch = MagicMock()
    batch.batch_id = "999"
    batch.branch = "mq/999"
    batch.ruleset_id = 42

    with (
        patch("merge_queue.cli.QueueState") as mock_qs_cls,
        patch("merge_queue.cli.batch_mod.check_merge_conflict", return_value=None),
        patch("merge_queue.cli.batch_mod.create_batch", return_value=batch),
        patch(
            "merge_queue.cli.batch_mod.run_ci",
            return_value=MagicMock(passed=True, run_url=""),
        ),
        patch(
            "merge_queue.cli.batch_mod.complete_batch",
            side_effect=BatchError("diverged — another commit landed"),
        ),
        patch("merge_queue.cli.batch_mod.fail_batch"),
    ):
        mock_qs_cls.fetch.return_value = api_state

        written_states: list[dict] = []
        mock_store.write.side_effect = lambda s: written_states.append(copy.deepcopy(s))

        result = cli.do_process(mock_client)

    assert result == "complete_error"
    # Ensure no re-queued entry in branch state
    requeued = None
    for s in written_states:
        branch_q = s.get("branches", {}).get("main", {}).get("queue", [])
        if branch_q:
            requeued = branch_q[0]
            break
    assert requeued is None, "entry must NOT be re-queued after retry_count exhausted"


def test_merge_conflict_comments_and_removes_queue_label(
    mock_client: MagicMock,
    mock_store: MagicMock,
) -> None:
    """When create_batch raises a conflict, post merge_conflict comment and remove label."""
    from merge_queue import cli

    entry = _make_entry()
    api_state = _setup_do_process(mock_client, mock_store, entry)

    with (
        patch("merge_queue.cli.QueueState") as mock_qs_cls,
        patch("merge_queue.cli.batch_mod.check_merge_conflict", return_value=None),
        patch(
            "merge_queue.cli.batch_mod.create_batch",
            side_effect=Exception("merge conflict detected in foo.py"),
        ),
        patch("merge_queue.cli.batch_mod.fail_batch"),
    ):
        mock_qs_cls.fetch.return_value = api_state
        result = cli.do_process(mock_client)

    assert result == "batch_error"
    # _comment() calls update_comment when comment_ids already has an entry for this PR
    all_comment_calls = (
        mock_client.update_comment.call_args_list
        + mock_client.create_comment.call_args_list
    )
    comment_bodies = [call.args[1] for call in all_comment_calls]
    assert any(
        "Merge conflict" in body or "merge conflict" in body.lower()
        for body in comment_bodies
    )
    mock_client.remove_label.assert_any_call(1, "queue")


def test_comment_templates() -> None:
    """Verify merge_conflict and auto_retrying templates contain expected text."""
    conflict_msg = comments.merge_conflict("main", owner="acme", repo="app")
    assert "merge conflicts" in conflict_msg
    assert "`main`" in conflict_msg
    assert "queue" in conflict_msg

    retry_msg = comments.auto_retrying("main", owner="acme", repo="app")
    assert "etrying" in retry_msg
    assert "`main`" in retry_msg
