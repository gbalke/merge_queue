"""Render queue state as markdown and terminal output."""

from __future__ import annotations

from typing import Any

from merge_queue.comments import _fmt_duration


def render_status_md(state: dict, client: Any = None) -> str:
    """Render state as a clean queue-focused STATUS.md grouped by target branch.

    Groups items by target_branch. Within each branch:
    1. Active batch (currently processing) at the top
    2. Waiting stacks below in FIFO order

    Uses GitHub's <relative-time> custom element for queue times so they
    auto-update in the browser.
    """
    lines = ["# Merge Queue", ""]

    owner_repo = ""
    if client and hasattr(client, "owner") and hasattr(client, "repo"):
        owner_repo = f"{client.owner}/{client.repo}"

    batch = state.get("active_batch")
    queue = state.get("queue", [])

    # Collect all items grouped by target_branch.
    # Each item is a dict with keys: pr_dicts, status_label, queued_at (ISO str | None)
    branch_items: dict[str, list[dict]] = {}

    if batch:
        target = batch.get("target_branch") or "main"
        progress = batch.get("progress", "processing")
        status_label = {
            "locking": "\U0001f512 locking",
            "running_ci": "\U0001f504 CI running",
            "completing": "\U0001f504 merging",
        }.get(progress, f"\U0001f504 {progress}")
        branch_items.setdefault(target, []).append(
            {
                "pr_dicts": batch.get("stack", []),
                "status_label": status_label,
                "queued_at": batch.get("queued_at"),
            }
        )

    for entry in queue:
        target = entry.get("target_branch") or "main"
        branch_items.setdefault(target, []).append(
            {
                "pr_dicts": entry.get("stack", []),
                "status_label": "\u23f3 waiting",
                "queued_at": entry.get("queued_at"),
            }
        )

    if branch_items:
        for branch, items in branch_items.items():
            lines.append(f"## {branch}")
            lines.append("")
            lines.append("| # | PR | Title | Status | Queued |")
            lines.append("|:--|:---|:------|:-------|:------|")

            pos = 1
            for item in items:
                queued_cell = _relative_time(item.get("queued_at"))
                for pr in item["pr_dicts"]:
                    pr_link = _pr_link(pr, owner_repo)
                    lines.append(
                        f"| {pos} | {pr_link} | {pr.get('title', '')} "
                        f"| {item['status_label']} | {queued_cell} |"
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
        emoji = {"merged": "\u2705", "failed": "\u274c", "aborted": "\u23f9\ufe0f"}.get(
            status, ""
        )
        lines.append(f"Last: {emoji} {prs} {status} ({dur})")
        lines.append("")

    # Footer
    updated = state.get("updated_at", "")
    if updated and len(updated) > 19:
        updated = updated[:19]
    lines.append(f"<sub>Updated {updated or 'never'} UTC</sub>")

    return "\n".join(lines) + "\n"


def _relative_time(iso: str | None) -> str:
    """Render an ISO timestamp as a GitHub relative-time element.

    Returns an empty string when no timestamp is available.
    """
    if not iso:
        return ""
    # Normalise to a clean ISO-8601 UTC string for the datetime attribute
    clean = iso.replace("Z", "+00:00")
    try:
        from datetime import datetime

        dt = datetime.fromisoformat(clean)
        # Always emit in the canonical form GitHub expects
        attr = dt.strftime("%Y-%m-%dT%H:%M:%SZ").replace("+00:00Z", "Z")
        if not attr.endswith("Z"):
            attr = dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        attr = iso
    return f'<relative-time datetime="{attr}">{attr}</relative-time>'


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
