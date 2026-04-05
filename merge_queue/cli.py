"""CLI entry point for the merge queue."""

from __future__ import annotations

import argparse
import datetime
import logging
import os
import sys

from merge_queue import batch as batch_mod
from merge_queue import comments
from merge_queue import rules as rules_mod
from merge_queue.github_client import GitHubClient, GitHubClientProtocol
from merge_queue.queue import detect_stacks
from merge_queue.state import QueueState
from merge_queue.status import render_status_md, render_status_terminal
from merge_queue.store import StateStore
from merge_queue.types import empty_branch_state

log = logging.getLogger("merge_queue")


def _make_client() -> GitHubClient:
    repo_full = os.environ.get("GITHUB_REPOSITORY", "")
    if "/" in repo_full:
        owner, repo = repo_full.split("/", 1)
    else:
        owner = os.environ.get("GITHUB_OWNER", "")
        repo = repo_full or os.environ.get("GITHUB_REPO", "")
    if not owner or not repo:
        sys.exit("Set GITHUB_REPOSITORY=owner/repo or GITHUB_OWNER + GITHUB_REPO")
    return GitHubClient(owner, repo)


def _owner_repo(client: GitHubClientProtocol) -> tuple[str, str]:
    return getattr(client, "owner", ""), getattr(client, "repo", "")


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _event_time_or_now() -> str:
    """Return the GitHub event timestamp if available, otherwise now.

    GITHUB_EVENT_TIME is set from github.event.pull_request.updated_at in the
    workflow, which reflects when the label was added — a more accurate
    queued_at for PRs that waited in the concurrency queue before do_enqueue ran.
    """
    event_time = os.environ.get("GITHUB_EVENT_TIME", "")
    return event_time if event_time else _now_iso()


def _fmt_duration(seconds: float) -> str:
    if seconds >= 60:
        return f"{int(seconds // 60)}m {int(seconds % 60)}s"
    return f"{int(seconds)}s"


def _comment(
    client, pr_number: int, body: str, comment_ids: dict | None = None
) -> int | None:
    """Create or update a comment on a PR.

    If comment_ids is provided and has an entry for pr_number, updates that comment.
    Otherwise creates a new one. Returns the comment ID.
    """
    try:
        existing = (comment_ids or {}).get(pr_number) or (comment_ids or {}).get(
            str(pr_number)
        )
        if existing:
            client.update_comment(existing, body)
            return existing
        else:
            return client.create_comment(pr_number, body)
    except Exception as e:
        log.warning("Could not comment on PR #%d: %s", pr_number, e)
        return None


def _normalize_cids(cids: dict | None) -> dict[int, int]:
    """Normalize comment_ids keys to int (JSON deserializes them as strings)."""
    if not cids:
        return {}
    return {int(k): v for k, v in cids.items()}


def _update_deployment(
    client: GitHubClientProtocol,
    dep_id: int | None,
    state: str,
    description: str = "",
) -> None:
    """Update deployment status, ignoring errors."""
    if dep_id:
        try:
            client.update_deployment_status(dep_id, state, description)
        except Exception:
            pass


def _clear_active_batch(
    state: dict, store: StateStore, target_branch: str = ""
) -> None:
    """Clear active_batch for target_branch from state with retry on conflict."""
    for attempt in range(3):
        try:
            if target_branch:
                state.setdefault("branches", {}).setdefault(
                    target_branch, empty_branch_state()
                )["active_batch"] = None
            else:
                # Fallback: clear first branch with an active_batch
                for branch_state in state.get("branches", {}).values():
                    if branch_state.get("active_batch") is not None:
                        branch_state["active_batch"] = None
                        break
            state["updated_at"] = _now_iso()
            store.write(state)
            return
        except Exception as e:
            log.warning("Failed to clear active_batch (attempt %d): %s", attempt + 1, e)
            try:
                state = store.read()
            except Exception:
                pass
    log.error("Could not clear active_batch after 3 attempts")


def _resume_completion(
    client: GitHubClientProtocol,
    state: dict,
    store: StateStore,
    branch_name: str,
    active: dict,
    owner: str,
    repo: str,
) -> None:
    """Resume a batch stuck in 'completing' state.

    A previous run set progress='completing' and started complete_batch() but
    was cancelled (e.g. by the concurrency group) before it finished.  We
    reconstruct the Batch and retry the merge.
    """
    from merge_queue.types import Batch, PullRequest, Stack

    prs = tuple(
        PullRequest(
            number=pr["number"],
            head_sha=pr["head_sha"],
            head_ref=pr["head_ref"],
            base_ref=pr.get("base_ref", branch_name),
            labels=("queue",),
        )
        for pr in active.get("stack", [])
    )
    started_at = active.get("started_at", "")
    try:
        queued_at = datetime.datetime.fromisoformat(active.get("queued_at", started_at))
    except (ValueError, TypeError):
        queued_at = datetime.datetime.now(datetime.timezone.utc)
    batch = Batch(
        batch_id=active["batch_id"],
        branch=active["branch"],
        stack=Stack(prs=prs, queued_at=queued_at),
        ruleset_id=active.get("ruleset_id"),
    )
    target = active.get("target_branch", branch_name)
    entry = active
    cids = entry.get("comment_ids", {})
    dep_id = entry.get("deployment_id")

    try:
        batch_mod.complete_batch(client, batch, target_branch=target)
        log.info("Resumed completion succeeded — batch merged to %s", target)
        _update_deployment(client, dep_id, "success", f"Merged to {target}")
        for pr in prs:
            _comment(
                client,
                pr.number,
                comments.merged(
                    target,
                    stack=entry.get("stack"),
                    queued_at=entry.get("queued_at", ""),
                    started_at=entry.get("started_at", ""),
                    ci_started_at=entry.get("ci_started_at", ""),
                    completed_at=_now_iso(),
                    owner=owner,
                    repo=repo,
                ),
                cids,
            )
        # Record in history
        completed_at = _now_iso()
        state.setdefault("history", []).append(
            {
                "batch_id": active["batch_id"],
                "status": "merged",
                "completed_at": completed_at,
                "prs": [pr.number for pr in prs],
                "target_branch": target,
            }
        )
    except Exception as e:
        log.error("Resumed completion failed: %s", e)
        try:
            batch_mod.fail_batch(client, batch, str(e))
        except Exception:
            pass
        _update_deployment(client, dep_id, "failure", str(e)[:140])
        state.setdefault("history", []).append(
            {
                "batch_id": active["batch_id"],
                "status": "complete_error",
                "completed_at": _now_iso(),
                "prs": [pr.number for pr in prs],
                "target_branch": target,
            }
        )
    finally:
        _clear_active_batch(state, store, branch_name)


def _is_break_glass_authorized(client: GitHubClientProtocol, sender: str) -> bool:
    """Return True if sender is allowed to use the break-glass label.

    Checks in order:
    1. sender must be non-empty
    2. Explicit allow list from merge-queue.yml (break_glass_users)
    3. Repo admin or maintain permission via GitHub collaborator API
    """
    if not sender:
        return False
    from merge_queue.config import get_break_glass_users

    allowed = get_break_glass_users(client)
    if sender in allowed:
        return True
    try:
        perm = client.get_user_permission(sender)
        return perm in ("admin", "maintain")
    except Exception:
        return False


def _matches_protected(
    files: list[str], patterns: list[str] | list[dict]
) -> list[dict]:
    """Return list of matched protected path dicts touched by the PR's files.

    ``patterns`` may be a list of plain strings (legacy) or a list of dicts
    with at least a ``"path"`` key (new format).  Always returns a list of
    dicts so callers have uniform access to per-path approvers.
    """
    matched: list[dict] = []
    for entry in patterns:
        if isinstance(entry, str):
            pattern = entry
            approvers: list[str] = []
        else:
            pattern = entry["path"]
            approvers = entry.get("approvers", [])

        for f in files:
            # Directory pattern (ends with /): any file under it matches
            if pattern.endswith("/") and f.startswith(pattern):
                matched.append({"path": pattern, "approvers": approvers})
                break
            # Exact file match
            elif f == pattern:
                matched.append({"path": pattern, "approvers": approvers})
                break
    return matched


def _has_authorized_approval(
    client: GitHubClientProtocol,
    pr_number: int,
    path_approvers: list[str] | None = None,
) -> bool:
    """Check if an authorized user has approved the PR.

    If ``path_approvers`` is provided and non-empty, those users are checked
    first (in addition to admins/maintain-permission users).  When
    ``path_approvers`` is empty or ``None``, falls back to the global
    ``break_glass_users`` list plus admins.
    """
    from merge_queue import config as config_mod

    reviews = client.get_pr_reviews(pr_number)

    # Build allowed set: path-specific approvers or global break_glass_users
    if path_approvers:
        allowed = set(path_approvers)
    else:
        allowed = set(config_mod.get_break_glass_users(client))

    # Reduce to latest review state per user
    latest: dict[str, str] = {}
    for r in reviews:
        latest[r["user"]] = r["state"]

    for user, state in latest.items():
        if state == "APPROVED":
            if user in allowed:
                return True
            try:
                perm = client.get_user_permission(user)
                if perm in ("admin", "maintain"):
                    return True
            except Exception:
                pass
    return False


# --- Core logic ---


def _sync_missing_prs(client, state, store, open_prs: list[dict] | None = None):
    """Scan for PRs with 'queue' label not in state.json, auto-enqueue them.

    Args:
        client: GitHub client (used to fetch open PRs when open_prs is not supplied).
        state: current queue state dict.
        store: state store used to persist changes.
        open_prs: optional pre-fetched list of open PR dicts; avoids an API call
            when the caller has already fetched this list (e.g. do_process).
    """
    from merge_queue import config

    owner, repo = _owner_repo(client)

    # Collect known PR numbers across all branches
    known: set[int] = set()
    for branch_state in state.get("branches", {}).values():
        for entry in branch_state.get("queue", []):
            for pr in entry.get("stack", []):
                known.add(pr["number"])
        active = branch_state.get("active_batch")
        if active:
            for pr in active.get("stack", []):
                known.add(pr["number"])

    all_prs = open_prs if open_prs is not None else client.list_open_prs()
    missing = [
        pr
        for pr in all_prs
        if any(lbl["name"] == "queue" for lbl in pr.get("labels", []))
        and pr["number"] not in known
    ]

    if not missing:
        return state

    log.info(
        "Auto-enqueuing %d missing PRs: %s",
        len(missing),
        [p["number"] for p in missing],
    )

    target_branches = config.get_target_branches(client)

    for pr_data in missing:
        base = pr_data["base"]["ref"]
        target_branch = base if base in target_branches else client.get_default_branch()

        branch_state = state.setdefault("branches", {}).setdefault(
            target_branch, empty_branch_state()
        )
        position = len(branch_state.get("queue", [])) + 1
        stack_dicts = [
            {
                "number": pr_data["number"],
                "head_sha": pr_data["head"]["sha"],
                "head_ref": pr_data["head"]["ref"],
                "base_ref": pr_data["base"]["ref"],
                "title": pr_data.get("title", ""),
            }
        ]

        entry = {
            "position": position,
            "queued_at": _now_iso(),
            "stack": stack_dicts,
            "deployment_id": None,
            "comment_ids": {},
            "target_branch": target_branch,
        }

        try:
            dep_id = client.create_deployment(
                f"Queue position {position}: #{pr_data['number']}"
            )
            client.update_deployment_status(dep_id, "queued", f"Position {position}")
            entry["deployment_id"] = dep_id
        except Exception:
            pass

        cid = _comment(
            client,
            pr_data["number"],
            comments.progress(
                "queued",
                stack_dicts,
                target_branch=target_branch,
                owner=owner,
                repo=repo,
            ),
        )
        if cid:
            entry["comment_ids"] = {pr_data["number"]: cid}

        branch_state.setdefault("queue", []).append(entry)
        log.info("Auto-enqueued PR #%d at position %d", pr_data["number"], position)

    state["updated_at"] = _now_iso()
    store.write(state)
    return state


def _cleanup_stale_entries(client, state, store, open_prs: list[dict] | None = None):
    """Remove queue entries for PRs that no longer have the queue label.

    Args:
        client: GitHub client (used to fetch open PRs when open_prs is not supplied).
        state: current queue state dict.
        store: state store used to persist changes.
        open_prs: optional pre-fetched list of open PR dicts; avoids a second API
            call when the caller has already fetched this list (e.g. do_process).
    """
    all_prs = open_prs if open_prs is not None else client.list_open_prs()
    # Build set of PR numbers that have queue label
    queued_pr_numbers: set[int] = set()
    for pr_data in all_prs:
        labels = [lbl["name"] for lbl in pr_data.get("labels", [])]
        if "queue" in labels:
            queued_pr_numbers.add(pr_data["number"])

    changed = False
    for branch_name, branch_state in state.get("branches", {}).items():
        queue = branch_state.get("queue", [])
        original_len = len(queue)
        branch_state["queue"] = [
            entry
            for entry in queue
            if any(pr["number"] in queued_pr_numbers for pr in entry.get("stack", []))
        ]
        removed = original_len - len(branch_state["queue"])
        if removed:
            log.info("Removed %d stale entries from %s queue", removed, branch_name)
            changed = True

    if changed:
        # Re-number positions
        for branch_state in state.get("branches", {}).values():
            for i, entry in enumerate(branch_state.get("queue", [])):
                entry["position"] = i + 1
        state["updated_at"] = _now_iso()
        store.write(state)

    return state


def do_enqueue(client: GitHubClientProtocol, pr_number: int) -> str:
    """Enqueue a PR: update state, create deployment, trigger processing."""
    # Guard: skip if PR is already merged or closed.
    # Cache pr_data so we can reuse it below without a second get_pr call.
    cached_pr_data: dict | None = None
    try:
        cached_pr_data = client.get_pr(pr_number)
        if cached_pr_data.get("state") != "open":
            log.info(
                "PR #%d is %s, skipping enqueue", pr_number, cached_pr_data.get("state")
            )
            return "pr_not_open"
    except Exception:
        pass

    store = StateStore(client)
    state = store.read()
    owner, repo = _owner_repo(client)

    # Guard: already in active batch (any branch)?
    for branch_state in state.get("branches", {}).values():
        active = branch_state.get("active_batch")
        if active and any(pr["number"] == pr_number for pr in active.get("stack", [])):
            log.info("PR #%d is already in the active batch, skipping", pr_number)
            return "already_active"

    # Guard: already queued (any branch)?
    for branch_state in state.get("branches", {}).values():
        for entry in branch_state.get("queue", []):
            if any(pr["number"] == pr_number for pr in entry.get("stack", [])):
                log.info(
                    "PR #%d is already queued at position %d",
                    pr_number,
                    entry.get("position", 0),
                )
                return "already_queued"

    # Guard: recently merged? (skip if successfully merged in last 5 minutes)
    now = datetime.datetime.now(datetime.timezone.utc)
    for h in reversed(state.get("history", [])):
        if pr_number in h.get("prs", []) and h.get("status") == "merged":
            try:
                completed = datetime.datetime.fromisoformat(h["completed_at"])
                if (now - completed).total_seconds() < 300:
                    log.info(
                        "PR #%d was merged %ds ago, skipping duplicate",
                        pr_number,
                        int((now - completed).total_seconds()),
                    )
                    return "recently_processed"
            except Exception:
                pass
            break

    # Detect the stack and which target branch it roots at
    from merge_queue import config as config_mod

    api_state = QueueState.fetch(client)
    target_branches = config_mod.get_target_branches(client)

    # Verify PR targets a configured MQ branch; reject early if not.
    pr_data_for_check = cached_pr_data or client.get_pr(pr_number)
    cached_pr_data = pr_data_for_check  # avoid duplicate fetch below
    pr_target_ref = (pr_data_for_check.get("base") or {}).get(
        "ref", client.get_default_branch()
    )

    # Walk the base_ref chain through open PRs to find the ultimate target branch.
    # This handles stacked PRs where a PR targets another PR's head branch.
    resolved_target = pr_target_ref
    if resolved_target not in target_branches:
        all_prs = client.list_open_prs()
        head_to_base = {p["head"]["ref"]: p["base"]["ref"] for p in all_prs}
        visited: set[str] = set()
        cursor = resolved_target
        while (
            cursor not in target_branches
            and cursor in head_to_base
            and cursor not in visited
        ):
            visited.add(cursor)
            cursor = head_to_base[cursor]
        if cursor in target_branches:
            resolved_target = cursor
            log.info(
                "PR #%d targets %s via stack chain through %s",
                pr_number,
                cursor,
                pr_target_ref,
            )

    if resolved_target not in target_branches:
        _comment(
            client,
            pr_number,
            f"\u26a0\ufe0f **Not a merge queue target** \u2014 `{pr_target_ref}` is not "
            f"configured. Configured branches: "
            f"{', '.join(f'`{b}`' for b in target_branches)}",
        )
        try:
            client.remove_label(pr_number, "queue")
        except Exception:
            pass
        return "invalid_target"

    stack_dicts = None
    target_branch: str | None = None
    for pr in api_state.prs:
        if pr.number == pr_number:
            for target in target_branches:
                stacks = detect_stacks(api_state.queued_prs, target)
                for s in stacks:
                    if any(p.number == pr_number for p in s.prs):
                        stack_dicts = _stack_to_dicts(s, client)
                        target_branch = target
                        break
                if stack_dicts is not None:
                    break
            break

    if stack_dicts is None:
        # Reuse the pr_data fetched during the guard check — avoids a duplicate API call.
        pr_data = cached_pr_data or client.get_pr(pr_number)
        pr_base_ref = pr_data["base"]["ref"]
        stack_dicts = [
            {
                "number": pr_number,
                "head_sha": pr_data["head"]["sha"],
                "head_ref": pr_data["head"]["ref"],
                "base_ref": pr_base_ref,
                "title": pr_data.get("title", ""),
            }
        ]
        # Use resolved_target (which walks the stacked-PR chain) as the target branch,
        # falling back to the first configured target branch only as a last resort.
        if target_branch is None:
            target_branch = (
                resolved_target
                if resolved_target in target_branches
                else target_branches[0]
            )

    # CI gate: all PRs in stack must have passing CI (unless break-glass)
    pr_labels = [lbl["name"] for lbl in (cached_pr_data or {}).get("labels", [])]
    has_break_glass = "break-glass" in pr_labels
    if has_break_glass:
        sender = os.environ.get("MQ_SENDER", "")
        authorized = _is_break_glass_authorized(client, sender)
        if authorized:
            log.warning(
                "break-glass by %s on PR #%d — bypassing CI gate", sender, pr_number
            )
        else:
            log.warning("break-glass rejected: %s is not authorized", sender)
            cids_early: dict[int, int] = {}
            _comment(
                client,
                pr_number,
                comments.break_glass_denied(sender, owner, repo),
                cids_early,
            )
            try:
                client.remove_label(pr_number, "break-glass")
            except Exception:
                pass
            has_break_glass = False  # Fall through to normal CI gate

    if not has_break_glass:
        for pr_dict in stack_dicts:
            ci_passed, _ci_url = client.get_pr_ci_status(pr_dict["number"])
            # None = pending (CI hasn't completed yet) — allow through
            # True = passed — allow through
            # False = explicitly failed — reject
            if ci_passed is False:
                _comment(
                    client,
                    pr_dict["number"],
                    comments.ci_not_ready(pr_dict["number"], owner, repo),
                )
                try:
                    client.remove_label(pr_number, "queue")
                except Exception:
                    pass
                return "ci_not_ready"

    # Protected paths check
    protected_paths = config_mod.get_protected_paths(client)
    if protected_paths:
        pr_files = client.get_pr_files(pr_number)
        touched_protected = _matches_protected(pr_files, protected_paths)
        if touched_protected:
            # Each touched entry may have its own approvers list; check all
            approved = all(
                _has_authorized_approval(
                    client, pr_number, path_approvers=entry["approvers"]
                )
                for entry in touched_protected
            )
            if not approved:
                _comment(
                    client,
                    pr_number,
                    comments.protected_path_approval_required(
                        touched_protected, owner, repo
                    ),
                )
                try:
                    client.remove_label(pr_number, "queue")
                except Exception:
                    pass
                return "approval_required"

    resolved_target = target_branch or api_state.default_branch
    branch_state = state.setdefault("branches", {}).setdefault(
        resolved_target, empty_branch_state()
    )
    position = len(branch_state.get("queue", [])) + 1
    entry = {
        "position": position,
        "queued_at": _event_time_or_now(),
        "stack": stack_dicts,
        "deployment_id": None,
        "target_branch": resolved_target,
    }

    # Create deployment
    try:
        prs_desc = ", ".join(f"#{pr['number']}" for pr in stack_dicts)
        dep_id = client.create_deployment(f"Queue position {position}: {prs_desc}")
        client.update_deployment_status(
            dep_id, "queued", f"Waiting in position {position}"
        )
        entry["deployment_id"] = dep_id
    except Exception as e:
        log.warning("Could not create deployment: %s", e)

    # Post initial comments and track IDs
    cids: dict[int, int] = {}
    for pr in stack_dicts:
        cid = _comment(
            client,
            pr["number"],
            comments.progress("queued", stack_dicts, owner=owner, repo=repo),
        )
        if cid:
            cids[pr["number"]] = cid
    entry["comment_ids"] = cids

    branch_state.setdefault("queue", []).append(entry)
    state["updated_at"] = _now_iso()
    store.write(state)

    log.info("Enqueued stack at position %d for branch %s", position, resolved_target)

    # Trigger processing inline if the target branch is idle.
    has_active = (
        branch_state.get("active_batch") is not None or api_state.has_active_batch
    )
    if not has_active:
        return do_process(client)
    return "queued_waiting"


def _stack_to_dicts(stack, client) -> list[dict]:
    """Convert Stack to serializable dicts, fetching titles."""
    result = []
    for pr in stack.prs:
        title = ""
        try:
            pr_data = client.get_pr(pr.number)
            title = pr_data.get("title", "")
        except Exception:
            pass
        result.append(
            {
                "number": pr.number,
                "head_sha": pr.head_sha,
                "head_ref": pr.head_ref,
                "base_ref": pr.base_ref,
                "title": title,
            }
        )
    return result


def do_process(client: GitHubClientProtocol) -> str:
    """Process the next batch from the queue across all branches."""
    store = StateStore(client)
    state = store.read()
    owner, repo = _owner_repo(client)

    # Clear any stale active batches across all branches; track genuinely-active ones.
    has_active_batch = False
    for branch_name, branch_state in list(state.get("branches", {}).items()):
        active = branch_state.get("active_batch")
        if active is None:
            continue

        all_merged = True
        for pr_info in active.get("stack", []):
            try:
                pr_data = client.get_pr(pr_info["number"])
                if pr_data.get("state") == "open":
                    all_merged = False
                    break
            except Exception:
                all_merged = False
                break

        if all_merged:
            log.warning(
                "Active batch for %s PRs are merged/closed, clearing stale state",
                branch_name,
            )
            _clear_active_batch(state, store, branch_name)
        else:
            try:
                started = datetime.datetime.fromisoformat(active["started_at"])
                age = (
                    datetime.datetime.now(datetime.timezone.utc) - started
                ).total_seconds()
                if age > 30 * 60:
                    log.warning(
                        "Active batch for %s is stale (%.0fs old), clearing it",
                        branch_name,
                        age,
                    )
                    batch_mod.abort_batch(client)
                    _clear_active_batch(state, store, branch_name)
                elif active.get("progress") == "completing":
                    # A previous run wrote "completing" but was cancelled before
                    # finishing the merge.  Resume the completion.
                    log.info(
                        "Resuming stuck completion for %s (%.0fs old)",
                        branch_name,
                        age,
                    )
                    _resume_completion(
                        client, state, store, branch_name, active, owner, repo
                    )
                else:
                    log.info(
                        "Active batch for %s in progress (%.0fs), skipping",
                        branch_name,
                        age,
                    )
                    has_active_batch = True
            except Exception:
                log.info("Active batch for %s in progress, skipping", branch_name)
                has_active_batch = True

    # Skip the sync scan when all branches are already busy — avoids an extra API call.
    if not has_active_batch:
        # Fetch open PRs once and share with both helpers to avoid redundant API calls.
        all_open_prs = client.list_open_prs()
        state = _sync_missing_prs(client, state, store, open_prs=all_open_prs)
        state = _cleanup_stale_entries(client, state, store, open_prs=all_open_prs)

    # Find first branch with a non-empty queue and no active_batch
    target_branch_to_process: str | None = None
    for branch_name, branch_state in state.get("branches", {}).items():
        if branch_state.get("active_batch") is None and branch_state.get("queue"):
            target_branch_to_process = branch_name
            break

    if target_branch_to_process is None:
        if has_active_batch:
            log.info("Active batch in progress, skipping")
            return "batch_active"
        log.info("All branch queues are empty or busy, nothing to do")
        return "no_stacks"

    # Ensure all target branches have protection rulesets now that we know there is
    # work to process.  Runs after early-exit paths to avoid unnecessary API calls.
    from merge_queue import config as config_mod

    target_branches = config_mod.get_target_branches(client)
    config_mod.ensure_branch_protection(client, target_branches)

    branch_state = state["branches"][target_branch_to_process]
    queue = branch_state["queue"]

    # Pre-condition rules
    api_state = QueueState.fetch(client)
    results = rules_mod.check_all(api_state)
    failures = [r for r in results if not r.passed]
    if failures:
        for f in failures:
            log.error("Rule failed: %s — %s", f.name, f.message)
        return "rules_failed"

    # Pop first (FIFO)
    entry = queue.pop(0)
    for i, e in enumerate(queue):
        e["position"] = i + 1

    # Build stack
    from merge_queue.types import PullRequest, Stack

    prs = tuple(
        PullRequest(
            number=pr["number"],
            head_sha=pr["head_sha"],
            head_ref=pr["head_ref"],
            base_ref=pr["base_ref"],
            labels=("queue",),
            queued_at=datetime.datetime.fromisoformat(entry["queued_at"]),
        )
        for pr in entry["stack"]
    )
    next_stack = Stack(
        prs=prs, queued_at=datetime.datetime.fromisoformat(entry["queued_at"])
    )

    log.info(
        "Processing for %s: %s",
        target_branch_to_process,
        " -> ".join(f"#{pr.number}" for pr in prs),
    )

    dep_id = entry.get("deployment_id")
    cids = _normalize_cids(entry.get("comment_ids"))
    entry_target_branch: str = (
        entry.get("target_branch")
        or target_branch_to_process
        or api_state.default_branch
    )

    _update_deployment(client, dep_id, "in_progress", "Locking branches...")

    # Set active batch in state (include comment_ids for abort)
    started_at = _now_iso()
    active_batch_dict = {
        "batch_id": "",
        "branch": "",
        "ruleset_id": None,
        "started_at": started_at,
        "progress": "locking",
        "stack": entry["stack"],
        "deployment_id": dep_id,
        "comment_ids": cids,
        "queued_at": entry["queued_at"],
        "target_branch": entry_target_branch,
    }
    branch_state["active_batch"] = active_batch_dict
    state["updated_at"] = _now_iso()
    store.write(state)

    # Update comments: show queue-wait duration now that processing has started
    queue_wait = _fmt_duration(
        (
            datetime.datetime.fromisoformat(started_at)
            - datetime.datetime.fromisoformat(entry["queued_at"])
        ).total_seconds()
    )
    for pr in prs:
        _comment(
            client,
            pr.number,
            comments.progress(
                "locking",
                entry["stack"],
                timings={"Queue wait": queue_wait},
                owner=owner,
                repo=repo,
            ),
            cids,
        )

    # Create batch
    try:
        batch = batch_mod.create_batch(
            client, next_stack, target_branch=entry_target_branch
        )
    except Exception as e:
        log.error("Failed to create batch: %s", e)
        error_str = str(e)
        if "conflict" in error_str.lower():
            log.info("Merge conflict detected, notifying author")
            for pr in prs:
                _comment(
                    client,
                    pr.number,
                    comments.merge_conflict(entry_target_branch, owner, repo),
                    cids,
                )
                try:
                    client.remove_label(pr.number, "queue")
                except Exception:
                    pass
        else:
            for pr in prs:
                _comment(
                    client,
                    pr.number,
                    comments.batch_error(error_str, owner, repo),
                    cids,
                )
                try:
                    client.remove_label(pr.number, "queue")
                except Exception:
                    pass
        _update_deployment(client, dep_id, "failure", error_str[:140])
        _clear_active_batch(state, store, entry_target_branch)
        return "batch_error"

    # Update state
    ci_started_at = _now_iso()
    active_batch_dict["batch_id"] = batch.batch_id
    active_batch_dict["branch"] = batch.branch
    active_batch_dict["ruleset_id"] = batch.ruleset_id
    active_batch_dict["progress"] = "running_ci"
    active_batch_dict["ci_started_at"] = ci_started_at
    state["updated_at"] = _now_iso()
    store.write(state)

    _update_deployment(client, dep_id, "in_progress", f"CI running on {batch.branch}")

    # Update comments: show queue-wait + lock duration now that CI has started
    lock_duration = _fmt_duration(
        (
            datetime.datetime.fromisoformat(ci_started_at)
            - datetime.datetime.fromisoformat(started_at)
        ).total_seconds()
    )
    actions_url = f"https://github.com/{owner}/{repo}/actions" if owner and repo else ""
    for pr in prs:
        _comment(
            client,
            pr.number,
            comments.progress(
                "running_ci",
                entry["stack"],
                timings={"Queue wait": queue_wait, "Lock": lock_duration},
                branch=batch.branch,
                ci_run_url=actions_url,
                owner=owner,
                repo=repo,
            ),
            cids,
        )

    # Run CI
    ci_result = batch_mod.run_ci(client, batch)
    ci_completed_at = _now_iso()

    if ci_result.passed:
        active_batch_dict["progress"] = "completing"
        state["updated_at"] = _now_iso()
        store.write(state)

        try:
            batch_mod.complete_batch(client, batch, target_branch=entry_target_branch)
            merge_completed_at = _now_iso()
            log.info("Batch merged!")
            status = "merged"
            _update_deployment(
                client, dep_id, "success", f"Merged to {entry_target_branch}"
            )
            # Update comments with full timing breakdown
            for pr in prs:
                _comment(
                    client,
                    pr.number,
                    comments.merged(
                        entry_target_branch,
                        stack=entry["stack"],
                        queued_at=entry["queued_at"],
                        started_at=started_at,
                        ci_started_at=ci_started_at,
                        ci_completed_at=ci_completed_at,
                        completed_at=merge_completed_at,
                        ci_run_url=ci_result.run_url,
                        owner=owner,
                        repo=repo,
                    ),
                    cids,
                )
        except batch_mod.BatchError as e:
            log.error("Complete failed: %s", e)
            error_str = str(e)
            batch_mod.fail_batch(client, batch, error_str)
            if "diverged" in error_str.lower():
                retry_count = entry.get("retry_count", 0)
                max_retries = 3
                total_attempts = max_retries + 1
                if retry_count < max_retries:
                    attempt_num = retry_count + 2  # next attempt number (1-indexed)
                    log.info(
                        "Target branch diverged, auto-retrying (attempt %d/%d)",
                        attempt_num,
                        total_attempts,
                    )
                    retry_info = (
                        f"(attempt {attempt_num}/{total_attempts})"
                        if retry_count > 0
                        else None
                    )
                    for pr in prs:
                        _comment(
                            client,
                            pr.number,
                            comments.auto_retrying(
                                entry_target_branch, owner, repo, retry_info=retry_info
                            ),
                            cids,
                        )
                    entry["retry_count"] = retry_count + 1
                    branch_state.setdefault("queue", []).insert(0, entry)
                    _clear_active_batch(state, store, entry_target_branch)
                    return do_process(client)
                log.info("Giving up after %d retries", max_retries)
                error_str = (
                    f"Failed after {total_attempts} attempts"
                    " \u2014 target branch keeps moving"
                )
            status = "complete_error"
            _update_deployment(client, dep_id, "failure", error_str[:140])
            for pr in prs:
                _comment(
                    client,
                    pr.number,
                    comments.failed(
                        error_str, ci_run_url=ci_result.run_url, owner=owner, repo=repo
                    ),
                    cids,
                )
    else:
        batch_mod.fail_batch(client, batch, "CI failed")
        status = "ci_failed"
        # Extract which job/step failed
        failed_job, failed_step = "", ""
        if ci_result.run_url:
            try:
                failed_job, failed_step = client.get_failed_job_info(ci_result.run_url)
            except Exception:
                pass
        desc = f"CI failed: {failed_job}" if failed_job else "CI failed"
        _update_deployment(client, dep_id, "failure", desc[:140])
        for pr in prs:
            _comment(
                client,
                pr.number,
                comments.failed(
                    "CI failed",
                    ci_result.run_url,
                    failed_job,
                    failed_step,
                    owner,
                    repo,
                ),
                cids,
            )

    # Record history
    started = active_batch_dict["started_at"]
    completed = _now_iso()
    try:
        dur = (
            datetime.datetime.fromisoformat(completed)
            - datetime.datetime.fromisoformat(started)
        ).total_seconds()
    except Exception:
        dur = 0

    state.setdefault("history", []).append(
        {
            "batch_id": batch.batch_id,
            "status": status,
            "completed_at": completed,
            "prs": [pr.number for pr in prs],
            "duration_seconds": dur,
            "target_branch": entry_target_branch,
        }
    )
    _clear_active_batch(state, store, entry_target_branch)

    # Process next if any branch still has items queued
    has_more = any(bs.get("queue") for bs in state.get("branches", {}).values())
    if has_more:
        log.info("More stacks queued, continuing...")
        return do_process(client)

    return status


def do_abort(client: GitHubClientProtocol, pr_number: int) -> str:
    """Abort active batch or remove from queue."""
    store = StateStore(client)
    state = store.read()
    owner, repo = _owner_repo(client)

    # Search all branches for active batch containing this PR
    for branch_name, branch_state in state.get("branches", {}).items():
        active = branch_state.get("active_batch")
        if active and any(pr["number"] == pr_number for pr in active.get("stack", [])):
            log.info(
                "Aborting active batch for PR #%d on branch %s", pr_number, branch_name
            )
            batch_mod.abort_batch(client)
            dep_id = active.get("deployment_id")
            cids = _normalize_cids(active.get("comment_ids"))
            _update_deployment(client, dep_id, "inactive", "Aborted")
            branch_state["active_batch"] = None
            state["updated_at"] = _now_iso()
            store.write(state)
            for pr in active.get("stack", []):
                _comment(client, pr["number"], comments.aborted(owner, repo), cids)
            return "aborted"

    # Search all branches' queues
    for branch_name, branch_state in state.get("branches", {}).items():
        queue = branch_state.get("queue", [])
        for i, entry in enumerate(queue):
            if any(pr["number"] == pr_number for pr in entry.get("stack", [])):
                removed = queue.pop(i)
                for j, e in enumerate(queue):
                    e["position"] = j + 1
                cids = _normalize_cids(removed.get("comment_ids"))
                dep_id = removed.get("deployment_id")
                _update_deployment(client, dep_id, "inactive", "Removed")
                state["updated_at"] = _now_iso()
                store.write(state)
                for pr in removed.get("stack", []):
                    _comment(
                        client,
                        pr["number"],
                        comments.removed_from_queue(owner, repo),
                        cids,
                    )
                return "removed"

    log.info("PR #%d not found in queue or active batch", pr_number)
    return "not_found"


def do_retest(client: GitHubClientProtocol, pr_number: int) -> str:
    """Retrigger CI on a PR's head branch, then clean up the re-test label."""
    pr_data = client.get_pr(pr_number)
    ref = pr_data["head"]["ref"]
    owner, repo = _owner_repo(client)
    client.dispatch_ci_on_ref(ref)
    client.remove_label(pr_number, "re-test")
    _comment(client, pr_number, comments.ci_retriggered(owner, repo))
    return "retriggered"


def do_hotfix(client: GitHubClientProtocol, pr_number: int) -> str:
    """Hotfix — insert at front of queue, abort active batch, process immediately.

    Only authorized users (admins or break_glass_users) can use this.
    The hotfix PR goes through the normal MQ pipeline but jumps to position 0.
    If there is an active batch, it is aborted and its PRs are re-queued behind
    the hotfix.
    """
    owner, repo = _owner_repo(client)
    sender = os.environ.get("MQ_SENDER", "")

    # Auth check (same as break-glass)
    if not _is_break_glass_authorized(client, sender):
        log.warning("hotfix rejected: %s is not authorized", sender)
        _comment(
            client,
            pr_number,
            comments.break_glass_denied(sender, owner, repo),
        )
        client.remove_label(pr_number, "hotfix")
        return "denied"

    log.warning("HOTFIX by %s on PR #%d — jumping to front of queue", sender, pr_number)

    # Get PR info
    pr_data = client.get_pr(pr_number)
    from merge_queue import config

    target_branch = pr_data["base"]["ref"]
    target_branches = config.get_target_branches(client)
    if target_branch not in target_branches:
        target_branch = client.get_default_branch()

    store = StateStore(client)
    state = store.read()

    branch_state = state.setdefault("branches", {}).setdefault(
        target_branch, empty_branch_state()
    )

    # Build queue entry for the hotfix (same shape as do_enqueue)
    hotfix_stack = [
        {
            "number": pr_number,
            "head_sha": pr_data["head"]["sha"],
            "head_ref": pr_data["head"]["ref"],
            "base_ref": pr_data["base"]["ref"],
            "title": pr_data.get("title", ""),
        }
    ]
    hotfix_entry = {
        "position": 1,
        "queued_at": _now_iso(),
        "stack": hotfix_stack,
        "deployment_id": None,
        "target_branch": target_branch,
    }

    # If there is an active batch for this branch, abort it and re-queue its PRs
    requeued_entries: list[dict] = []
    active = branch_state.get("active_batch")
    if active is not None:
        log.info("Aborting active batch %s for hotfix", active.get("batch_id"))
        batch_mod.abort_batch(client)
        # Re-queue the batch's PRs as a single entry (preserving the stack)
        requeued_entries.append(
            {
                "position": 0,  # will be renumbered below
                "queued_at": active.get("started_at", _now_iso()),
                "stack": active.get("stack", []),
                "deployment_id": active.get("deployment_id"),
                "target_branch": target_branch,
            }
        )
        branch_state["active_batch"] = None

    # Build new queue: hotfix first, then re-queued batch PRs, then existing queue
    existing_queue = branch_state.get("queue", [])
    new_queue = [hotfix_entry] + requeued_entries + existing_queue

    # Renumber positions (1-based)
    for i, entry in enumerate(new_queue):
        entry["position"] = i + 1

    branch_state["queue"] = new_queue
    state["updated_at"] = _now_iso()
    store.write(state)

    _comment(
        client,
        pr_number,
        f"\U0001f6a8 **Hotfix** `[{target_branch}]` — queued at front of merge queue (by `{sender}`)",
    )

    log.info(
        "Hotfix PR #%d queued at position 1 for branch %s", pr_number, target_branch
    )

    return do_process(client)


def do_check_rules(client: GitHubClientProtocol) -> list[rules_mod.RuleResult]:
    state = QueueState.fetch(client)
    return rules_mod.check_all(state)


def do_status(client: GitHubClientProtocol) -> str:
    store = StateStore(client)
    return render_status_terminal(store.read())


# --- CLI wrappers ---


def _log_rate_limit(client: GitHubClientProtocol) -> None:
    rl = getattr(client, "rate_limit", None)
    if rl:
        log.info("API usage: %s", rl.summary())


def cmd_enqueue(args: argparse.Namespace) -> None:
    client = _make_client()
    do_enqueue(client, args.pr_number)
    _log_rate_limit(client)


def cmd_process(args: argparse.Namespace) -> None:
    client = _make_client()
    result = do_process(client)
    _log_rate_limit(client)
    if result == "rules_failed":
        sys.exit(1)


def cmd_abort(args: argparse.Namespace) -> None:
    client = _make_client()
    do_abort(client, args.pr_number)
    _log_rate_limit(client)


def cmd_retest(args: argparse.Namespace) -> None:
    client = _make_client()
    do_retest(client, args.pr_number)
    _log_rate_limit(client)


def cmd_hotfix(args: argparse.Namespace) -> None:
    client = _make_client()
    result = do_hotfix(client, args.pr_number)
    _log_rate_limit(client)
    if result == "failed":
        sys.exit(1)


def cmd_check_rules(args: argparse.Namespace) -> None:
    client = _make_client()
    results = do_check_rules(client)
    _log_rate_limit(client)
    any_failed = False
    for r in results:
        status = "PASS" if r.passed else "FAIL"
        print(f"[{status}] {r.name}: {r.message}")
        if not r.passed:
            any_failed = True
    if any_failed:
        sys.exit(1)


def cmd_status(args: argparse.Namespace) -> None:
    client = _make_client()
    print(do_status(client))
    _log_rate_limit(client)


def cmd_summary(args: argparse.Namespace) -> None:
    client = _make_client()
    store = StateStore(client)
    state = store.read()
    print(render_status_md(state, client))
    _log_rate_limit(client)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(name)s: %(message)s"
    )

    parser = argparse.ArgumentParser(prog="merge-queue")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("enqueue")
    p.add_argument("pr_number", type=int)
    p.set_defaults(func=cmd_enqueue)

    sub.add_parser("process").set_defaults(func=cmd_process)

    p = sub.add_parser("abort")
    p.add_argument("pr_number", type=int)
    p.set_defaults(func=cmd_abort)

    p = sub.add_parser("retest")
    p.add_argument("pr_number", type=int)
    p.set_defaults(func=cmd_retest)

    p = sub.add_parser("hotfix")
    p.add_argument("pr_number", type=int)
    p.set_defaults(func=cmd_hotfix)

    sub.add_parser("check-rules").set_defaults(func=cmd_check_rules)
    sub.add_parser("status").set_defaults(func=cmd_status)
    sub.add_parser("summary").set_defaults(func=cmd_summary)

    args = parser.parse_args()
    args.func(args)
