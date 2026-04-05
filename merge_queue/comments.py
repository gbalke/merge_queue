"""PR comment templates for the merge queue.

Uses GitHub's native auto-linking (#123 -> PR link) instead of explicit
markdown links for a cleaner rendered appearance.
"""

from __future__ import annotations

import os


def _actions_link() -> str:
    """Return the Actions link from GITHUB_RUN_URL env var, or empty string."""
    return os.environ.get("GITHUB_RUN_URL", "")


def _footer(*links: str) -> str:
    """Render a compact footer line from non-empty link fragments."""
    parts = [link for link in links if link]
    if not parts:
        return ""
    return "\n\n" + " \u00b7 ".join(parts)


def _pr_table(stack: list[dict]) -> str:
    """Render stack as a compact PR table."""
    if not stack:
        return ""
    rows = []
    for pr in stack:
        num = pr.get("number", "?")
        title = pr.get("title", "")
        rows.append(f"| #{num} | {title} |")
    return "\n| PR | Title |\n|:---|:------|\n" + "\n".join(rows)


def _timing_table(timings: dict[str, str] | None, active_label: str = "") -> str:
    """Render a phase/duration timing table.

    timings: dict of phase name -> duration string
    active_label: if set, append an in-progress row with this label
    """
    if not timings and not active_label:
        return ""
    rows = []
    if timings:
        for name, dur in timings.items():
            rows.append(f"| {name} | {dur} |")
    if active_label:
        rows.append(f"| *{active_label}* | *...* |")
    return "\n| Phase | Duration |\n|:------|:---------|\n" + "\n".join(rows)


def _mq_link(owner: str, repo: str) -> str:
    if owner and repo:
        return f"[Queue](https://github.com/{owner}/{repo}/deployments/merge-queue)"
    return ""


def _actions_or_mq_footer(
    owner: str = "",
    repo: str = "",
    ci_run_url: str = "",
    ci_link_text: str = "CI run",
) -> str:
    """Build a standard footer with optional CI link, Actions link, and Queue link."""
    links: list[str] = []
    if ci_run_url:
        links.append(f"[{ci_link_text}]({ci_run_url})")
    actions = _actions_link()
    if actions:
        links.append(f"[Actions]({actions})")
    mq = _mq_link(owner, repo)
    if mq:
        links.append(mq)
    return _footer(*links)


# ---------------------------------------------------------------------------
# Comment templates
# ---------------------------------------------------------------------------


def queued(
    position: int,
    total: int,
    stack: list[dict],
    owner: str = "",
    repo: str = "",
) -> str:
    table = _pr_table(stack)
    return f"\U0001f6a6 **Queued** \u00b7 position {position}{table}{_actions_or_mq_footer(owner, repo)}"


def progress(
    phase: str,
    stack: list[dict],
    timings: dict[str, str] | None = None,
    ci_run_url: str = "",
    branch: str = "",
    owner: str = "",
    repo: str = "",
) -> str:
    """Single updating comment showing current phase with timing.

    phase: "queued", "locking", "running_ci", "completing", "merged", "failed", "aborted"
    timings: dict of phase name -> duration string
        e.g. {"Queued": "5s", "Lock + merge": "3s"}
    """
    phase_headers = {
        "queued": "\U0001f6a6 **Queued**",
        "locking": "\U0001f512 **Locking branches**",
        "running_ci": "\U0001f504 **CI running**",
        "completing": "\U0001f504 **Merging to main**",
        "merged": "\u2705 **Merged**",
        "failed": "\u274c **Failed**",
        "aborted": "\u23f9\ufe0f **Aborted**",
    }
    header = phase_headers.get(phase, phase)

    if branch and phase in ("running_ci", "completing"):
        header += f" on `{branch}`"

    active_label = ""
    if phase not in ("merged", "failed", "aborted"):
        active_phases = {
            "queued": "Queued",
            "locking": "Locking",
            "running_ci": "CI",
            "completing": "Merge",
        }
        active_label = active_phases.get(phase, "")

    timing = _timing_table(timings, active_label)
    table = _pr_table(stack)
    ci_text = "View CI run" if phase == "running_ci" else "CI run"
    footer = _actions_or_mq_footer(owner, repo, ci_run_url, ci_text)

    return f"{header}{timing}{table}{footer}"


def already_queued(position: int, owner: str = "", repo: str = "") -> str:
    return f"\U0001f6a6 **Already queued** \u00b7 position {position}{_actions_or_mq_footer(owner, repo)}"


def batch_started(
    branch: str,
    stack: list[dict],
    ci_run_url: str = "",
    owner: str = "",
    repo: str = "",
) -> str:
    table = _pr_table(stack)
    footer = _actions_or_mq_footer(owner, repo, ci_run_url, "View CI run")
    return f"\U0001f504 **CI running** on `{branch}`{table}{footer}"


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
    header = f"\u2705 **Merged** to `{default_branch}`"

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
                    f"| Queued | {_fmt_duration((t_started - t_queued).total_seconds())} |"
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
                            f"| Merge | {_fmt_duration((t_completed - t_ci_end).total_seconds())} |"
                        )
                    else:
                        rows.append(
                            f"| CI + merge | {_fmt_duration((t_completed - t_ci_start).total_seconds())} |"
                        )
            rows.append(f"| **Total** | **{_fmt_duration(total)}** |")
            stats = "\n\n| Phase | Duration |\n|:------|:---------|\n" + "\n".join(rows)
        except Exception:
            pass

    table = _pr_table(stack) if stack else ""
    footer = _actions_or_mq_footer(owner, repo, ci_run_url, "CI run")

    return f"{header}{stats}{table}{footer}"


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
    header = f"\u274c **Failed** \u2014 {reason}"
    details = ""
    if failed_job or failed_step:
        parts = []
        if failed_job:
            parts.append(f"**Job:** {failed_job}")
        if failed_step:
            parts.append(f"**Step:** {failed_step}")
        details = "\n\n> " + " \u00b7 ".join(parts)

    footer = _actions_or_mq_footer(owner, repo, ci_run_url, "View failed run")
    return f"{header}{details}{footer}"


def batch_error(error: str, owner: str = "", repo: str = "") -> str:
    return f"\u274c **Failed** \u2014 {error}{_actions_or_mq_footer(owner, repo)}"


def aborted(owner: str = "", repo: str = "") -> str:
    return f"\u23f9\ufe0f **Aborted** \u2014 `queue` label removed{_actions_or_mq_footer(owner, repo)}"


def removed_from_queue(owner: str = "", repo: str = "") -> str:
    return f"\u23f9\ufe0f **Removed** from queue{_actions_or_mq_footer(owner, repo)}"


def ci_not_ready(pr_number: int, owner: str = "", repo: str = "") -> str:
    footer = _actions_or_mq_footer(owner, repo)
    return (
        f"\u26a0\ufe0f **CI required** \u2014 #{pr_number} does not have passing CI\n\n"
        f"Add `re-test` to retrigger, or fix and re-add `queue`."
        f"{footer}"
    )


def ci_retriggered(owner: str = "", repo: str = "") -> str:
    return f"\U0001f504 **CI retriggered** via `re-test` label{_actions_or_mq_footer(owner, repo)}"
