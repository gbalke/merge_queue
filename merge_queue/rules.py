"""Invariant rules for the merge queue.

Each rule checks a property that should always hold true.
Rules are run as pre/post conditions and via the check-rules CLI command.
"""

from __future__ import annotations

from merge_queue.github_client import GitHubClientProtocol
from merge_queue.queue import detect_stacks, order_queue
from merge_queue.types import PullRequest, RuleResult

import datetime


def single_active_batch(client: GitHubClientProtocol) -> RuleResult:
    """At most one mq/* branch should exist at any time."""
    branches = client.list_mq_branches()
    if len(branches) <= 1:
        return RuleResult("single_active_batch", True, f"{len(branches)} mq/ branch(es)")
    return RuleResult(
        "single_active_batch",
        False,
        f"Found {len(branches)} mq/ branches: {', '.join(branches)}. Expected at most 1.",
    )


def locked_prs_have_rulesets(client: GitHubClientProtocol) -> RuleResult:
    """Every PR with 'locked' label should have its branch covered by an mq-lock ruleset."""
    locked_prs = []
    for pr_data in client.list_open_prs():
        labels = [l["name"] for l in pr_data.get("labels", [])]
        if "locked" in labels:
            locked_prs.append(pr_data)

    if not locked_prs:
        return RuleResult("locked_prs_have_rulesets", True, "No locked PRs")

    rulesets = client.list_rulesets()
    mq_rulesets = [rs for rs in rulesets if rs.get("name", "").startswith("mq-lock-")]

    # Collect all branch patterns from mq-lock rulesets
    locked_patterns: set[str] = set()
    for rs in mq_rulesets:
        conditions = rs.get("conditions", {}).get("ref_name", {})
        for pattern in conditions.get("include", []):
            locked_patterns.add(pattern)

    uncovered = []
    for pr_data in locked_prs:
        head_ref = pr_data["head"]["ref"]
        expected = f"refs/heads/{head_ref}"
        if expected not in locked_patterns:
            uncovered.append(f"#{pr_data['number']} ({head_ref})")

    if uncovered:
        return RuleResult(
            "locked_prs_have_rulesets",
            False,
            f"Locked PRs without matching ruleset: {', '.join(uncovered)}",
        )
    return RuleResult("locked_prs_have_rulesets", True, "All locked PRs have rulesets")


def no_orphaned_locks(client: GitHubClientProtocol) -> RuleResult:
    """No PRs should have 'locked' label when no mq/ branch exists."""
    branches = client.list_mq_branches()
    if branches:
        return RuleResult("no_orphaned_locks", True, "Active batch exists")

    locked_prs = []
    for pr_data in client.list_open_prs():
        labels = [l["name"] for l in pr_data.get("labels", [])]
        if "locked" in labels:
            locked_prs.append(pr_data["number"])

    if locked_prs:
        return RuleResult(
            "no_orphaned_locks",
            False,
            f"PRs with 'locked' label but no active batch: {locked_prs}",
        )
    return RuleResult("no_orphaned_locks", True, "No orphaned locks")


def queue_order_is_fifo(client: GitHubClientProtocol) -> RuleResult:
    """If an active batch exists, it should contain the earliest-queued stack."""
    branches = client.list_mq_branches()
    if not branches:
        return RuleResult("queue_order_is_fifo", True, "No active batch")

    all_prs_data = client.list_open_prs()
    prs: list[PullRequest] = []
    for pr_data in all_prs_data:
        labels = tuple(l["name"] for l in pr_data.get("labels", []))
        if "queue" not in labels and "locked" not in labels:
            continue
        queued_at = client.get_label_timestamp(pr_data["number"], "queue")
        if queued_at is None:
            queued_at = client.get_label_timestamp(pr_data["number"], "locked")
        prs.append(PullRequest(
            number=pr_data["number"],
            head_sha=pr_data["head"]["sha"],
            head_ref=pr_data["head"]["ref"],
            base_ref=pr_data["base"]["ref"],
            labels=labels,
            queued_at=queued_at or datetime.datetime.now(datetime.timezone.utc),
        ))

    # The locked PRs are in the active batch
    locked_times = [pr.queued_at for pr in prs if "locked" in pr.labels and pr.queued_at]
    queued_times = [pr.queued_at for pr in prs if "queue" in pr.labels and "locked" not in pr.labels and pr.queued_at]

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


def stack_integrity(client: GitHubClientProtocol) -> RuleResult:
    """Each detected stack should form a valid chain ending at the default branch."""
    all_prs_data = client.list_open_prs()
    default_branch = client.get_default_branch()

    prs = []
    for pr_data in all_prs_data:
        labels = tuple(l["name"] for l in pr_data.get("labels", []))
        if "queue" not in labels:
            continue
        prs.append(PullRequest(
            number=pr_data["number"],
            head_sha=pr_data["head"]["sha"],
            head_ref=pr_data["head"]["ref"],
            base_ref=pr_data["base"]["ref"],
            labels=labels,
            queued_at=datetime.datetime.now(datetime.timezone.utc),
        ))

    stacks = detect_stacks(prs, default_branch)
    issues = []
    for stack in stacks:
        if not stack.prs:
            continue
        if stack.prs[0].base_ref != default_branch:
            issues.append(f"Stack starting at #{stack.prs[0].number} doesn't target {default_branch}")

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


def check_all(client: GitHubClientProtocol) -> list[RuleResult]:
    """Run all rules and return results."""
    return [rule(client) for rule in ALL_RULES]
