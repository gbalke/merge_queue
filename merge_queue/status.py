"""Render queue state as markdown and terminal output."""

from __future__ import annotations

from typing import Any

from merge_queue.comments import _fmt_duration


def render_branch_status_md(
    branch_name: str, branch_state: dict, client: Any = None
) -> str:
    """Render a single branch's queue as a STATUS.md page.

    Shows everything for this branch in order:
    1. Active batch (currently processing) at the top
    2. Waiting stacks below in FIFO order
    3. Empty message when idle
    """
    lines = [f"# Merge Queue — `{branch_name}`", ""]

    owner_repo = ""
    if client and hasattr(client, "owner") and hasattr(client, "repo"):
        owner_repo = f"{client.owner}/{client.repo}"

    batch = branch_state.get("active_batch")
    queue = branch_state.get("queue", [])

    if batch or queue:
        lines.append("| # | PR | Title | Status | Queued |")
        lines.append("|:--|:---|:------|:-------|:------|")
        pos = 1
        if batch:
            progress = batch.get("progress", "processing")
            status_label = {
                "locking": "\U0001f512 locking",
                "running_ci": "\U0001f504 CI running",
                "completing": "\U0001f504 merging",
            }.get(progress, f"\U0001f504 {progress}")
            queued_cell = _relative_time(batch.get("queued_at"))
            for pr in batch.get("stack", []):
                pr_link = _pr_link(pr, owner_repo)
                lines.append(
                    f"| {pos} | {pr_link} | {pr.get('title', '')} "
                    f"| {status_label} | {queued_cell} |"
                )
            pos += 1
        for entry in queue:
            queued_cell = _relative_time(entry.get("queued_at"))
            for pr in entry.get("stack", []):
                pr_link = _pr_link(pr, owner_repo)
                lines.append(
                    f"| {pos} | {pr_link} | {pr.get('title', '')} "
                    f"| \u23f3 waiting | {queued_cell} |"
                )
            pos += 1
        lines.append("")
    else:
        lines.append("_Queue is empty — all clear._")
        lines.append("")

    return "\n".join(lines) + "\n"


def render_root_status_md(state: dict, client: Any = None) -> str:
    """Render the root STATUS.md linking to all per-branch status pages.

    Also shows recent history from the global history list.
    """
    lines = ["# Merge Queue Status", ""]

    owner_repo = ""
    if client and hasattr(client, "owner") and hasattr(client, "repo"):
        owner_repo = f"{client.owner}/{client.repo}"

    branches = state.get("branches", {})
    if branches:
        lines.append("## Branches")
        lines.append("")
        for branch_name in sorted(branches):
            branch_state = branches[branch_name]
            batch = branch_state.get("active_batch")
            queue_len = len(branch_state.get("queue", []))
            if batch:
                indicator = "\U0001f504 processing"
            elif queue_len:
                indicator = f"\u23f3 {queue_len} waiting"
            else:
                indicator = "\u2705 idle"
            if owner_repo:
                status_url = (
                    f"https://github.com/{owner_repo}/blob/mq/state"
                    f"/{branch_name}/STATUS.md"
                )
                lines.append(f"- [`{branch_name}`]({status_url}) — {indicator}")
            else:
                lines.append(f"- `{branch_name}` — {indicator}")
        lines.append("")

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

    updated = state.get("updated_at", "")
    if updated and len(updated) > 19:
        updated = updated[:19]
    lines.append(f"<sub>Updated {updated or 'never'} UTC</sub>")

    return "\n".join(lines) + "\n"


def render_status_md(state: dict, client: Any = None) -> str:
    """Render state as a STATUS.md.

    For v2 state (has 'branches' key), renders the root status page.
    For legacy flat state passed directly, renders a flat single-branch view.
    """
    if "branches" in state:
        return render_root_status_md(state, client)
    return render_branch_status_md("main", state, client)


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

    branches = state.get("branches")
    if branches:
        for branch_name, branch_state in branches.items():
            batch = branch_state.get("active_batch")
            queue = branch_state.get("queue", [])
            if batch:
                prs = ", ".join(f"#{pr['number']}" for pr in batch.get("stack", []))
                lines.append(
                    f"ACTIVE [{branch_name}]: {prs} [{batch.get('progress', '?')}]"
                    f" on {batch.get('branch', '?')}"
                )
            else:
                lines.append(f"ACTIVE [{branch_name}]: none")
            if queue:
                lines.append(f"QUEUE  [{branch_name}]: {len(queue)} stack(s) waiting")
                for entry in queue:
                    prs = ", ".join(f"#{pr['number']}" for pr in entry.get("stack", []))
                    lines.append(f"  #{entry.get('position', '?')}: {prs}")
            else:
                lines.append(f"QUEUE  [{branch_name}]: empty")
    else:
        # Legacy v1 flat state
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
