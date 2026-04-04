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
from merge_queue.queue import detect_stacks, order_queue, select_next
from merge_queue.state import QueueState
from merge_queue.status import render_status_terminal
from merge_queue.store import StateStore
from merge_queue.types import empty_state

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


def _comment(client, pr_number: int, body: str) -> None:
    try:
        client.create_comment(pr_number, body)
    except Exception as e:
        log.warning("Could not comment on PR #%d: %s", pr_number, e)


# --- Core logic ---


def do_enqueue(client: GitHubClientProtocol, pr_number: int) -> str:
    """Enqueue a PR: update state, create deployment, trigger processing."""
    store = StateStore(client)
    state = store.read()
    owner, repo = _owner_repo(client)

    # Already queued?
    for entry in state.get("queue", []):
        if any(pr["number"] == pr_number for pr in entry.get("stack", [])):
            _comment(client, pr_number, comments.already_queued(entry.get("position", 0), owner, repo))
            return "already_queued"

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
        pr_data = client.get_pr(pr_number)
        stack_dicts = [{
            "number": pr_number,
            "head_sha": pr_data["head"]["sha"],
            "head_ref": pr_data["head"]["ref"],
            "base_ref": pr_data["base"]["ref"],
            "title": pr_data.get("title", ""),
        }]

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
        client.update_deployment_status(dep_id, "queued", f"Waiting in position {position}")
        entry["deployment_id"] = dep_id
    except Exception as e:
        log.warning("Could not create deployment: %s", e)

    state.setdefault("queue", []).append(entry)
    state["updated_at"] = _now_iso()
    store.write(state)

    for pr in stack_dicts:
        _comment(client, pr["number"], comments.queued(position, total, stack_dicts, owner, repo))

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
        result.append({
            "number": pr.number, "head_sha": pr.head_sha,
            "head_ref": pr.head_ref, "base_ref": pr.base_ref,
            "title": title,
        })
    return result


def do_process(client: GitHubClientProtocol) -> str:
    """Process the next batch from the queue."""
    store = StateStore(client)
    state = store.read()
    owner, repo = _owner_repo(client)

    if state.get("active_batch") is not None:
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
            number=pr["number"], head_sha=pr["head_sha"],
            head_ref=pr["head_ref"], base_ref=pr["base_ref"],
            labels=("queue",),
            queued_at=datetime.datetime.fromisoformat(entry["queued_at"]),
        )
        for pr in entry["stack"]
    )
    next_stack = Stack(prs=prs, queued_at=datetime.datetime.fromisoformat(entry["queued_at"]))

    log.info("Processing: %s", " -> ".join(f"#{pr.number}" for pr in prs))

    dep_id = entry.get("deployment_id")
    if dep_id:
        try:
            client.update_deployment_status(dep_id, "in_progress", "Locking branches...")
        except Exception:
            pass

    # Set active batch in state
    state["active_batch"] = {
        "batch_id": "", "branch": "", "ruleset_id": None,
        "started_at": _now_iso(), "progress": "locking",
        "stack": entry["stack"], "deployment_id": dep_id,
    }
    state["updated_at"] = _now_iso()
    store.write(state)

    # Create batch
    try:
        batch = batch_mod.create_batch(client, next_stack)
    except batch_mod.BatchError as e:
        log.error("Failed to create batch: %s", e)
        for pr in prs:
            _comment(client, pr.number, comments.batch_error(str(e), owner, repo))
            client.remove_label(pr.number, "queue")
        if dep_id:
            try:
                client.update_deployment_status(dep_id, "failure", str(e)[:140])
            except Exception:
                pass
        state["active_batch"] = None
        state["updated_at"] = _now_iso()
        store.write(state)
        return "batch_error"

    # Update state
    state["active_batch"]["batch_id"] = batch.batch_id
    state["active_batch"]["branch"] = batch.branch
    state["active_batch"]["ruleset_id"] = batch.ruleset_id
    state["active_batch"]["progress"] = "running_ci"
    state["updated_at"] = _now_iso()
    store.write(state)

    if dep_id:
        try:
            client.update_deployment_status(dep_id, "in_progress", f"CI running on {batch.branch}")
        except Exception:
            pass

    # Notify PRs
    for pr in prs:
        _comment(client, pr.number, comments.batch_started(batch.branch, entry["stack"], owner, repo))

    # Run CI
    ci_result = batch_mod.run_ci(client, batch)

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
                    client.update_deployment_status(dep_id, "success", f"Merged to {api_state.default_branch}")
                except Exception:
                    pass
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
                _comment(client, pr.number, comments.failed(str(e), ci_result.run_url, owner, repo))
    else:
        batch_mod.fail_batch(client, batch, "CI failed")
        status = "ci_failed"
        if dep_id:
            try:
                client.update_deployment_status(dep_id, "failure", "CI failed")
            except Exception:
                pass
        for pr in prs:
            _comment(client, pr.number, comments.failed("CI failed", ci_result.run_url, owner, repo))

    # Record history
    started = state["active_batch"]["started_at"]
    completed = _now_iso()
    try:
        dur = (datetime.datetime.fromisoformat(completed) - datetime.datetime.fromisoformat(started)).total_seconds()
    except Exception:
        dur = 0

    state.setdefault("history", []).append({
        "batch_id": batch.batch_id, "status": status,
        "completed_at": completed, "prs": [pr.number for pr in prs],
        "duration_seconds": dur,
    })
    state["active_batch"] = None
    state["updated_at"] = _now_iso()
    store.write(state)

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
        if dep_id:
            try:
                client.update_deployment_status(dep_id, "inactive", "Aborted")
            except Exception:
                pass
        state["active_batch"] = None
        state["updated_at"] = _now_iso()
        store.write(state)
        _comment(client, pr_number, comments.aborted(owner, repo))
        return "aborted"

    # In queue?
    queue = state.get("queue", [])
    for i, entry in enumerate(queue):
        if any(pr["number"] == pr_number for pr in entry.get("stack", [])):
            removed = queue.pop(i)
            for j, e in enumerate(queue):
                e["position"] = j + 1
            dep_id = removed.get("deployment_id")
            if dep_id:
                try:
                    client.update_deployment_status(dep_id, "inactive", "Removed")
                except Exception:
                    pass
            state["updated_at"] = _now_iso()
            store.write(state)
            _comment(client, pr_number, comments.removed_from_queue(owner, repo))
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
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

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
