"""PR comment templates for the merge queue.

All comments go through this module for consistency.
"""

from __future__ import annotations


def _mq_link(owner: str, repo: str) -> str:
    if owner and repo:
        return f"[View merge queue →](https://github.com/{owner}/{repo}/deployments/merge-queue)"
    return ""


def _pr_table(stack: list[dict], owner: str = "", repo: str = "") -> str:
    """Render a stack as a markdown table."""
    lines = ["| PR | Branch | Title |", "|:---|:-------|:------|"]
    for pr in stack:
        num = pr.get("number", "?")
        title = pr.get("title", "")
        head = pr.get("head_ref", "")
        if owner and repo:
            pr_link = f"[#{num}](https://github.com/{owner}/{repo}/pull/{num})"
        else:
            pr_link = f"#{num}"
        lines.append(f"| {pr_link} | `{head}` | {title} |")
    return "\n".join(lines)


def queued(
    position: int,
    total: int,
    stack: list[dict],
    owner: str = "",
    repo: str = "",
) -> str:
    """Comment when a PR is added to the queue."""
    link = _mq_link(owner, repo)
    table = _pr_table(stack, owner, repo)
    return (
        f"## 🚦 Merge Queue — Queued\n\n"
        f"**Position:** {position} of {total}\n\n"
        f"### Commits in this batch\n{table}\n\n"
        f"---\n{link}"
    )


def already_queued(position: int, owner: str = "", repo: str = "") -> str:
    link = _mq_link(owner, repo)
    return (
        f"## 🚦 Merge Queue — Already Queued\n\n"
        f"This PR is already in the queue at **position {position}**.\n\n"
        f"---\n{link}"
    )


def batch_started(
    branch: str,
    stack: list[dict],
    owner: str = "",
    repo: str = "",
) -> str:
    """Comment when CI starts on the batch branch."""
    link = _mq_link(owner, repo)
    table = _pr_table(stack, owner, repo)
    return (
        f"## 🔄 Merge Queue — CI Running\n\n"
        f"**Branch:** `{branch}`\n\n"
        f"### Commits in this batch\n{table}\n\n"
        f"---\n{link}"
    )


def merged(default_branch: str, owner: str = "", repo: str = "") -> str:
    link = _mq_link(owner, repo)
    return (
        f"## ✅ Merge Queue — Merged\n\n"
        f"Successfully merged to `{default_branch}`.\n\n"
        f"---\n{link}"
    )


def failed(
    reason: str,
    ci_run_url: str = "",
    owner: str = "",
    repo: str = "",
) -> str:
    link = _mq_link(owner, repo)
    ci_link = ""
    if ci_run_url:
        ci_link = f"\n\n**CI Run:** [View failed run →]({ci_run_url})"
    return (
        f"## ❌ Merge Queue — Failed\n\n"
        f"**Reason:** {reason}{ci_link}\n\n"
        f"Fix the issue and re-add the `queue` label to retry.\n\n"
        f"---\n{link}"
    )


def batch_error(
    error: str,
    owner: str = "",
    repo: str = "",
) -> str:
    link = _mq_link(owner, repo)
    return (
        f"## ❌ Merge Queue — Batch Creation Failed\n\n"
        f"**Error:** {error}\n\n"
        f"Fix the issue and re-add the `queue` label to retry.\n\n"
        f"---\n{link}"
    )


def aborted(owner: str = "", repo: str = "") -> str:
    link = _mq_link(owner, repo)
    return (
        f"## ⏹️ Merge Queue — Aborted\n\n"
        f"The `queue` label was removed. Branches unlocked.\n\n"
        f"---\n{link}"
    )


def removed_from_queue(owner: str = "", repo: str = "") -> str:
    link = _mq_link(owner, repo)
    return (
        f"## ⏹️ Merge Queue — Removed\n\n"
        f"PR was removed from the queue.\n\n"
        f"---\n{link}"
    )
