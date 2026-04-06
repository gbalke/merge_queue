"""Shared state utilities."""

from __future__ import annotations

from merge_queue.types import empty_branch_state


def get_branch_state(state: dict, branch: str) -> dict:
    """Get or create branch state dict."""
    return state.setdefault("branches", {}).setdefault(branch, empty_branch_state())
