"""Additional tests for store.py -- conflict retry, write_with_retry, and STATUS.md."""

from __future__ import annotations

import base64
import json
from unittest.mock import MagicMock, patch

import pytest

from merge_queue.store import ConflictError, StateStore
from merge_queue.types import empty_state


@pytest.fixture
def store(mock_client: MagicMock) -> StateStore:
    return StateStore(mock_client)


def _encoded(state: dict) -> str:
    return base64.b64encode(json.dumps(state).encode()).decode()


def _state_response(state: dict, sha: str = "abc123") -> dict:
    return {"sha": sha, "content": _encoded(state)}


# --- read() ---


def test_read_reraises_non_404(store: StateStore, mock_client: MagicMock) -> None:
    """Non-404 exceptions from get_file_content should propagate."""
    mock_client.get_file_content.side_effect = RuntimeError("500 Server Error")

    with pytest.raises(RuntimeError, match="500"):
        store.read()


# --- write() STATUS.md conflict retry ---


def _patch_status_renders():
    """Context manager that stubs out both render functions used by store.write."""
    from contextlib import ExitStack

    stack = ExitStack()
    stack.enter_context(
        patch("merge_queue.store.render_branch_status_md", return_value="# branch\n")
    )
    stack.enter_context(
        patch("merge_queue.store.render_root_status_md", return_value="# root\n")
    )
    return stack


def test_write_status_md_conflict_retries_once(
    store: StateStore, mock_client: MagicMock
) -> None:
    """On root STATUS.md 409, the store re-reads the SHA and retries the write."""
    # Force legacy write path (no atomic commit_files)
    del mock_client.commit_files
    mock_client.get_file_content.return_value = _state_response(
        empty_state(), "current-sha"
    )

    final_sha = "final-sha"
    mock_client.put_file_content.side_effect = [
        {"content": {"sha": "new-state-sha"}},  # state.json
        RuntimeError("409 Conflict"),  # root STATUS.md first attempt
        {"content": {"sha": final_sha}},  # root STATUS.md retry
    ]

    with _patch_status_renders():
        store.write(empty_state())

    # 3 calls: state.json, root STATUS.md (fail), root STATUS.md (retry)
    assert mock_client.put_file_content.call_count == 3
    assert store._status_shas.get("STATUS.md") == final_sha


def test_write_status_md_conflict_retry_also_fails_logs_warning(
    store: StateStore, mock_client: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    """If the root STATUS.md retry also fails, a warning is logged and write does not raise."""
    del mock_client.commit_files
    mock_client.get_file_content.return_value = _state_response(empty_state(), "sha-x")

    mock_client.put_file_content.side_effect = [
        {"content": {"sha": "new-sha"}},  # state.json succeeds
        RuntimeError("409 Conflict"),  # root STATUS.md first attempt
        RuntimeError("500 Server Error"),  # root STATUS.md retry also fails
    ]

    with _patch_status_renders():
        store.write(empty_state())  # must not raise

    assert mock_client.put_file_content.call_count == 3


def test_write_status_md_non_404_non_409_logs_warning(
    store: StateStore, mock_client: MagicMock
) -> None:
    """A non-404/non-409 error on STATUS.md logs a warning but does not raise."""
    del mock_client.commit_files
    mock_client.get_file_content.return_value = _state_response(empty_state(), "sha-x")

    mock_client.put_file_content.side_effect = [
        {"content": {"sha": "new-sha"}},  # state.json succeeds
        RuntimeError("403 Forbidden"),  # root STATUS.md forbidden -- not 404, not 409
    ]

    with _patch_status_renders():
        store.write(empty_state())  # must not raise

    # Only 2 calls -- no retry for non-409 errors
    assert mock_client.put_file_content.call_count == 2


def test_write_status_md_404_is_silently_ignored(
    store: StateStore, mock_client: MagicMock
) -> None:
    """A 404 error on STATUS.md (e.g. branch gone) is silently swallowed."""
    del mock_client.commit_files
    mock_client.get_file_content.return_value = _state_response(empty_state(), "sha-x")

    mock_client.put_file_content.side_effect = [
        {"content": {"sha": "new-sha"}},  # state.json succeeds
        RuntimeError("404 Not Found"),  # root STATUS.md -- 404 is ignored
    ]

    with _patch_status_renders():
        store.write(empty_state())  # must not raise

    assert mock_client.put_file_content.call_count == 2


# --- _ensure_branch() 422 race condition ---


def test_ensure_branch_already_exists_422_is_swallowed(
    store: StateStore, mock_client: MagicMock
) -> None:
    """If create_orphan_branch raises 422 already exists, it is a race -- silently ignored."""
    mock_client.get_file_content.side_effect = RuntimeError("404")
    mock_client.create_orphan_branch.side_effect = RuntimeError("422 already exists")

    store._ensure_branch()  # must not raise

    mock_client.create_orphan_branch.assert_called_once()


def test_ensure_branch_non_422_error_propagates(
    store: StateStore, mock_client: MagicMock
) -> None:
    """Non-422 errors from create_orphan_branch should propagate."""
    mock_client.get_file_content.side_effect = RuntimeError("404")
    mock_client.create_orphan_branch.side_effect = RuntimeError("500 Server Error")

    with pytest.raises(RuntimeError, match="500"):
        store._ensure_branch()


# --- write() state.json retry -- write_with_retry re-reads on every attempt ---


def test_write_conflict_retries_with_backoff(
    store: StateStore, mock_client: MagicMock
) -> None:
    """On 409 conflicts, write retries and sleeps between attempts.

    write_with_retry re-reads state on every attempt (not just on conflict),
    so the SHA is always fresh -- this is the core fix for the 409 race.
    """
    mock_client.get_file_content.return_value = _state_response(
        empty_state(), "refreshed-sha"
    )

    # First 4 attempts conflict; 5th succeeds (atomic commit_files path)
    conflict = RuntimeError("409 Conflict")
    mock_client.commit_files.side_effect = [
        conflict,
        conflict,
        conflict,
        conflict,
        "new-commit-sha",
    ]

    with (
        patch("merge_queue.store.time.sleep") as mock_sleep,
        patch("merge_queue.store.render_branch_status_md", return_value="# b\n"),
        patch("merge_queue.store.render_root_status_md", return_value="# r\n"),
    ):
        store.write(empty_state())

    # 4 conflicts -> 4 sleeps before the 5th (successful) attempt
    assert mock_sleep.call_count == 4


def test_write_exhausts_retries_then_raises(
    store: StateStore, mock_client: MagicMock
) -> None:
    """After max retries failed attempts, ConflictError is raised."""
    mock_client.get_file_content.return_value = _state_response(empty_state(), "rx")
    mock_client.commit_files.side_effect = RuntimeError("409 Conflict")

    with (
        patch("merge_queue.store.time.sleep"),
        pytest.raises(ConflictError),
    ):
        store.write(empty_state())

    assert mock_client.commit_files.call_count == 7


# --- write_with_retry() -- the core read-mutate-write loop ---


def test_write_with_retry_applies_mutation_on_fresh_read(
    store: StateStore, mock_client: MagicMock
) -> None:
    """write_with_retry reads state, applies the mutation, and writes the result."""
    initial = empty_state()
    initial["branches"]["main"] = {"queue": [], "active_batch": None}
    mock_client.get_file_content.return_value = _state_response(initial, "sha-1")
    mock_client.commit_files.return_value = "new-commit-sha"

    def add_entry(state: dict) -> None:
        state["branches"]["main"]["queue"].append({"position": 1, "stack": []})

    with (
        patch("merge_queue.store.render_branch_status_md", return_value="# b\n"),
        patch("merge_queue.store.render_root_status_md", return_value="# r\n"),
    ):
        result = store.write_with_retry(add_entry)

    assert len(result["branches"]["main"]["queue"]) == 1


def test_write_with_retry_reapplies_mutation_after_conflict(
    store: StateStore, mock_client: MagicMock
) -> None:
    """On 409, write_with_retry re-reads the remote state and re-applies the mutation.

    This is the core fix: after a conflict, the fresh remote state is read and
    the mutation is applied on top -- not the stale pre-conflict snapshot.
    The final written state incorporates changes from both concurrent writers.
    """
    initial = empty_state()
    initial["branches"]["main"] = {"queue": [], "active_batch": None}
    # After the conflict, the remote has a new entry added by the other writer
    after_conflict = empty_state()
    after_conflict["branches"]["main"] = {
        "queue": [{"position": 1, "stack": [{"number": 99}]}],
        "active_batch": None,
    }

    mock_client.get_file_content.side_effect = [
        _state_response(initial, "sha-1"),  # _ensure_branch check
        _state_response(initial, "sha-1"),  # first read() attempt
        _state_response(after_conflict, "sha-2"),  # second read() after conflict
    ]
    mock_client.commit_files.side_effect = [
        RuntimeError("409 Conflict"),  # first write fails
        "new-commit-sha",  # second write succeeds
    ]

    entries_added: list[int] = []

    def add_pr42(state: dict) -> None:
        branch = state.setdefault("branches", {}).setdefault(
            "main", {"queue": [], "active_batch": None}
        )
        branch["queue"].append(
            {"position": len(branch["queue"]) + 1, "stack": [{"number": 42}]}
        )
        entries_added.append(1)

    with (
        patch("merge_queue.store.time.sleep"),
        patch("merge_queue.store.render_branch_status_md", return_value="# b\n"),
        patch("merge_queue.store.render_root_status_md", return_value="# r\n"),
    ):
        result = store.write_with_retry(add_pr42)

    # Mutation was applied twice (once per attempt)
    assert len(entries_added) == 2
    # Final state has PR #99 (from the remote conflict commit) AND PR #42 (our addition)
    queue = result["branches"]["main"]["queue"]
    pr_numbers = [pr["number"] for entry in queue for pr in entry["stack"]]
    assert 42 in pr_numbers
    assert 99 in pr_numbers


def test_write_with_retry_raises_conflict_after_exhausting_retries(
    store: StateStore, mock_client: MagicMock
) -> None:
    """write_with_retry raises ConflictError after max_retries failed attempts."""
    mock_client.get_file_content.return_value = _state_response(empty_state(), "sha-x")
    mock_client.commit_files.side_effect = RuntimeError("409 Conflict")

    with (
        patch("merge_queue.store.time.sleep"),
        pytest.raises(ConflictError, match="after 3 attempts"),
    ):
        store.write_with_retry(lambda s: None, max_retries=3)

    assert mock_client.commit_files.call_count == 3
