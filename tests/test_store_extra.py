"""Additional tests for store.py — covering missing branches."""

from __future__ import annotations

import base64
import json
from unittest.mock import MagicMock, patch

import pytest

from merge_queue.store import StateStore
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


def test_write_status_md_conflict_retries_once(
    store: StateStore, mock_client: MagicMock
) -> None:
    """On STATUS.md 409, the store re-reads the SHA and retries the write."""
    store._state_sha = "old-sha"
    retry_sha = "retry-sha"
    final_sha = "final-sha"

    # state.json write succeeds; STATUS.md first write 409; re-read; retry succeeds
    mock_client.put_file_content.side_effect = [
        {"content": {"sha": "new-state-sha"}},  # state.json
        RuntimeError("409 Conflict"),  # STATUS.md first attempt
        {"content": {"sha": final_sha}},  # STATUS.md retry
    ]
    mock_client.get_file_content.return_value = {
        "sha": retry_sha,
        "content": _encoded({}),
    }

    with patch("merge_queue.store.render_status_md", return_value="# md\n"):
        store.write(empty_state())

    # Should have tried put_file_content 3 times
    assert mock_client.put_file_content.call_count == 3
    assert store._status_sha == final_sha


def test_write_status_md_conflict_retry_also_fails_logs_warning(
    store: StateStore, mock_client: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    """If the STATUS.md retry also fails, a warning is logged and write does not raise."""
    store._state_sha = "old-sha"

    mock_client.put_file_content.side_effect = [
        {"content": {"sha": "new-sha"}},  # state.json succeeds
        RuntimeError("409 Conflict"),  # STATUS.md first attempt
        RuntimeError("500 Server Error"),  # STATUS.md retry also fails
    ]
    mock_client.get_file_content.return_value = {"sha": "rx", "content": _encoded({})}

    with patch("merge_queue.store.render_status_md", return_value="# md\n"):
        store.write(empty_state())  # must not raise

    assert mock_client.put_file_content.call_count == 3


def test_write_status_md_non_404_non_409_logs_warning(
    store: StateStore, mock_client: MagicMock
) -> None:
    """A non-404/non-409 error on STATUS.md logs a warning but does not raise."""
    store._state_sha = "old-sha"

    mock_client.put_file_content.side_effect = [
        {"content": {"sha": "new-sha"}},  # state.json succeeds
        RuntimeError("403 Forbidden"),  # STATUS.md forbidden — not 404, not 409
    ]

    with patch("merge_queue.store.render_status_md", return_value="# md\n"):
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
        RuntimeError("404 Not Found"),  # STATUS.md — 404 is ignored
    ]

    with patch("merge_queue.store.render_status_md", return_value="# md\n"):
        store.write(empty_state())  # must not raise

    assert mock_client.put_file_content.call_count == 2


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
