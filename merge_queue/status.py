"""Render queue state as markdown and terminal output."""

from __future__ import annotations

from typing import Any

from merge_queue.comments import _fmt_duration


def render_status_md(state: dict, client: Any = None) -> str:
    """Render state as a clean queue-focused STATUS.md.

    Shows only what's currently happening — active batch and waiting stacks.
    No history clutter. This is the persistent queue page.
    """
    lines = ["# Merge Queue", ""]

    owner_repo = ""
    if client and hasattr(client, "owner") and hasattr(client, "repo"):
        owner_repo = f"{client.owner}/{client.repo}"

    # Active batch
    batch = state.get("active_batch")
    if batch:
        progress = batch.get("progress", "processing")
        progress_emoji = {
            "locking": "🔒",
            "running_ci": "🔄",
            "completing": "🔄",
        }.get(progress, "🔄")

        lines.append(f"{progress_emoji} **Now processing**")
        lines.append("")
        lines.append("| # | PR | Title | Status |")
        lines.append("|:--|:---|:------|:-------|")
        for i, pr in enumerate(batch.get("stack", []), 1):
            pr_link = f"#{pr['number']}"
            if owner_repo:
                pr_link = f"[#{pr['number']}](https://github.com/{owner_repo}/pull/{pr['number']})"
            lines.append(f"| {i} | {pr_link} | {pr.get('title', '')} | {progress} |")
        lines.append("")

    # Queue
    queue = state.get("queue", [])
    if queue:
        lines.append(f"**Waiting** ({len(queue)})")
        lines.append("")
        lines.append("| # | PR | Title |")
        lines.append("|:--|:---|:------|")
        pos = 1
        for entry in queue:
            for pr in entry.get("stack", []):
                pr_link = f"#{pr['number']}"
                if owner_repo:
                    pr_link = f"[#{pr['number']}](https://github.com/{owner_repo}/pull/{pr['number']})"
                lines.append(f"| {pos} | {pr_link} | {pr.get('title', '')} |")
            pos += 1
        lines.append("")

    # Empty state
    if not batch and not queue:
        lines.append("_Queue is empty._")
        lines.append("")

    # Last completed (just one line, not a full history)
    history = state.get("history", [])
    if history:
        last = history[-1]
        prs = ", ".join(f"#{n}" for n in last.get("prs", []))
        status = last.get("status", "?")
        dur = _fmt_duration(last.get("duration_seconds", 0))
        emoji = {"merged": "✅", "failed": "❌", "aborted": "⏹️"}.get(status, "")
        lines.append(f"Last: {emoji} {prs} {status} ({dur})")
        lines.append("")

    # Footer
    updated = state.get("updated_at", "")
    if updated and len(updated) > 19:
        updated = updated[:19]
    lines.append(f"<sub>Updated {updated or 'never'} UTC</sub>")

    return "\n".join(lines) + "\n"


def render_status_terminal(state: dict) -> str:
    """Render state as a compact terminal-friendly string."""
    lines = []

    batch = state.get("active_batch")
    if batch:
        prs = ", ".join(f"#{pr['number']}" for pr in batch.get("stack", []))
        lines.append(
            f"ACTIVE: {prs} [{batch.get('progress', '?')}] on {batch.get('branch', '?')}"
        )
    else:
        lines.append("ACTIVE: none")

    queue = state.get("queue", [])
    if queue:
        lines.append(f"QUEUE:  {len(queue)} stack(s) waiting")
        for entry in queue:
            prs = ", ".join(f"#{pr['number']}" for pr in entry.get("stack", []))
            lines.append(f"  #{entry.get('position', '?')}: {prs}")
    else:
        lines.append("QUEUE:  empty")

    history = state.get("history", [])
    if history:
        last = history[-1]
        prs = ", ".join(f"#{n}" for n in last.get("prs", []))
        lines.append(f"LAST:   {prs} → {last.get('status', '?')}")

    return "\n".join(lines)
