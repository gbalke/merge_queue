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


def do_process(client: GitHubClientProtocol, state: QueueState | None = None) -> str:
    """Core processing logic. Returns a status string.

    If state is not provided, fetches it (1 round of API calls).
    """
    if state is None:
        state = QueueState.fetch(client)

    if state.has_active_batch:
        log.info("Active batch found: %s. Skipping.", state.mq_branches[0])
        return "batch_active"

    # Run pre-condition rules (zero API calls — operates on snapshot)
    results = rules_mod.check_all(state)
    failures = [r for r in results if not r.passed]
    if failures:
        for f in failures:
            log.error("Rule failed: %s — %s", f.name, f.message)
        return "rules_failed"

    # Find next stack (zero API calls — operates on snapshot)
    stacks = detect_stacks(state.queued_prs, state.default_branch)
    ordered = order_queue(stacks)
    next_stack = select_next(ordered)

    if next_stack is None:
        log.info("No stacks queued. Nothing to do.")
        return "no_stacks"

    log.info(
        "Processing stack: %s",
        " -> ".join(f"#{pr.number}" for pr in next_stack.prs),
    )

    # Create batch (API calls: lock + git + labels)
    try:
        batch = batch_mod.create_batch(client, next_stack)
    except batch_mod.BatchError as e:
        log.error("Failed to create batch: %s", e)
        for pr in next_stack.prs:
            client.create_comment(
                pr.number,
                f"**Merge Queue:** Failed to create batch — {e}. "
                "Fix the issue and re-add the `queue` label.",
            )
            client.remove_label(pr.number, "queue")
        return "batch_error"

    # Post queued comments
    pr_list = "\n".join(f"- #{pr.number} ({pr.head_ref})" for pr in next_stack.prs)
    for pr in next_stack.prs:
        client.create_comment(
            pr.number,
            f"**Merge Queue:** Queued in batch `{batch.branch}`. CI running. Branches locked.\n\n"
            f"Batch contents:\n{pr_list}",
        )

    # Run CI (API calls: dispatch + polling)
    ci_passed = batch_mod.run_ci(client, batch)

    if ci_passed:
        try:
            batch_mod.complete_batch(client, batch)
            log.info("Batch merged successfully!")
            status = "merged"
        except batch_mod.BatchError as e:
            log.error("Failed to complete batch: %s", e)
            batch_mod.fail_batch(client, batch, str(e))
            status = "complete_error"
    else:
        batch_mod.fail_batch(client, batch, "CI failed")
        status = "ci_failed"

    # Check for more queued stacks
    _dispatch_next_if_queued(client, state.default_branch)

    return status


def _dispatch_next_if_queued(
    client: GitHubClientProtocol, default_branch: str
) -> bool:
    """Check for more queued stacks and dispatch a processing run."""
    # Invalidate cache since we just modified state (merged/failed a batch)
    if hasattr(client, "invalidate_cache"):
        client.invalidate_cache()

    fresh_state = QueueState.fetch(client)
    if not fresh_state.queued_prs:
        return False

    stacks = detect_stacks(fresh_state.queued_prs, default_branch)
    if not stacks:
        return False

    log.info("More stacks queued. Dispatching next processing run.")
    try:
        base_url = getattr(client, "_base_url", "")
        session = getattr(client, "_session", None)
        if session and base_url:
            r = session.post(
                f"{base_url}/actions/workflows/merge-queue.yml/dispatches",
                json={"ref": default_branch, "inputs": {"command": "process"}},
            )
            r.raise_for_status()
            return True
    except Exception as e:
        log.warning("Could not dispatch next processing run: %s", e)
    return False


def do_enqueue(client: GitHubClientProtocol, pr_number: int) -> str:
    """Enqueue a PR. Returns status string."""
    log.info("Enqueuing PR #%d", pr_number)
    client.create_comment(
        pr_number,
        f"**Merge Queue:** PR queued at {datetime.datetime.now(datetime.timezone.utc).isoformat()}. "
        "Waiting for processor.",
    )

    state = QueueState.fetch(client)
    if state.has_active_batch:
        log.info("Batch already in progress (%s). PR will be processed next.", state.mq_branches[0])
        return "queued_waiting"

    return do_process(client, state=state)


def do_abort(client: GitHubClientProtocol, pr_number: int) -> str:
    """Abort active batch if PR is locked."""
    pr_data = client.get_pr(pr_number)
    labels = [l["name"] for l in pr_data.get("labels", [])]

    if "locked" not in labels:
        log.info("PR #%d is not in an active batch. Nothing to abort.", pr_number)
        return "not_locked"

    log.info("Aborting active batch due to queue label removal on PR #%d", pr_number)
    batch_mod.abort_batch(client)
    client.create_comment(
        pr_number,
        "**Merge Queue:** Aborted — `queue` label was removed. Branches unlocked.",
    )
    return "aborted"


def do_check_rules(client: GitHubClientProtocol) -> list[rules_mod.RuleResult]:
    """Run all rules."""
    state = QueueState.fetch(client)
    return rules_mod.check_all(state)


# --- CLI entry points (thin wrappers) ---


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

    args = parser.parse_args()
    args.func(args)
