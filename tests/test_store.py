"""Tests for store.py — state read/write on mq/state branch."""

from __future__ import annotations

import base64
import json
from unittest.mock import patch

import pytest

from merge_queue.store import ConflictError, StateStore
from merge_queue.types import empty_state


@pytest.fixture
def store(mock_client):
    return StateStore(mock_client)


class TestRead:
    def test_returns_state_from_branch(self, store, mock_client):
        # v1 state is auto-migrated to v2 on read
        state = {
            "version": 1,
            "queue": [{"position": 1}],
            "active_batch": None,
            "history": [],
        }
        encoded = base64.b64encode(json.dumps(state).encode()).decode()
        mock_client.get_file_content.return_value = {"sha": "abc", "content": encoded}
        mock_client.get_default_branch.return_value = "main"

        result = store.read()

        # Migrated to v2 per-branch schema
        assert result["version"] == 2
        assert result["branches"]["main"]["queue"][0]["position"] == 1
        mock_client.get_file_content.assert_called_once_with("state.json", "mq/state")

    def test_returns_empty_state_on_404(self, store, mock_client):
        mock_client.get_file_content.side_effect = RuntimeError("404 Not Found")

        result = store.read()

        assert result == empty_state()

    def test_caches_file_sha(self, store, mock_client):
        encoded = base64.b64encode(json.dumps(empty_state()).encode()).decode()
        mock_client.get_file_content.return_value = {
            "sha": "abc123",
            "content": encoded,
        }

        store.read()

        assert store._state_sha == "abc123"


class TestWrite:
    def test_writes_state_and_status(self, store, mock_client):
        store._state_sha = "old-sha"
        mock_client.put_file_content.return_value = {"content": {"sha": "new-sha"}}

        state = empty_state()
        state["updated_at"] = "2026-04-04T00:00:00Z"

        with (
            patch(
                "merge_queue.store.render_branch_status_md", return_value="# Branch\n"
            ),
            patch("merge_queue.store.render_root_status_md", return_value="# Root\n"),
        ):
            store.write(state)

        # v2 empty state has no branches, so only state.json + root STATUS.md
        assert mock_client.put_file_content.call_count == 2
        call1 = mock_client.put_file_content.call_args_list[0]
        assert call1[0][0] == "state.json"
        assert call1[0][1] == "mq/state"
        assert call1[1]["sha"] == "old-sha"
        call2 = mock_client.put_file_content.call_args_list[1]
        assert call2[0][0] == "STATUS.md"

    def test_conflict_raises(self, store, mock_client):
        store._state_sha = "old-sha"
        mock_client.put_file_content.side_effect = RuntimeError("409 Conflict")

        with patch("merge_queue.store.time.sleep"), pytest.raises(ConflictError):
            store.write(empty_state())

    def test_ensures_branch_first(self, store, mock_client):
        mock_client.get_file_content.side_effect = [
            RuntimeError("404"),  # _ensure_branch check
        ]
        mock_client.put_file_content.return_value = {"content": {"sha": "new"}}

        with (
            patch(
                "merge_queue.store.render_branch_status_md", return_value="# Branch\n"
            ),
            patch("merge_queue.store.render_root_status_md", return_value="# Root\n"),
        ):
            store.write(empty_state())

        mock_client.create_orphan_branch.assert_called_once()

    def test_status_md_failure_does_not_block(self, store, mock_client):
        store._state_sha = "old-sha"
        mock_client.put_file_content.side_effect = [
            {"content": {"sha": "new"}},  # state.json succeeds
            RuntimeError("500 Server Error"),  # root STATUS.md fails
        ]

        with (
            patch(
                "merge_queue.store.render_branch_status_md", return_value="# Branch\n"
            ),
            patch("merge_queue.store.render_root_status_md", return_value="# Root\n"),
        ):
            store.write(empty_state())  # Should not raise


class TestEnsureBranch:
    def test_noop_if_exists(self, store, mock_client):
        mock_client.get_file_content.return_value = {"sha": "abc", "content": "e30="}

        store._ensure_branch()

        mock_client.create_orphan_branch.assert_not_called()

    def test_creates_if_missing(self, store, mock_client):
        mock_client.get_file_content.side_effect = RuntimeError("404")

        store._ensure_branch()

        mock_client.create_orphan_branch.assert_called_once()
        call_args = mock_client.create_orphan_branch.call_args
        assert call_args[0][0] == "mq/state"
        files = call_args[0][1]
        assert "state.json" in files
        assert "STATUS.md" in files

    def test_race_condition_handled(self, store, mock_client):
        mock_client.get_file_content.side_effect = RuntimeError("404")
        mock_client.create_orphan_branch.side_effect = RuntimeError(
            "422 already exists"
        )

        store._ensure_branch()  # Should not raise
