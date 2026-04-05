"""Additional tests for status.py — covering missing branches."""

from __future__ import annotations

from merge_queue.status import render_status_md, render_status_terminal
from merge_queue.types import empty_state


def test_render_status_md_queue_shows_pr_numbers() -> None:
    """Queue entries show PR numbers and titles via render_branch_status_md."""
    from merge_queue.status import render_branch_status_md

    branch_state = {
        "queue": [
            {
                "position": 1,
                "queued_at": "2026-04-04T12:34:56.789012+00:00",
                "stack": [
                    {
                        "number": 1,
                        "head_sha": "s",
                        "head_ref": "feat",
                        "base_ref": "main",
                        "title": "Add feature",
                    }
                ],
            }
        ],
        "active_batch": None,
    }
    md = render_branch_status_md("main", branch_state)
    assert "#1" in md
    assert "waiting" in md


def test_render_status_md_history_duration_over_60s() -> None:
    """History entries with duration >= 60s are formatted as Xm Ys."""
    state = {
        **empty_state(),
        "history": [
            {
                "batch_id": "b1",
                "status": "merged",
                "completed_at": "2026-04-04T01:00:00Z",
                "prs": [5],
                "duration_seconds": 125,
            }
        ],
    }
    md = render_status_md(state)
    assert "2m 5s" in md


def test_render_status_md_history_duration_under_60s() -> None:
    """History entries with duration < 60s are formatted as Xs."""
    state = {
        **empty_state(),
        "history": [
            {
                "batch_id": "b2",
                "status": "merged",
                "completed_at": "2026-04-04T01:00:00Z",
                "prs": [6],
                "duration_seconds": 45,
            }
        ],
    }
    md = render_status_md(state)
    assert "45s" in md


def test_render_status_terminal_no_active_batch_no_queue() -> None:
    """Empty state produces well-formed terminal output with no LAST line."""
    out = render_status_terminal(empty_state())
    assert "ACTIVE: none" in out
    assert "QUEUE:  empty" in out
    assert "LAST:" not in out
