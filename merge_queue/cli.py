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
from merge_queue.status import render_status_terminal
from merge_queue.store import StateStore

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


def _clear_active_batch(state: dict, store: StateStore) -> None:
    """Clear active_batch from state with retry on conflict."""
    for attempt in range(3):
        try:
            state["active_batch"] = None
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


# --- Core logic ---


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

    # Guard: already in active batch?
    active = state.get("active_batch")
    if active and any(pr["number"] == pr_number for pr in active.get("stack", [])):
        log.info("PR #%d is already in the active batch, skipping", pr_number)
        return "already_active"

    # Guard: already queued?
    for entry in state.get("queue", []):
        if any(pr["number"] == pr_number for pr in entry.get("stack", [])):
            log.info(
                "PR #%d is already queued at position %d",
                pr_number,
                entry.get("position", 0),
            )
            return "already_queued"

    # Guard: recently processed? (check history for this PR in last 5 minutes)
    now = datetime.datetime.now(datetime.timezone.utc)
    for h in reversed(state.get("history", [])):
        if pr_number in h.get("prs", []):
            try:
                completed = datetime.datetime.fromisoformat(h["completed_at"])
                if (now - completed).total_seconds() < 300:
                    log.info(
                        "PR #%d was processed %ds ago, skipping duplicate",
                        pr_number,
                        int((now - completed).total_seconds()),
                    )
                    return "recently_processed"
            except Exception:
                pass
            break

    # Detect the stack
    api_state = QueueState.fetch(client)
    stack_dicts = None
    for pr in api_state.prs:
        if pr.number == pr_number:
            stacks = detect_stacks(api_state.queued_prs, api_state.default_branch)
            for s in stacks:
                if any(p.number == pr_number for p in s.prs):
                    stack_dicts = _stack_to_dicts(s, client)
                    break
            break

    if stack_dicts is None:
        # Reuse the pr_data fetched during the guard check — avoids a duplicate API call.
        pr_data = cached_pr_data or client.get_pr(pr_number)
        stack_dicts = [
            {
                "number": pr_number,
                "head_sha": pr_data["head"]["sha"],
                "head_ref": pr_data["head"]["ref"],
                "base_ref": pr_data["base"]["ref"],
                "title": pr_data.get("title", ""),
            }
        ]

    position = len(state.get("queue", [])) + 1
    total = position
    entry = {
        "position": position,
        "queued_at": _now_iso(),
        "stack": stack_dicts,
        "deployment_id": None,
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
            comments.queued(position, total, stack_dicts, owner, repo),
        )
        if cid:
            cids[pr["number"]] = cid
    entry["comment_ids"] = cids

    state.setdefault("queue", []).append(entry)
    state["updated_at"] = _now_iso()
    store.write(state)

    log.info("Enqueued stack at position %d", position)

    # Trigger processing if idle
    if state.get("active_batch") is None and not api_state.has_active_batch:
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
    """Process the next batch from the queue."""
    store = StateStore(client)
    state = store.read()
    owner, repo = _owner_repo(client)

    active = state.get("active_batch")
    if active is not None:
        # Check if the active batch's PRs are already merged (stale from race condition)
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
            log.warning("Active batch PRs are all merged/closed, clearing stale state")
            _clear_active_batch(state, store)
            # Fall through to process next
        else:
            # Check for stale batch (crashed worker) — auto-recover after 30 minutes
            try:
                started = datetime.datetime.fromisoformat(active["started_at"])
                age = (
                    datetime.datetime.now(datetime.timezone.utc) - started
                ).total_seconds()
                if age > 30 * 60:
                    log.warning("Active batch is stale (%.0fs old), clearing it", age)
                    batch_mod.abort_batch(client)
                    _clear_active_batch(state, store)
                    # Fall through to process next
                else:
                    log.info("Active batch in progress (%.0fs), skipping", age)
                    return "batch_active"
            except Exception:
                log.info("Active batch in progress, skipping")
                return "batch_active"

    queue = state.get("queue", [])
    if not queue:
        log.info("Queue empty, nothing to do")
        return "no_stacks"

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

    log.info("Processing: %s", " -> ".join(f"#{pr.number}" for pr in prs))

    dep_id = entry.get("deployment_id")
    cids = entry.get("comment_ids", {})
    # Normalize keys to int
    cids = {int(k): v for k, v in cids.items()} if cids else {}

    if dep_id:
        try:
            client.update_deployment_status(
                dep_id, "in_progress", "Locking branches..."
            )
        except Exception:
            pass

    # Set active batch in state (include comment_ids for abort)
    started_at = _now_iso()
    state["active_batch"] = {
        "batch_id": "",
        "branch": "",
        "ruleset_id": None,
        "started_at": started_at,
        "progress": "locking",
        "stack": entry["stack"],
        "deployment_id": dep_id,
        "comment_ids": cids,
        "queued_at": entry["queued_at"],
    }
    state["updated_at"] = _now_iso()
    store.write(state)

    # Create batch
    try:
        batch = batch_mod.create_batch(client, next_stack)
    except Exception as e:
        log.error("Failed to create batch: %s", e)
        for pr in prs:
            _comment(client, pr.number, comments.batch_error(str(e), owner, repo), cids)
            try:
                client.remove_label(pr.number, "queue")
            except Exception:
                pass
        if dep_id:
            try:
                client.update_deployment_status(dep_id, "failure", str(e)[:140])
            except Exception:
                pass
        _clear_active_batch(state, store)
        return "batch_error"

    # Update state
    ci_started_at = _now_iso()
    state["active_batch"]["batch_id"] = batch.batch_id
    state["active_batch"]["branch"] = batch.branch
    state["active_batch"]["ruleset_id"] = batch.ruleset_id
    state["active_batch"]["progress"] = "running_ci"
    state["active_batch"]["ci_started_at"] = ci_started_at
    state["updated_at"] = _now_iso()
    store.write(state)

    if dep_id:
        try:
            client.update_deployment_status(
                dep_id, "in_progress", f"CI running on {batch.branch}"
            )
        except Exception:
            pass

    # Update comments to show CI running (with link to Actions tab)
    actions_url = f"https://github.com/{owner}/{repo}/actions" if owner and repo else ""
    for pr in prs:
        _comment(
            client,
            pr.number,
            comments.batch_started(
                batch.branch,
                entry["stack"],
                ci_run_url=actions_url,
                owner=owner,
                repo=repo,
            ),
            cids,
        )

    # Run CI
    ci_result = batch_mod.run_ci(client, batch)
    completed_at = _now_iso()

    if ci_result.passed:
        state["active_batch"]["progress"] = "completing"
        state["updated_at"] = _now_iso()
        store.write(state)

        try:
            batch_mod.complete_batch(client, batch)
            log.info("Batch merged!")
            status = "merged"
            if dep_id:
                try:
                    client.update_deployment_status(
                        dep_id, "success", f"Merged to {api_state.default_branch}"
                    )
                except Exception:
                    pass
            # Update comments with final merged status + stats + CI link
            for pr in prs:
                _comment(
                    client,
                    pr.number,
                    comments.merged(
                        api_state.default_branch,
                        stack=entry["stack"],
                        queued_at=entry["queued_at"],
                        ci_started_at=ci_started_at,
                        completed_at=completed_at,
                        ci_run_url=ci_result.run_url,
                        owner=owner,
                        repo=repo,
                    ),
                    cids,
                )
        except batch_mod.BatchError as e:
            log.error("Complete failed: %s", e)
            batch_mod.fail_batch(client, batch, str(e))
            status = "complete_error"
            if dep_id:
                try:
                    client.update_deployment_status(dep_id, "failure", str(e)[:140])
                except Exception:
                    pass
            for pr in prs:
                _comment(
                    client,
                    pr.number,
                    comments.failed(str(e), ci_result.run_url, owner, repo),
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
        if dep_id:
            desc = f"CI failed: {failed_job}" if failed_job else "CI failed"
            try:
                client.update_deployment_status(dep_id, "failure", desc[:140])
            except Exception:
                pass
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
    started = state["active_batch"]["started_at"]
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
        }
    )
    _clear_active_batch(state, store)

    # Process next
    if state.get("queue"):
        log.info("More stacks queued, continuing...")
        return do_process(client)

    return status


def do_abort(client: GitHubClientProtocol, pr_number: int) -> str:
    """Abort active batch or remove from queue."""
    store = StateStore(client)
    state = store.read()
    owner, repo = _owner_repo(client)

    # In active batch?
    active = state.get("active_batch")
    if active and any(pr["number"] == pr_number for pr in active.get("stack", [])):
        log.info("Aborting active batch for PR #%d", pr_number)
        batch_mod.abort_batch(client)
        dep_id = active.get("deployment_id")
        cids = active.get("comment_ids", {})
        cids = {int(k): v for k, v in cids.items()} if cids else {}
        if dep_id:
            try:
                client.update_deployment_status(dep_id, "inactive", "Aborted")
            except Exception:
                pass
        state["active_batch"] = None
        state["updated_at"] = _now_iso()
        store.write(state)
        # Update ALL PR comments in the batch, not just the one that was unlabeled
        for pr in active.get("stack", []):
            _comment(client, pr["number"], comments.aborted(owner, repo), cids)
        return "aborted"

    # In queue?
    queue = state.get("queue", [])
    for i, entry in enumerate(queue):
        if any(pr["number"] == pr_number for pr in entry.get("stack", [])):
            removed = queue.pop(i)
            for j, e in enumerate(queue):
                e["position"] = j + 1
            cids = removed.get("comment_ids", {})
            cids = {int(k): v for k, v in cids.items()} if cids else {}
            dep_id = removed.get("deployment_id")
            if dep_id:
                try:
                    client.update_deployment_status(dep_id, "inactive", "Removed")
                except Exception:
                    pass
            state["updated_at"] = _now_iso()
            store.write(state)
            for pr in removed.get("stack", []):
                _comment(
                    client, pr["number"], comments.removed_from_queue(owner, repo), cids
                )
            return "removed"

    log.info("PR #%d not found in queue or active batch", pr_number)
    return "not_found"


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

    sub.add_parser("check-rules").set_defaults(func=cmd_check_rules)
    sub.add_parser("status").set_defaults(func=cmd_status)

    args = parser.parse_args()
    args.func(args)
