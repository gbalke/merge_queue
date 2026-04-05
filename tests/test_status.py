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
        assert "Merge Queue" in md

    def test_active_batch_shows_in_table(self):
        md = render_status_md(_state_with_active())
        assert "CI running" in md
        assert "#1" in md
        assert "#2" in md
        assert "Add feature A" in md

    def test_waiting_entries_show_in_table(self):
        md = render_status_md(_state_with_active())
        assert "waiting" in md
        assert "#3" in md
        assert "Fix X" in md

    def test_unified_table_has_all_prs(self):
        md = render_status_md(_state_with_active())
        # All 3 PRs should appear in the table (2 active + 1 waiting)
        assert "#1" in md
        assert "#2" in md
        assert "#3" in md
        assert "CI running" in md
        assert "waiting" in md

    def test_history_last_line(self):
        # v1 flat state — render_branch_status_md is used (no "Last:" line there)
        # Use v2 state with history to test history rendering via render_root_status_md
        from merge_queue.status import render_root_status_md

        state = {
            **empty_state(),
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
        md = render_root_status_md(state)
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
        # v1 flat state doesn't render updated_at via render_branch_status_md
        # Test timestamp via render_root_status_md with v2 state
        from merge_queue.status import render_root_status_md

        state = {**empty_state(), "updated_at": "2026-04-04T01:00:00Z"}
        md = render_root_status_md(state)
        assert "2026-04-04T01:00:00" in md

    def test_only_queue_no_active(self):
        # Use v2 state with a branch that has a queue entry
        from merge_queue.status import render_branch_status_md

        branch_state = {
            "queue": [{"position": 1, "stack": [{"number": 5, "title": "PR 5"}]}],
            "active_batch": None,
        }
        md = render_branch_status_md("main", branch_state)
        assert "waiting" in md
        assert "#5" in md
        assert "empty" not in md.lower()


class TestRenderStatusTerminal:
    def test_empty_state(self):
        # v2 empty state has no branches — falls through to legacy path
        out = render_status_terminal(empty_state())
        assert "ACTIVE: none" in out

    def test_active_batch(self):
        # v1 flat state still uses legacy path
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

    def test_v2_state_with_branches(self):
        state = {
            **empty_state(),
            "branches": {
                "main": {
                    "active_batch": {
                        "batch_id": "123",
                        "branch": "mq/main/123",
                        "progress": "running_ci",
                        "stack": [{"number": 7}],
                    },
                    "queue": [],
                }
            },
        }
        out = render_status_terminal(state)
        assert "ACTIVE [main]:" in out
        assert "#7" in out
        assert "running_ci" in out
