"""Tests for queue.py — pure logic, no mocks needed."""

from __future__ import annotations

import datetime

import pytest

from merge_queue.queue import (
    build_pr_graph,
    detect_stacks,
    find_stack_for_pr,
    order_queue,
    select_next,
    validate_contiguous,
)
from merge_queue.types import PullRequest, Stack


def _pr(
    number: int,
    head_ref: str,
    base_ref: str = "main",
    queued_at: datetime.datetime | None = None,
    labels: tuple[str, ...] = ("queue",),
    head_sha: str = "",
) -> PullRequest:
    return PullRequest(
        number=number,
        head_sha=head_sha or f"sha-{number}",
        head_ref=head_ref,
        base_ref=base_ref,
        labels=labels,
        queued_at=queued_at,
    )


T0 = datetime.datetime(2026, 1, 1, 0, 0, 0, tzinfo=datetime.timezone.utc)
T1 = datetime.datetime(2026, 1, 1, 0, 1, 0, tzinfo=datetime.timezone.utc)
T2 = datetime.datetime(2026, 1, 1, 0, 2, 0, tzinfo=datetime.timezone.utc)
T3 = datetime.datetime(2026, 1, 1, 0, 3, 0, tzinfo=datetime.timezone.utc)


class TestBuildPrGraph:
    def test_empty(self):
        by_head, by_base = build_pr_graph([])
        assert by_head == {}
        assert by_base == {}

    def test_single_pr(self):
        pr = _pr(1, "feat-a")
        by_head, by_base = build_pr_graph([pr])
        assert by_head == {"feat-a": pr}
        assert by_base == {"main": [pr]}

    def test_stacked_prs(self):
        a = _pr(1, "feat-a", "main")
        b = _pr(2, "feat-b", "feat-a")
        by_head, by_base = build_pr_graph([a, b])
        assert by_head["feat-a"] == a
        assert by_head["feat-b"] == b
        assert by_base["main"] == [a]
        assert by_base["feat-a"] == [b]


class TestDetectStacks:
    def test_empty(self):
        assert detect_stacks([]) == []

    def test_no_queued_prs(self):
        pr = _pr(1, "feat-a", labels=())
        assert detect_stacks([pr]) == []

    def test_single_pr(self):
        pr = _pr(1, "feat-a", queued_at=T0)
        stacks = detect_stacks([pr])
        assert len(stacks) == 1
        assert stacks[0].prs == (pr,)
        assert stacks[0].queued_at == T0

    def test_linear_stack_of_3(self):
        a = _pr(1, "feat-a", "main", queued_at=T0)
        b = _pr(2, "feat-b", "feat-a", queued_at=T1)
        c = _pr(3, "feat-c", "feat-b", queued_at=T2)
        stacks = detect_stacks([a, b, c])
        assert len(stacks) == 1
        assert stacks[0].prs == (a, b, c)
        assert stacks[0].queued_at == T0

    def test_two_independent_stacks(self):
        a = _pr(1, "feat-a", "main", queued_at=T1)
        x = _pr(2, "fix-x", "main", queued_at=T0)
        stacks = detect_stacks([a, x])
        assert len(stacks) == 2
        # FIFO: x was queued first
        assert stacks[0].prs == (x,)
        assert stacks[1].prs == (a,)

    def test_stack_uses_earliest_queued_at(self):
        a = _pr(1, "feat-a", "main", queued_at=T2)
        b = _pr(2, "feat-b", "feat-a", queued_at=T0)
        stacks = detect_stacks([a, b])
        assert len(stacks) == 1
        assert stacks[0].queued_at == T0  # min of T0, T2

    def test_non_contiguous_labels_stops_at_gap(self):
        """If A is queued but B (in middle) is not, C is unreachable."""
        a = _pr(1, "feat-a", "main", queued_at=T0)
        b = _pr(2, "feat-b", "feat-a", labels=())  # not queued
        c = _pr(3, "feat-c", "feat-b", queued_at=T1)
        stacks = detect_stacks([a, b, c])
        # Only A should be in a stack (B breaks the chain since it's not queued)
        assert len(stacks) == 1
        assert stacks[0].prs == (a,)

    def test_pr_not_targeting_default_branch_excluded(self):
        """PR targeting a non-existent base (orphaned) is not in any stack."""
        pr = _pr(1, "feat-a", "some-other-branch", queued_at=T0)
        stacks = detect_stacks([pr])
        assert len(stacks) == 0

    def test_mixed_queued_and_unqueued(self):
        a = _pr(1, "feat-a", "main", queued_at=T0)
        b = _pr(2, "feat-b", "feat-a", queued_at=T1)
        # c is in the stack structure but not queued
        c = _pr(3, "feat-c", "feat-b", labels=())
        stacks = detect_stacks([a, b, c])
        assert len(stacks) == 1
        assert stacks[0].prs == (a, b)

    def test_custom_default_branch(self):
        a = _pr(1, "feat-a", "develop", queued_at=T0)
        stacks = detect_stacks([a], default_branch="develop")
        assert len(stacks) == 1

    def test_cycle_detection(self):
        """A cycle in the graph should not cause infinite loop."""
        # a -> main, b -> a, but b is already seen (simulated by duplicate)
        a = _pr(1, "feat-a", "main", queued_at=T0)
        # Create a scenario where walking could revisit a node
        # b targets a, c targets b, but if we had a->c somehow it'd cycle
        b = _pr(2, "feat-b", "feat-a", queued_at=T1)
        stacks = detect_stacks([a, b])
        assert len(stacks) == 1
        assert len(stacks[0].prs) == 2

    def test_pr_with_no_queued_at_skipped(self):
        """Stack where all PRs have queued_at=None should be excluded."""
        a = _pr(1, "feat-a", "main", queued_at=None)
        stacks = detect_stacks([a])
        assert len(stacks) == 0

    def test_two_stacks_fifo_order(self):
        """Stacks are returned in FIFO order by queued_at."""
        a = _pr(1, "feat-a", "main", queued_at=T2)
        b = _pr(2, "feat-b", "feat-a", queued_at=T3)
        x = _pr(3, "fix-x", "main", queued_at=T0)
        y = _pr(4, "fix-y", "fix-x", queued_at=T1)
        stacks = detect_stacks([a, b, x, y])
        assert len(stacks) == 2
        assert stacks[0].prs[0].number == 3  # fix-x stack first (T0)
        assert stacks[1].prs[0].number == 1  # feat-a stack second (T2)


class TestOrderQueue:
    def test_sorts_by_queued_at(self):
        s1 = Stack(prs=(), queued_at=T2)
        s2 = Stack(prs=(), queued_at=T0)
        s3 = Stack(prs=(), queued_at=T1)
        assert order_queue([s1, s2, s3]) == [s2, s3, s1]


class TestSelectNext:
    def test_empty(self):
        assert select_next([]) is None

    def test_returns_first(self):
        s1 = Stack(prs=(), queued_at=T0)
        s2 = Stack(prs=(), queued_at=T1)
        assert select_next([s1, s2]) is s1


class TestValidateContiguous:
    def test_empty_stack(self):
        s = Stack(prs=(), queued_at=T0)
        valid, msg = validate_contiguous(s)
        assert not valid
        assert "empty" in msg

    def test_single_pr_targeting_main(self):
        pr = _pr(1, "feat-a", "main")
        s = Stack(prs=(pr,), queued_at=T0)
        valid, msg = validate_contiguous(s)
        assert valid

    def test_single_pr_not_targeting_main(self):
        pr = _pr(1, "feat-a", "develop")
        s = Stack(prs=(pr,), queued_at=T0)
        valid, msg = validate_contiguous(s)
        assert not valid
        assert "develop" in msg

    def test_valid_chain(self):
        a = _pr(1, "feat-a", "main")
        b = _pr(2, "feat-b", "feat-a")
        c = _pr(3, "feat-c", "feat-b")
        s = Stack(prs=(a, b, c), queued_at=T0)
        valid, msg = validate_contiguous(s)
        assert valid

    def test_broken_chain(self):
        a = _pr(1, "feat-a", "main")
        c = _pr(3, "feat-c", "feat-b")  # skips feat-b
        s = Stack(prs=(a, c), queued_at=T0)
        valid, msg = validate_contiguous(s)
        assert not valid
        assert "feat-b" in msg

    def test_custom_default_branch(self):
        a = _pr(1, "feat-a", "develop")
        s = Stack(prs=(a,), queued_at=T0)
        valid, _ = validate_contiguous(s, default_branch="develop")
        assert valid


class TestFindStackForPr:
    def test_found(self):
        a = _pr(1, "feat-a", "main", queued_at=T0)
        b = _pr(2, "feat-b", "feat-a", queued_at=T1)
        stack = find_stack_for_pr(2, [a, b])
        assert stack is not None
        assert len(stack.prs) == 2

    def test_not_found(self):
        a = _pr(1, "feat-a", "main", queued_at=T0)
        assert find_stack_for_pr(99, [a]) is None

    def test_finds_in_correct_stack(self):
        a = _pr(1, "feat-a", "main", queued_at=T0)
        x = _pr(2, "fix-x", "main", queued_at=T1)
        stack = find_stack_for_pr(2, [a, x])
        assert stack is not None
        assert stack.prs[0].number == 2
