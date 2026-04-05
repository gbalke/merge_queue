"""Additional tests for store.py — covering missing branches."""

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
    store._state_sha = "old-sha"
    retry_sha = "retry-sha"
    final_sha = "final-sha"

    # state.json write succeeds; root STATUS.md first write 409; re-read; retry succeeds
    mock_client.put_file_content.side_effect = [
        {"content": {"sha": "new-state-sha"}},  # state.json
        RuntimeError("409 Conflict"),  # root STATUS.md first attempt
        {"content": {"sha": final_sha}},  # root STATUS.md retry
    ]
    mock_client.get_file_content.return_value = {
        "sha": retry_sha,
        "content": _encoded({}),
    }

    with _patch_status_renders():
        store.write(empty_state())

    # 3 calls: state.json, root STATUS.md (fail), root STATUS.md (retry)
    assert mock_client.put_file_content.call_count == 3
    assert store._status_shas.get("STATUS.md") == final_sha


def test_write_status_md_conflict_retry_also_fails_logs_warning(
    store: StateStore, mock_client: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    """If the root STATUS.md retry also fails, a warning is logged and write does not raise."""
    store._state_sha = "old-sha"

    mock_client.put_file_content.side_effect = [
        {"content": {"sha": "new-sha"}},  # state.json succeeds
        RuntimeError("409 Conflict"),  # root STATUS.md first attempt
        RuntimeError("500 Server Error"),  # root STATUS.md retry also fails
    ]
    mock_client.get_file_content.return_value = {"sha": "rx", "content": _encoded({})}

    with _patch_status_renders():
        store.write(empty_state())  # must not raise

    assert mock_client.put_file_content.call_count == 3


def test_write_status_md_non_404_non_409_logs_warning(
    store: StateStore, mock_client: MagicMock
) -> None:
    """A non-404/non-409 error on STATUS.md logs a warning but does not raise."""
    store._state_sha = "old-sha"

    mock_client.put_file_content.side_effect = [
        {"content": {"sha": "new-sha"}},  # state.json succeeds
        RuntimeError("403 Forbidden"),  # root STATUS.md forbidden — not 404, not 409
    ]

    with _patch_status_renders():
        store.write(empty_state())  # must not raise

    # Only 2 calls — no retry for non-409 errors
    assert mock_client.put_file_content.call_count == 2


def test_write_status_md_404_is_silently_ignored(
    store: StateStore, mock_client: MagicMock
) -> None:
    """A 404 error on STATUS.md (e.g. branch gone) is silently swallowed."""
    store._state_sha = "old-sha"

    mock_client.put_file_content.side_effect = [
        {"content": {"sha": "new-sha"}},  # state.json succeeds
        RuntimeError("404 Not Found"),  # root STATUS.md — 404 is ignored
    ]

    with _patch_status_renders():
        store.write(empty_state())  # must not raise

    assert mock_client.put_file_content.call_count == 2


# --- _ensure_branch() 422 race condition ---


# --- write() state.json retry with backoff ---


def test_write_conflict_retries_with_backoff(
    store: StateStore, mock_client: MagicMock
) -> None:
    """On 409 conflicts, write retries up to max_retries=5 and sleeps between attempts."""
    store._state_sha = "old-sha"

    # First 4 attempts conflict; 5th succeeds
    conflict = RuntimeError("409 Conflict")
    mock_client.put_file_content.side_effect = [
        conflict,
        conflict,
        conflict,
        conflict,
        {"content": {"sha": "final-sha"}},
    ]
    mock_client.get_file_content.return_value = {
        "sha": "refreshed-sha",
        "content": base64.b64encode(json.dumps(empty_state()).encode()).decode(),
    }

    with (
        patch("merge_queue.store.time.sleep") as mock_sleep,
        patch("merge_queue.store.render_branch_status_md", return_value="# b\n"),
        patch("merge_queue.store.render_root_status_md", return_value="# r\n"),
    ):
        store.write(empty_state())

    # 4 conflicts → 4 sleeps before the 5th (successful) attempt
    assert mock_sleep.call_count == 4
    assert store._state_sha == "final-sha"


def test_write_exhausts_five_retries_then_raises(
    store: StateStore, mock_client: MagicMock
) -> None:
    """After 5 failed attempts, ConflictError is raised."""
    store._state_sha = "old-sha"
    mock_client.put_file_content.side_effect = RuntimeError("409 Conflict")
    mock_client.get_file_content.return_value = {
        "sha": "rx",
        "content": base64.b64encode(json.dumps(empty_state()).encode()).decode(),
    }

    with (
        patch("merge_queue.store.time.sleep"),
        pytest.raises(ConflictError),
    ):
        store.write(empty_state())

    assert mock_client.put_file_content.call_count == 5


# --- _ensure_branch() 422 race condition ---


def test_ensure_branch_already_exists_422_is_swallowed(
    store: StateStore, mock_client: MagicMock
) -> None:
    """If create_orphan_branch raises 422 already exists, it's a race — silently ignored."""
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
