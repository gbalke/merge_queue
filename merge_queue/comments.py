"""PR comment templates for the merge queue.

Uses GitHub's native auto-linking (#123 → PR link) instead of explicit
markdown links for a cleaner rendered appearance.
"""

from __future__ import annotations


def _mq_link(owner: str, repo: str) -> str:
    if owner and repo:
        return f"\n---\n[View merge queue →](https://github.com/{owner}/{repo}/deployments/merge-queue)"
    return ""


def _stack_list(stack: list[dict]) -> str:
    """Render stack as a compact list with branch names and titles."""
    lines = []
    for pr in stack:
        num = pr.get("number", "?")
        title = pr.get("title", "")
        head = pr.get("head_ref", "")
        line = f"- #{num} `{head}`"
        if title:
            line += f" — {title}"
        lines.append(line)
    return "\n".join(lines)


def queued(
    position: int,
    total: int,
    stack: list[dict],
    owner: str = "",
    repo: str = "",
) -> str:
    link = _mq_link(owner, repo)
    stack_list = _stack_list(stack)
    return (
        f"**Merge Queue — Queued (position {position}/{total})**\n\n"
        f"Commits in this batch:\n{stack_list}"
        f"{link}"
    )


def already_queued(position: int, owner: str = "", repo: str = "") -> str:
    link = _mq_link(owner, repo)
    return f"**Merge Queue** — Already queued at position {position}.{link}"


def batch_started(
    branch: str,
    stack: list[dict],
    owner: str = "",
    repo: str = "",
) -> str:
    link = _mq_link(owner, repo)
    stack_list = _stack_list(stack)
    return (
        f"**Merge Queue — CI Running**\n\n"
        f"Branch: `{branch}`\n\n"
        f"Commits in this batch:\n{stack_list}"
        f"{link}"
    )


def merged(default_branch: str, owner: str = "", repo: str = "") -> str:
    link = _mq_link(owner, repo)
    return f"**Merge Queue — Merged** to `{default_branch}`.{link}"


def failed(
    reason: str,
    ci_run_url: str = "",
    owner: str = "",
    repo: str = "",
) -> str:
    link = _mq_link(owner, repo)
    ci_link = ""
    if ci_run_url:
        ci_link = f"\n\n[View failed CI run →]({ci_run_url})"
    return (
        f"**Merge Queue — Failed**\n\n"
        f"{reason}{ci_link}\n\n"
        f"Fix the issue and re-add the `queue` label to retry."
        f"{link}"
    )


def batch_error(error: str, owner: str = "", repo: str = "") -> str:
    link = _mq_link(owner, repo)
    return (
        f"**Merge Queue — Batch Creation Failed**\n\n"
        f"{error}\n\n"
        f"Fix the issue and re-add the `queue` label to retry."
        f"{link}"
    )


def aborted(owner: str = "", repo: str = "") -> str:
    link = _mq_link(owner, repo)
    return f"**Merge Queue — Aborted.** `queue` label was removed, branches unlocked.{link}"


def removed_from_queue(owner: str = "", repo: str = "") -> str:
    link = _mq_link(owner, repo)
    return f"**Merge Queue — Removed** from queue.{link}"
