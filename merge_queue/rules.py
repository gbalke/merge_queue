"""Invariant rules for the merge queue.

Each rule operates on a QueueState snapshot — no API calls.
"""

from __future__ import annotations

from merge_queue.queue import detect_stacks
from merge_queue.state import QueueState
from merge_queue.types import RuleResult


def single_active_batch(state: QueueState) -> RuleResult:
    """At most one mq/* branch should exist at any time."""
    n = len(state.mq_branches)
    if n <= 1:
        return RuleResult("single_active_batch", True, f"{n} mq/ branch(es)")
    return RuleResult(
        "single_active_batch",
        False,
        f"Found {n} mq/ branches: {', '.join(state.mq_branches)}. Expected at most 1.",
    )


def locked_prs_have_rulesets(state: QueueState) -> RuleResult:
    """Every PR with 'locked' label should have its branch covered by an mq-lock ruleset."""
    locked = state.locked_prs
    if not locked:
        return RuleResult("locked_prs_have_rulesets", True, "No locked PRs")

    mq_rulesets = [rs for rs in state.rulesets if rs.get("name", "").startswith("mq-lock-")]
    covered: set[str] = set()
    for rs in mq_rulesets:
        conditions = rs.get("conditions", {}).get("ref_name", {})
        covered.update(conditions.get("include", []))

    uncovered = []
    for pr in locked:
        expected = f"refs/heads/{pr.head_ref}"
        if expected not in covered:
            uncovered.append(f"#{pr.number} ({pr.head_ref})")

    if uncovered:
        return RuleResult(
            "locked_prs_have_rulesets",
            False,
            f"Locked PRs without matching ruleset: {', '.join(uncovered)}",
        )
    return RuleResult("locked_prs_have_rulesets", True, "All locked PRs have rulesets")


def no_orphaned_locks(state: QueueState) -> RuleResult:
    """No PRs should have 'locked' label when no mq/ branch exists."""
    if state.has_active_batch:
        return RuleResult("no_orphaned_locks", True, "Active batch exists")

    locked = state.locked_prs
    if locked:
        nums = [pr.number for pr in locked]
        return RuleResult(
            "no_orphaned_locks",
            False,
            f"PRs with 'locked' label but no active batch: {nums}",
        )
    return RuleResult("no_orphaned_locks", True, "No orphaned locks")


def queue_order_is_fifo(state: QueueState) -> RuleResult:
    """If an active batch exists, it should contain the earliest-queued stack."""
    if not state.has_active_batch:
        return RuleResult("queue_order_is_fifo", True, "No active batch")

    locked_times = [pr.queued_at for pr in state.locked_prs if pr.queued_at]
    queued_times = [pr.queued_at for pr in state.queued_prs if pr.queued_at]

    if locked_times and queued_times:
        batch_time = min(locked_times)
        earliest_waiting = min(queued_times)
        if earliest_waiting < batch_time:
            return RuleResult(
                "queue_order_is_fifo",
                False,
                f"Active batch queued at {batch_time}, but earlier stack waiting since {earliest_waiting}",
            )

    return RuleResult("queue_order_is_fifo", True, "FIFO order correct")


def stack_integrity(state: QueueState) -> RuleResult:
    """Each detected stack should form a valid chain ending at the default branch."""
    queued = [pr for pr in state.prs if "queue" in pr.labels]
    stacks = detect_stacks(queued, state.default_branch)

    issues = []
    for stack in stacks:
        if not stack.prs:
            continue
        if stack.prs[0].base_ref != state.default_branch:
            issues.append(
                f"Stack starting at #{stack.prs[0].number} doesn't target {state.default_branch}"
            )
        for i in range(1, len(stack.prs)):
            if stack.prs[i].base_ref != stack.prs[i - 1].head_ref:
                issues.append(
                    f"PR #{stack.prs[i].number} targets {stack.prs[i].base_ref}, "
                    f"expected {stack.prs[i - 1].head_ref}"
                )

    if issues:
        return RuleResult("stack_integrity", False, "; ".join(issues))
    return RuleResult("stack_integrity", True, f"{len(stacks)} stack(s) valid")


ALL_RULES = [
    single_active_batch,
    locked_prs_have_rulesets,
    no_orphaned_locks,
    queue_order_is_fifo,
    stack_integrity,
]


def check_all(state: QueueState) -> list[RuleResult]:
    """Run all rules against a state snapshot."""
    return [rule(state) for rule in ALL_RULES]
