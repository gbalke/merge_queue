"""Pure queue logic — stack detection, FIFO ordering, batch selection.

All functions operate on typed data only. No API calls, no side effects.
"""

from __future__ import annotations

from merge_queue.types import PullRequest, Stack


def build_pr_graph(
    prs: list[PullRequest],
) -> tuple[dict[str, PullRequest], dict[str, list[PullRequest]]]:
    """Build lookup maps from a list of PRs.

    Returns:
        by_head: head_ref -> PullRequest (the PR that owns this branch)
        by_base: base_ref -> [PullRequests targeting this base]
    """
    by_head: dict[str, PullRequest] = {}
    by_base: dict[str, list[PullRequest]] = {}
    for pr in prs:
        by_head[pr.head_ref] = pr
        by_base.setdefault(pr.base_ref, []).append(pr)
    return by_head, by_base


def detect_stacks(prs: list[PullRequest], default_branch: str = "main") -> list[Stack]:
    """Group PRs into stacks by following base_ref chains.

    A stack is a chain where the bottom PR targets default_branch and each
    subsequent PR targets the previous PR's head_ref.

    Only PRs with the 'queue' label are considered. PRs must form a
    contiguous chain from the bottom for the stack to be valid.

    Returns stacks sorted by queued_at (FIFO).
    """
    queued = [pr for pr in prs if "queue" in pr.labels]
    if not queued:
        return []

    by_head, by_base = build_pr_graph(queued)

    # Find root PRs (those targeting default_branch)
    roots = [pr for pr in queued if pr.base_ref == default_branch]

    stacks: list[Stack] = []
    seen: set[int] = set()

    for root in roots:
        chain: list[PullRequest] = [root]
        seen.add(root.number)
        cursor = root

        # Walk up the chain: find PRs whose base_ref == cursor.head_ref
        while cursor.head_ref in by_base:
            children = by_base[cursor.head_ref]
            # Pick the first child (stacks should be linear)
            child = children[0]
            if child.number in seen:
                break
            chain.append(child)
            seen.add(child.number)
            cursor = child

        queued_times = [pr.queued_at for pr in chain if pr.queued_at is not None]
        if not queued_times:
            continue

        stacks.append(Stack(prs=tuple(chain), queued_at=min(queued_times)))

    return order_queue(stacks)


def order_queue(stacks: list[Stack]) -> list[Stack]:
    """Sort stacks by queued_at ascending (FIFO)."""
    return sorted(stacks, key=lambda s: s.queued_at)


def select_next(stacks: list[Stack]) -> Stack | None:
    """Return the first (earliest-queued) stack, or None if empty."""
    if not stacks:
        return None
    return stacks[0]


def validate_contiguous(stack: Stack, default_branch: str = "main") -> tuple[bool, str]:
    """Check that a stack forms a valid contiguous chain to default_branch.

    Returns (valid, error_message).
    """
    if not stack.prs:
        return False, "Stack is empty"

    # Bottom PR must target default_branch
    if stack.prs[0].base_ref != default_branch:
        return False, (
            f"Bottom PR #{stack.prs[0].number} targets '{stack.prs[0].base_ref}', "
            f"not '{default_branch}'"
        )

    # Each subsequent PR must target the previous PR's head_ref
    for i in range(1, len(stack.prs)):
        expected_base = stack.prs[i - 1].head_ref
        actual_base = stack.prs[i].base_ref
        if actual_base != expected_base:
            return False, (
                f"PR #{stack.prs[i].number} targets '{actual_base}', "
                f"expected '{expected_base}'"
            )

    return True, ""


def find_stack_for_pr(
    pr_number: int, prs: list[PullRequest], default_branch: str = "main"
) -> Stack | None:
    """Find the stack containing a specific PR."""
    stacks = detect_stacks(prs, default_branch)
    for stack in stacks:
        if any(pr.number == pr_number for pr in stack.prs):
            return stack
    return None
