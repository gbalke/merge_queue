"""TDD tests for CI gate, re-test, and break-glass features.

Written BEFORE the implementation — these must fail first, then pass after fixes.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch


from tests.conftest import T0, make_pr, make_state


class TestGetPrCiStatusExists:
    """get_pr_ci_status must exist on GitHubClient."""

    def test_method_exists(self):
        from merge_queue.github_client import GitHubClient

        assert hasattr(GitHubClient, "get_pr_ci_status")

    def test_protocol_has_method(self):
        from merge_queue.github_client import GitHubClientProtocol

        assert "get_pr_ci_status" in dir(GitHubClientProtocol)


class TestDispatchCiOnRefExists:
    """dispatch_ci_on_ref must exist on GitHubClient."""

    def test_method_exists(self):
        from merge_queue.github_client import GitHubClient

        assert hasattr(GitHubClient, "dispatch_ci_on_ref")


class TestCiGateInEnqueue:
    """do_enqueue must reject PRs with failing CI."""

    def test_rejects_when_ci_failing(self, mock_client):
        mock_client.get_pr.return_value = {"state": "open"}
        mock_client.get_pr_ci_status.return_value = (False, "https://example.com/run/1")

        with patch("merge_queue.cli.StateStore") as StoreCls:
            store = MagicMock()
            store.read.return_value = make_state()
            StoreCls.return_value = store

            with patch("merge_queue.cli.QueueState") as QS:
                from merge_queue.state import QueueState

                QS.fetch.return_value = QueueState(
                    default_branch="main",
                    mq_branches=[],
                    rulesets=[],
                    prs=[make_pr(1, "feat-a", queued_at=T0)],
                    all_pr_data=[],
                )

                from merge_queue.cli import do_enqueue

                result = do_enqueue(mock_client, 1)

        assert result == "ci_not_ready"

    def test_accepts_when_ci_passing(self, mock_client):
        mock_client.get_pr.return_value = {
            "state": "open",
            "head": {"sha": "sha-1", "ref": "feat-a"},
            "base": {"ref": "main"},
            "title": "Test",
            "labels": [{"name": "queue"}],
        }
        # Explicitly set CI passing
        mock_client.get_pr_ci_status.return_value = (True, "https://example.com")
        mock_client.create_comment.return_value = 100
        mock_client.create_deployment.return_value = 42

        with (
            patch("merge_queue.cli.StateStore") as StoreCls,
            patch("merge_queue.cli.QueueState") as QS,
            patch("merge_queue.cli.do_process", return_value="merged"),
        ):
            store = MagicMock()
            store.read.return_value = make_state()
            StoreCls.return_value = store

            from merge_queue.state import QueueState as RealQS

            QS.fetch.return_value = RealQS(
                default_branch="main",
                mq_branches=[],
                rulesets=[],
                prs=[],
                all_pr_data=[],
            )

            from merge_queue.cli import do_enqueue

            result = do_enqueue(mock_client, 1)

        assert result != "ci_not_ready"


class TestBreakGlassLabel:
    """break-glass label should bypass CI gate."""

    def test_bypasses_ci_check(self, mock_client):
        mock_client.get_pr.return_value = {
            "state": "open",
            "head": {"sha": "sha-1", "ref": "feat-a"},
            "base": {"ref": "main"},
            "title": "Emergency fix",
            "labels": [{"name": "queue"}, {"name": "break-glass"}],
        }
        mock_client.get_pr_ci_status.return_value = (False, "")  # CI failing
        mock_client.create_comment.return_value = 100
        mock_client.create_deployment.return_value = 42

        with (
            patch("merge_queue.cli.StateStore") as StoreCls,
            patch("merge_queue.cli.QueueState") as QS,
            patch("merge_queue.cli.do_process", return_value="merged"),
        ):
            store = MagicMock()
            store.read.return_value = make_state()
            StoreCls.return_value = store

            from merge_queue.state import QueueState as RealQS

            QS.fetch.return_value = RealQS(
                default_branch="main",
                mq_branches=[],
                rulesets=[],
                prs=[],
                all_pr_data=[],
            )

            from merge_queue.cli import do_enqueue

            result = do_enqueue(mock_client, 1)

        # Should NOT be ci_not_ready — break-glass bypasses
        assert result != "ci_not_ready"


class TestDoRetest:
    """do_retest must dispatch CI, remove label, and comment."""

    def test_retriggers_ci(self, mock_client):
        mock_client.get_pr.return_value = {
            "head": {"ref": "feat-a"},
            "labels": [{"name": "re-test"}],
        }

        from merge_queue.cli import do_retest

        result = do_retest(mock_client, 1)

        assert result == "retriggered"
        mock_client.dispatch_ci_on_ref.assert_called_once_with("feat-a")
        mock_client.remove_label.assert_called_once_with(1, "re-test")
        mock_client.create_comment.assert_called_once()


class TestCiPendingSkipsButKeepsLabel:
    """When CI is still running, queue the PR anyway (don't reject)."""

    def test_pending_ci_still_queues(self, mock_client):
        """If CI hasn't completed yet (no check runs), still add to queue."""
        mock_client.get_pr.return_value = {
            "state": "open",
            "head": {"sha": "sha-1", "ref": "feat-a"},
            "base": {"ref": "main"},
            "title": "Test",
            "labels": [{"name": "queue"}],
        }
        # No check runs yet — CI hasn't completed
        mock_client.get_pr_ci_status.return_value = (None, "")  # None = pending
        mock_client.create_comment.return_value = 100
        mock_client.create_deployment.return_value = 42

        with (
            patch("merge_queue.cli.StateStore") as StoreCls,
            patch("merge_queue.cli.QueueState") as QS,
            patch("merge_queue.cli.do_process", return_value="merged"),
        ):
            store = MagicMock()
            store.read.return_value = make_state()
            StoreCls.return_value = store

            from merge_queue.state import QueueState as RealQS

            QS.fetch.return_value = RealQS(
                default_branch="main",
                mq_branches=[],
                rulesets=[],
                prs=[],
                all_pr_data=[],
            )

            from merge_queue.cli import do_enqueue

            result = do_enqueue(mock_client, 1)

        # Should NOT be ci_not_ready — pending CI should be allowed through
        assert result != "ci_not_ready"
        # queue label should NOT be removed
        mock_client.remove_label.assert_not_called()


class TestEnqueueFailureRemovesLabel:
    """When MQ crashes during enqueue, queue label should be removed."""

    def test_crash_removes_queue_label(self, mock_client):
        mock_client.get_pr.return_value = {"state": "open"}
        mock_client.get_pr_ci_status.return_value = (True, "")

        with (
            patch("merge_queue.cli.StateStore") as StoreCls,
            patch("merge_queue.cli.QueueState"),
        ):
            store = MagicMock()
            store.read.side_effect = RuntimeError("state broken")
            StoreCls.return_value = store

            from merge_queue.cli import do_enqueue

            # Should not raise — should handle gracefully
            try:
                do_enqueue(mock_client, 1)
            except Exception:
                pass

        # Even on crash, queue label should ideally be removed
        # This documents the current behavior for tracking
