"""Tests for status.py — markdown and terminal rendering."""

from __future__ import annotations

from merge_queue.status import render_status_md, render_status_terminal
from merge_queue.types import empty_state


def _state_with_active():
    return {
        "version": 1,
        "updated_at": "2026-04-04T01:00:00Z",
        "queue": [
            {
                "position": 1,
                "queued_at": "2026-04-04T00:05:00Z",
                "stack": [
                    {
                        "number": 3,
                        "head_sha": "aaa",
                        "head_ref": "fix-x",
                        "base_ref": "main",
                        "title": "Fix X",
                    },
                ],
            }
        ],
        "active_batch": {
            "batch_id": "123",
            "branch": "mq/123",
            "ruleset_id": 42,
            "started_at": "2026-04-04T00:02:00Z",
            "progress": "running_ci",
            "stack": [
                {"number": 1, "head_ref": "feat-a", "title": "Add feature A"},
                {"number": 2, "head_ref": "feat-b", "title": "Add feature B"},
            ],
        },
        "history": [
            {
                "batch_id": "prev",
                "status": "merged",
                "completed_at": "2026-04-04T00:01:00Z",
                "prs": [10, 11],
                "duration_seconds": 123,
            }
        ],
    }


class TestRenderStatusMd:
    def test_empty_state(self):
        md = render_status_md(empty_state())
        assert "# Merge Queue" in md
        assert "empty" in md.lower()

    def test_active_batch(self):
        md = render_status_md(_state_with_active())
        assert "Now processing" in md
        assert "running_ci" in md
        assert "#1" in md
        assert "#2" in md
        assert "Add feature A" in md

    def test_queue_entries(self):
        md = render_status_md(_state_with_active())
        assert "Waiting" in md
        assert "#3" in md

    def test_history(self):
        md = render_status_md(_state_with_active())
        assert "merged" in md
        assert "#10" in md

    def test_pr_links_with_client(self):
        from unittest.mock import MagicMock

        client = MagicMock()
        client.owner = "testowner"
        client.repo = "testrepo"
        md = render_status_md(_state_with_active(), client)
        assert "https://github.com/testowner/testrepo/pull/1" in md

    def test_updated_timestamp(self):
        md = render_status_md(_state_with_active())
        assert "2026-04-04T01:00:00" in md


class TestRenderStatusTerminal:
    def test_empty_state(self):
        out = render_status_terminal(empty_state())
        assert "ACTIVE: none" in out
        assert "empty" in out.lower()

    def test_active_batch(self):
        out = render_status_terminal(_state_with_active())
        assert "ACTIVE:" in out
        assert "#1" in out
        assert "running_ci" in out

    def test_queue(self):
        out = render_status_terminal(_state_with_active())
        assert "QUEUE:" in out
        assert "#3" in out

    def test_history(self):
        out = render_status_terminal(_state_with_active())
        assert "LAST:" in out
        assert "merged" in out

    def test_no_history(self):
        state = empty_state()
        out = render_status_terminal(state)
        assert "LAST:" not in out
