"""CLI entry point for the merge queue."""

from __future__ import annotations

import argparse
import datetime
import logging
import os
import sys

from merge_queue import batch as batch_mod
from merge_queue import rules as rules_mod
from merge_queue.github_client import GitHubClient, GitHubClientProtocol
from merge_queue.queue import detect_stacks, order_queue, select_next
from merge_queue.state import QueueState
from merge_queue.status import render_status_terminal
from merge_queue.store import StateStore
from merge_queue.types import empty_state

log = logging.getLogger("merge_queue")

MQ_DEPLOYMENTS_URL = "https://github.com/{owner}/{repo}/deployments/merge-queue"


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


def _mq_url(client: GitHubClientProtocol) -> str:
    owner = getattr(client, "owner", "")
    repo = getattr(client, "repo", "")
    if owner and repo:
        return MQ_DEPLOYMENTS_URL.format(owner=owner, repo=repo)
    return ""


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _stack_to_dicts(prs) -> list[dict]:
    """Convert PullRequest objects to serializable dicts for state.json."""
    return [
        {"number": pr.number, "head_sha": pr.head_sha, "head_ref": pr.head_ref,
         "base_ref": pr.base_ref, "title": ""}
        for pr in prs
    ]


# --- Core logic functions ---


def do_enqueue(client: GitHubClientProtocol, pr_number: int) -> str:
    """Enqueue a PR: update state, create deployment, trigger processing."""
    store = StateStore(client)
    state = store.read()
    mq_url = _mq_url(client)

    # Check if this PR's stack is already queued
    for entry in state.get("queue", []):
        if any(pr["number"] == pr_number for pr in entry.get("stack", [])):
            log.info("PR #%d is already in queue position %d", pr_number, entry.get("position", 0))
            client.create_comment(
                pr_number,
                f"**Merge Queue:** PR already queued (position {entry.get('position', '?')}).\n\n"
                f"[View merge queue →]({mq_url})",
            )
            return "already_queued"

    # Detect the stack this PR belongs to
    api_state = QueueState.fetch(client)
    stack = None
    for pr in api_state.prs:
        if pr.number == pr_number:
            stacks = detect_stacks(api_state.queued_prs, api_state.default_branch)
            for s in stacks:
                if any(p.number == pr_number for p in s.prs):
                    stack = s
                    break
            break

    if stack is None:
        # Single PR or stack not detected yet — just add this PR
        pr_data = client.get_pr(pr_number)
        stack_dicts = [{
            "number": pr_number,
            "head_sha": pr_data["head"]["sha"],
            "head_ref": pr_data["head"]["ref"],
            "base_ref": pr_data["base"]["ref"],
            "title": pr_data.get("title", ""),
        }]
    else:
        stack_dicts = [
            {"number": pr.number, "head_sha": pr.head_sha, "head_ref": pr.head_ref,
             "base_ref": pr.base_ref, "title": ""}
            for pr in stack.prs
        ]

    # Add to queue
    position = len(state.get("queue", [])) + 1
    entry = {
        "position": position,
        "queued_at": _now_iso(),
        "stack": stack_dicts,
        "deployment_id": None,
    }

    # Create deployment for live tracking
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

    # Comment on all PRs in the stack
    for pr in stack_dicts:
        client.create_comment(
            pr["number"],
            f"**Merge Queue:** Queued at position {position}.\n\n"
            f"[View merge queue →]({mq_url})",
        )

    log.info("Enqueued stack at position %d: %s", position, prs_desc)

    # Trigger processing if no active batch
    if state.get("active_batch") is None and not api_state.has_active_batch:
        return do_process(client)
    return "queued_waiting"


def do_process(client: GitHubClientProtocol) -> str:
    """Process the next batch from the queue."""
    store = StateStore(client)
    state = store.read()

    # Already processing?
    if state.get("active_batch") is not None:
        log.info("Active batch in progress, skipping")
        return "batch_active"

    # Nothing queued?
    queue = state.get("queue", [])
    if not queue:
        log.info("Queue empty, nothing to do")
        return "no_stacks"

    # Run pre-condition rules
    api_state = QueueState.fetch(client)
    results = rules_mod.check_all(api_state)
    failures = [r for r in results if not r.passed]
    if failures:
        for f in failures:
            log.error("Rule failed: %s — %s", f.name, f.message)
        return "rules_failed"

    # Pop first entry (FIFO)
    entry = queue.pop(0)
    # Re-number remaining
    for i, e in enumerate(queue):
        e["position"] = i + 1

    # Build stack from entry
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

    log.info("Processing stack: %s", " -> ".join(f"#{pr.number}" for pr in prs))

    # Update deployment: in_progress
    dep_id = entry.get("deployment_id")
    if dep_id:
        try:
            client.update_deployment_status(dep_id, "in_progress", "Locking branches...")
        except Exception as e:
            log.warning("Could not update deployment: %s", e)

    # Update state: active_batch
    state["active_batch"] = {
        "batch_id": "",
        "branch": "",
        "ruleset_id": None,
        "started_at": _now_iso(),
        "progress": "locking",
        "stack": entry["stack"],
        "deployment_id": dep_id,
    }
    state["updated_at"] = _now_iso()
    store.write(state)

    # Create batch
    try:
        batch = batch_mod.create_batch(client, next_stack)
    except batch_mod.BatchError as e:
        log.error("Failed to create batch: %s", e)
        mq_url = _mq_url(client)
        for pr in prs:
            client.create_comment(pr.number, f"**Merge Queue:** Failed — {e}.\n\n[View merge queue →]({mq_url})")
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

    # Update state with batch info
    state["active_batch"]["batch_id"] = batch.batch_id
    state["active_batch"]["branch"] = batch.branch
    state["active_batch"]["ruleset_id"] = batch.ruleset_id
    state["active_batch"]["progress"] = "running_ci"
    state["updated_at"] = _now_iso()
    store.write(state)

    # Update deployment
    if dep_id:
        try:
            client.update_deployment_status(dep_id, "in_progress", f"CI running on {batch.branch}")
        except Exception:
            pass

    # Post queued comments
    mq_url = _mq_url(client)
    pr_list = "\n".join(f"- #{pr.number} ({pr.head_ref})" for pr in prs)
    for pr in prs:
        client.create_comment(
            pr.number,
            f"**Merge Queue:** CI running on `{batch.branch}`.\n\n"
            f"Batch:\n{pr_list}\n\n[View merge queue →]({mq_url})",
        )

    # Run CI
    ci_passed = batch_mod.run_ci(client, batch)

    if ci_passed:
        state["active_batch"]["progress"] = "completing"
        state["updated_at"] = _now_iso()
        store.write(state)

        try:
            batch_mod.complete_batch(client, batch)
            log.info("Batch merged successfully!")
            status = "merged"
            if dep_id:
                prs_desc = ", ".join(f"#{pr.number}" for pr in prs)
                try:
                    client.update_deployment_status(dep_id, "success", f"Merged {prs_desc}")
                except Exception:
                    pass
        except batch_mod.BatchError as e:
            log.error("Failed to complete batch: %s", e)
            batch_mod.fail_batch(client, batch, str(e))
            status = "complete_error"
            if dep_id:
                try:
                    client.update_deployment_status(dep_id, "failure", str(e)[:140])
                except Exception:
                    pass
    else:
        batch_mod.fail_batch(client, batch, "CI failed")
        status = "ci_failed"
        if dep_id:
            try:
                client.update_deployment_status(dep_id, "failure", "CI failed")
            except Exception:
                pass

    # Move to history
    started = state["active_batch"]["started_at"]
    completed = _now_iso()
    try:
        dur = (
            datetime.datetime.fromisoformat(completed) -
            datetime.datetime.fromisoformat(started)
        ).total_seconds()
    except Exception:
        dur = 0

    state.setdefault("history", []).append({
        "batch_id": batch.batch_id,
        "status": status,
        "completed_at": completed,
        "prs": [pr.number for pr in prs],
        "duration_seconds": dur,
    })
    state["active_batch"] = None
    state["updated_at"] = _now_iso()
    store.write(state)

    # Process next if queue has more
    if state.get("queue"):
        log.info("More stacks queued, continuing...")
        return do_process(client)

    return status


def do_abort(client: GitHubClientProtocol, pr_number: int) -> str:
    """Abort active batch if PR is locked, or remove from queue."""
    store = StateStore(client)
    state = store.read()

    # Check if PR is in active batch
    active = state.get("active_batch")
    if active and any(pr["number"] == pr_number for pr in active.get("stack", [])):
        log.info("Aborting active batch for PR #%d", pr_number)
        batch_mod.abort_batch(client)

        dep_id = active.get("deployment_id")
        if dep_id:
            try:
                client.update_deployment_status(dep_id, "inactive", "Aborted by user")
            except Exception:
                pass

        state["active_batch"] = None
        state["updated_at"] = _now_iso()
        store.write(state)

        client.create_comment(
            pr_number,
            f"**Merge Queue:** Aborted — `queue` label was removed.\n\n"
            f"[View merge queue →]({_mq_url(client)})",
        )
        return "aborted"

    # Check if PR is in the queue (not yet processing)
    queue = state.get("queue", [])
    for i, entry in enumerate(queue):
        if any(pr["number"] == pr_number for pr in entry.get("stack", [])):
            removed = queue.pop(i)
            # Re-number
            for j, e in enumerate(queue):
                e["position"] = j + 1

            dep_id = removed.get("deployment_id")
            if dep_id:
                try:
                    client.update_deployment_status(dep_id, "inactive", "Removed from queue")
                except Exception:
                    pass

            state["updated_at"] = _now_iso()
            store.write(state)

            client.create_comment(
                pr_number,
                f"**Merge Queue:** Removed from queue.\n\n"
                f"[View merge queue →]({_mq_url(client)})",
            )
            return "removed"

    log.info("PR #%d not found in queue or active batch", pr_number)
    return "not_found"


def do_check_rules(client: GitHubClientProtocol) -> list[rules_mod.RuleResult]:
    """Run all rules."""
    state = QueueState.fetch(client)
    return rules_mod.check_all(state)


def do_status(client: GitHubClientProtocol) -> str:
    """Print current queue status."""
    store = StateStore(client)
    state = store.read()
    return render_status_terminal(state)


# --- CLI entry points ---


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
        level=logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    parser = argparse.ArgumentParser(prog="merge-queue")
    subparsers = parser.add_subparsers(dest="command", required=True)

    p_enqueue = subparsers.add_parser("enqueue")
    p_enqueue.add_argument("pr_number", type=int)
    p_enqueue.set_defaults(func=cmd_enqueue)

    p_process = subparsers.add_parser("process")
    p_process.set_defaults(func=cmd_process)

    p_abort = subparsers.add_parser("abort")
    p_abort.add_argument("pr_number", type=int)
    p_abort.set_defaults(func=cmd_abort)

    p_rules = subparsers.add_parser("check-rules")
    p_rules.set_defaults(func=cmd_check_rules)

    p_status = subparsers.add_parser("status")
    p_status.set_defaults(func=cmd_status)

    args = parser.parse_args()
    args.func(args)
