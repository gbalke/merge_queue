"""Render queue state as markdown and terminal output."""

from __future__ import annotations

from typing import Any


def render_status_md(state: dict, client: Any = None) -> str:
    """Render state dict as a markdown STATUS.md file."""
    lines = ["# Merge Queue Status", ""]

    owner_repo = ""
    if client and hasattr(client, "owner") and hasattr(client, "repo"):
        owner_repo = f"{client.owner}/{client.repo}"

    # Active batch
    batch = state.get("active_batch")
    if batch:
        lines.append("## Active Batch")
        lines.append(f"**Branch:** `{batch.get('branch', '?')}`")
        lines.append(f"**Status:** {batch.get('progress', 'unknown')}")
        lines.append(f"**Started:** {batch.get('started_at', '?')}")
        lines.append("")
        lines.append("| PR | Branch | Title |")
        lines.append("|----|--------|-------|")
        for pr in batch.get("stack", []):
            pr_link = f"#{pr['number']}"
            if owner_repo:
                pr_link = f"[#{pr['number']}](https://github.com/{owner_repo}/pull/{pr['number']})"
            lines.append(f"| {pr_link} | `{pr['head_ref']}` | {pr.get('title', '')} |")
        lines.append("")
    else:
        lines.append("## Active Batch")
        lines.append("_None — queue is idle._")
        lines.append("")

    # Queue
    queue = state.get("queue", [])
    if queue:
        lines.append(
            f"## Queue ({len(queue)} stack{'s' if len(queue) != 1 else ''} waiting)"
        )
        lines.append("")
        lines.append("| Position | PRs | Queued At |")
        lines.append("|----------|-----|-----------|")
        for entry in queue:
            prs = ", ".join(f"#{pr['number']}" for pr in entry.get("stack", []))
            queued = entry.get("queued_at", "?")
            if len(queued) > 19:
                queued = queued[:19]  # trim to YYYY-MM-DDTHH:MM:SS
            lines.append(f"| {entry.get('position', '?')} | {prs} | {queued} |")
        lines.append("")
    else:
        lines.append("## Queue")
        lines.append("_Empty — nothing waiting._")
        lines.append("")

    # History (last 10)
    history = state.get("history", [])
    if history:
        recent = history[-10:]
        lines.append(f"## Recent History (last {len(recent)})")
        lines.append("")
        lines.append("| Batch | PRs | Result | Duration |")
        lines.append("|-------|-----|--------|----------|")
        for entry in reversed(recent):
            prs = ", ".join(f"#{n}" for n in entry.get("prs", []))
            dur = entry.get("duration_seconds", 0)
            if dur >= 60:
                dur_str = f"{int(dur // 60)}m {int(dur % 60)}s"
            else:
                dur_str = f"{int(dur)}s"
            status = entry.get("status", "?")
            emoji = {"merged": "✅", "failed": "❌", "aborted": "⏹️"}.get(status, "")
            lines.append(
                f"| `{entry.get('batch_id', '?')}` | {prs} | {emoji} {status} | {dur_str} |"
            )
        lines.append("")

    # Footer
    updated = state.get("updated_at", "")
    lines.append(f"_Updated {updated or 'never'}_")

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
