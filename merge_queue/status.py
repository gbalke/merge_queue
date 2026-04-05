"""Render queue state as markdown and terminal output."""

from __future__ import annotations

from typing import Any

from merge_queue.comments import _fmt_duration


def render_status_md(state: dict, client: Any = None) -> str:
    """Render state as a clean queue-focused STATUS.md.

    Shows everything in the queue in order:
    1. Active batch (currently processing) at the top
    2. Waiting stacks below in FIFO order
    3. Empty message when idle
    """
    lines = ["# Merge Queue", ""]

    owner_repo = ""
    if client and hasattr(client, "owner") and hasattr(client, "repo"):
        owner_repo = f"{client.owner}/{client.repo}"

    batch = state.get("active_batch")
    queue = state.get("queue", [])

    has_items = batch or queue

    if has_items:
        # Single unified table showing everything in order
        lines.append("| # | PR | Title | Status |")
        lines.append("|:--|:---|:------|:-------|")

        pos = 1

        # Active batch first
        if batch:
            progress = batch.get("progress", "processing")
            status_label = {
                "locking": "🔒 locking",
                "running_ci": "🔄 CI running",
                "completing": "🔄 merging",
            }.get(progress, f"🔄 {progress}")

            for pr in batch.get("stack", []):
                pr_link = _pr_link(pr, owner_repo)
                lines.append(
                    f"| {pos} | {pr_link} | {pr.get('title', '')} | {status_label} |"
                )
            pos += 1

        # Waiting entries
        for entry in queue:
            for pr in entry.get("stack", []):
                pr_link = _pr_link(pr, owner_repo)
                lines.append(
                    f"| {pos} | {pr_link} | {pr.get('title', '')} | ⏳ waiting |"
                )
            pos += 1

        lines.append("")
    else:
        lines.append("_Queue is empty — all clear._")
        lines.append("")

    # Last completed
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


def _pr_link(pr: dict, owner_repo: str) -> str:
    num = pr.get("number", "?")
    if owner_repo:
        return f"[#{num}](https://github.com/{owner_repo}/pull/{num})"
    return f"#{num}"


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
