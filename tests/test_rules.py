"""Tests for rules.py — each rule with QueueState snapshots."""

from __future__ import annotations

import datetime

from merge_queue.rules import (
    check_all,
    locked_prs_have_rulesets,
    no_orphaned_locks,
    queue_order_is_fifo,
    single_active_batch,
    stack_integrity,
)
from merge_queue.state import QueueState
from merge_queue.types import PullRequest

T0 = datetime.datetime(2026, 1, 1, 0, 0, tzinfo=datetime.timezone.utc)
T1 = datetime.datetime(2026, 1, 1, 0, 1, tzinfo=datetime.timezone.utc)


def _pr(number, head_ref, base_ref="main", labels=("queue",), queued_at=T0):
    return PullRequest(number, f"sha-{number}", head_ref, base_ref, tuple(labels), queued_at)


def _state(prs=None, mq_branches=None, rulesets=None):
    return QueueState(
        default_branch="main",
        mq_branches=mq_branches or [],
        rulesets=rulesets or [],
        prs=prs or [],
        all_pr_data=[],
    )


class TestSingleActiveBatch:
    def test_no_branches(self):
        assert single_active_batch(_state()).passed

    def test_one_branch(self):
        assert single_active_batch(_state(mq_branches=["mq/123"])).passed

    def test_two_branches_fails(self):
        result = single_active_batch(_state(mq_branches=["mq/1", "mq/2"]))
        assert not result.passed
        assert "2 mq/ branches" in result.message


class TestLockedPrsHaveRulesets:
    def test_no_locked_prs(self):
        assert locked_prs_have_rulesets(_state()).passed

    def test_locked_with_matching_ruleset(self):
        state = _state(
            prs=[_pr(1, "feat-a", labels=("locked", "queue"))],
            rulesets=[{
                "name": "mq-lock-123",
                "conditions": {"ref_name": {"include": ["refs/heads/feat-a"], "exclude": []}},
            }],
        )
        assert locked_prs_have_rulesets(state).passed

    def test_locked_without_ruleset_fails(self):
        state = _state(prs=[_pr(1, "feat-a", labels=("locked", "queue"))])
        result = locked_prs_have_rulesets(state)
        assert not result.passed
        assert "#1" in result.message


class TestNoOrphanedLocks:
    def test_no_locks_no_branches(self):
        assert no_orphaned_locks(_state()).passed

    def test_locks_with_active_batch(self):
        state = _state(
            prs=[_pr(1, "feat-a", labels=("locked",))],
            mq_branches=["mq/123"],
        )
        assert no_orphaned_locks(state).passed

    def test_locks_without_batch_fails(self):
        state = _state(prs=[_pr(1, "feat-a", labels=("locked",))])
        result = no_orphaned_locks(state)
        assert not result.passed


class TestQueueOrderIsFifo:
    def test_no_active_batch(self):
        assert queue_order_is_fifo(_state()).passed

    def test_correct_order(self):
        state = _state(
            prs=[
                _pr(1, "feat-a", labels=("locked",), queued_at=T0),
                _pr(2, "fix-x", labels=("queue",), queued_at=T1),
            ],
            mq_branches=["mq/123"],
        )
        assert queue_order_is_fifo(state).passed

    def test_wrong_order_fails(self):
        state = _state(
            prs=[
                _pr(1, "feat-a", labels=("locked",), queued_at=T1),
                _pr(2, "fix-x", labels=("queue",), queued_at=T0),
            ],
            mq_branches=["mq/123"],
        )
        result = queue_order_is_fifo(state)
        assert not result.passed


class TestStackIntegrity:
    def test_valid_single_pr(self):
        state = _state(prs=[_pr(1, "feat-a")])
        assert stack_integrity(state).passed

    def test_valid_chain(self):
        state = _state(prs=[
            _pr(1, "feat-a"),
            _pr(2, "feat-b", "feat-a"),
        ])
        assert stack_integrity(state).passed

    def test_no_queued_prs(self):
        assert stack_integrity(_state()).passed


class TestCheckAll:
    def test_all_pass_clean_state(self):
        results = check_all(_state())
        assert all(r.passed for r in results)
        assert len(results) == 5
