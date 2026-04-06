"""Shared formatting utilities."""

from __future__ import annotations


def fmt_duration(seconds: float) -> str:
    """Format a duration in seconds as a human-readable string."""
    if seconds >= 60:
        return f"{int(seconds // 60)}m {int(seconds % 60)}s"
    return f"{int(seconds)}s"
