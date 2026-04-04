"""Tests for rules.py — each rule with mocked state."""

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

from tests.conftest import make_pr_data

T0 = datetime.datetime(2026, 1, 1, 0, 0, tzinfo=datetime.timezone.utc)
T1 = datetime.datetime(2026, 1, 1, 0, 1, tzinfo=datetime.timezone.utc)


class TestSingleActiveBatch:
    def test_no_branches(self, mock_client):
        result = single_active_batch(mock_client)
        assert result.passed

    def test_one_branch(self, mock_client):
        mock_client.list_mq_branches.return_value = ["mq/123"]
        result = single_active_batch(mock_client)
        assert result.passed

    def test_two_branches_fails(self, mock_client):
        mock_client.list_mq_branches.return_value = ["mq/123", "mq/456"]
        result = single_active_batch(mock_client)
        assert not result.passed
        assert "2 mq/ branches" in result.message


class TestLockedPrsHaveRulesets:
    def test_no_locked_prs(self, mock_client):
        result = locked_prs_have_rulesets(mock_client)
        assert result.passed

    def test_locked_with_matching_ruleset(self, mock_client):
        mock_client.list_open_prs.return_value = [
            make_pr_data(1, "feat-a", labels=["locked", "queue"]),
        ]
        mock_client.list_rulesets.return_value = [{
            "name": "mq-lock-123",
            "conditions": {"ref_name": {"include": ["refs/heads/feat-a"], "exclude": []}},
        }]
        result = locked_prs_have_rulesets(mock_client)
        assert result.passed

    def test_locked_without_ruleset_fails(self, mock_client):
        mock_client.list_open_prs.return_value = [
            make_pr_data(1, "feat-a", labels=["locked", "queue"]),
        ]
        mock_client.list_rulesets.return_value = []
        result = locked_prs_have_rulesets(mock_client)
        assert not result.passed
        assert "#1" in result.message


class TestNoOrphanedLocks:
    def test_no_locks_no_branches(self, mock_client):
        result = no_orphaned_locks(mock_client)
        assert result.passed

    def test_locks_with_active_batch(self, mock_client):
        mock_client.list_mq_branches.return_value = ["mq/123"]
        result = no_orphaned_locks(mock_client)
        assert result.passed

    def test_locks_without_batch_fails(self, mock_client):
        mock_client.list_open_prs.return_value = [
            make_pr_data(1, "feat-a", labels=["locked"]),
        ]
        result = no_orphaned_locks(mock_client)
        assert not result.passed
        assert "1" in result.message


class TestQueueOrderIsFifo:
    def test_no_active_batch(self, mock_client):
        result = queue_order_is_fifo(mock_client)
        assert result.passed

    def test_correct_order(self, mock_client):
        mock_client.list_mq_branches.return_value = ["mq/123"]
        mock_client.list_open_prs.return_value = [
            make_pr_data(1, "feat-a", labels=["locked"]),
            make_pr_data(2, "fix-x", labels=["queue"]),
        ]
        # Locked PR was queued first
        mock_client.get_label_timestamp.side_effect = lambda n, l: T0 if n == 1 else T1
        result = queue_order_is_fifo(mock_client)
        assert result.passed

    def test_wrong_order_fails(self, mock_client):
        mock_client.list_mq_branches.return_value = ["mq/123"]
        mock_client.list_open_prs.return_value = [
            make_pr_data(1, "feat-a", labels=["locked"]),
            make_pr_data(2, "fix-x", labels=["queue"]),
        ]
        # Waiting PR was queued BEFORE the active batch
        mock_client.get_label_timestamp.side_effect = lambda n, l: T1 if n == 1 else T0
        result = queue_order_is_fifo(mock_client)
        assert not result.passed


class TestStackIntegrity:
    def test_valid_single_pr(self, mock_client):
        mock_client.list_open_prs.return_value = [
            make_pr_data(1, "feat-a", "main", labels=["queue"]),
        ]
        result = stack_integrity(mock_client)
        assert result.passed

    def test_valid_chain(self, mock_client):
        mock_client.list_open_prs.return_value = [
            make_pr_data(1, "feat-a", "main", labels=["queue"]),
            make_pr_data(2, "feat-b", "feat-a", labels=["queue"]),
        ]
        result = stack_integrity(mock_client)
        assert result.passed

    def test_no_queued_prs(self, mock_client):
        mock_client.list_open_prs.return_value = [
            make_pr_data(1, "feat-a", "main", labels=[]),
        ]
        result = stack_integrity(mock_client)
        assert result.passed


class TestCheckAll:
    def test_all_pass_clean_state(self, mock_client):
        results = check_all(mock_client)
        assert all(r.passed for r in results)
        assert len(results) == 5
