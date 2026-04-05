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
    ci_run_url: str = "",
    owner: str = "",
    repo: str = "",
) -> str:
    link = _mq_link(owner, repo)
    stack_list = _stack_list(stack)
    ci_link = ""
    if ci_run_url:
        ci_link = f"\n\n[View CI run →]({ci_run_url})"
    return (
        f"**Merge Queue — CI Running**\n\n"
        f"Branch: `{branch}`\n\n"
        f"**Commits in this batch:**\n{stack_list}"
        f"{ci_link}{link}"
    )


def merged(
    default_branch: str,
    stack: list[dict] | None = None,
    queued_at: str = "",
    started_at: str = "",
    ci_started_at: str = "",
    ci_completed_at: str = "",
    completed_at: str = "",
    ci_run_url: str = "",
    owner: str = "",
    repo: str = "",
) -> str:
    link = _mq_link(owner, repo)
    stats = ""
    if queued_at and completed_at:
        try:
            from datetime import datetime

            t_queued = datetime.fromisoformat(queued_at)
            t_completed = datetime.fromisoformat(completed_at)
            total = (t_completed - t_queued).total_seconds()
            rows = []

            if started_at:
                t_started = datetime.fromisoformat(started_at)
                rows.append(
                    f"| Queue wait | {_fmt_duration((t_started - t_queued).total_seconds())} |"
                )

                if ci_started_at:
                    t_ci_start = datetime.fromisoformat(ci_started_at)
                    rows.append(
                        f"| Lock + merge | {_fmt_duration((t_ci_start - t_started).total_seconds())} |"
                    )

                    if ci_completed_at:
                        t_ci_end = datetime.fromisoformat(ci_completed_at)
                        rows.append(
                            f"| CI | {_fmt_duration((t_ci_end - t_ci_start).total_seconds())} |"
                        )
                        rows.append(
                            f"| Merge to {default_branch} | {_fmt_duration((t_completed - t_ci_end).total_seconds())} |"
                        )
                    else:
                        rows.append(
                            f"| CI + merge | {_fmt_duration((t_completed - t_ci_start).total_seconds())} |"
                        )
            rows.append(f"| **Total** | **{_fmt_duration(total)}** |")
            stats = "\n\n| Phase | Duration |\n|:------|:---------|\n" + "\n".join(rows)
        except Exception:
            pass

    stack_list = ""
    if stack:
        stack_list = "\n\n**Commits:**\n" + _stack_list(stack)

    ci_link = ""
    if ci_run_url:
        ci_link = f"\n\n[View CI run →]({ci_run_url})"

    return f"**Merge Queue — Merged** to `{default_branch}`.{stats}{stack_list}{ci_link}{link}"


def _fmt_duration(seconds: float) -> str:
    if seconds >= 60:
        return f"{int(seconds // 60)}m {int(seconds % 60)}s"
    return f"{int(seconds)}s"


def failed(
    reason: str,
    ci_run_url: str = "",
    failed_job: str = "",
    failed_step: str = "",
    owner: str = "",
    repo: str = "",
) -> str:
    link = _mq_link(owner, repo)
    details = ""
    if failed_job:
        details += f"\n**Job:** {failed_job}"
    if failed_step:
        details += f"\n**Step:** {failed_step}"
    ci_link = ""
    if ci_run_url:
        ci_link = f"\n\n[View failed CI run →]({ci_run_url})"
    return (
        f"**Merge Queue — Failed**\n\n"
        f"{reason}{details}{ci_link}\n\n"
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


def ci_not_ready(pr_number: int, owner: str = "", repo: str = "") -> str:
    link = _mq_link(owner, repo)
    return (
        f"**Merge Queue — CI Required**\n\n"
        f"PR #{pr_number} does not have passing CI. "
        f"Fix the issue and re-add `queue`, or add the `re-test` label to retrigger CI."
        f"{link}"
    )


def ci_retriggered(owner: str = "", repo: str = "") -> str:
    link = _mq_link(owner, repo)
    return f"**Merge Queue** — CI retriggered by `re-test` label.{link}"
