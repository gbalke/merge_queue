"""Tests for _sync_missing_prs — auto-enqueues PRs missed by cancelled workflow runs."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from merge_queue.cli import _sync_missing_prs
from merge_queue.types import empty_state

from .conftest import make_pr_data, make_queue_entry, make_state


def _make_client(open_prs: list[dict] | None = None) -> MagicMock:
    client = MagicMock()
    client.owner = "owner"
    client.repo = "repo"
    client.get_default_branch.return_value = "main"
    client.list_open_prs.return_value = open_prs or []
    client.create_deployment.return_value = 42
    client.create_comment.return_value = 999
    return client


def _make_store(state: dict) -> MagicMock:
    store = MagicMock()
    store.read.return_value = state
    return store


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _pr_with_queue_label(number: int, base_ref: str = "main") -> dict:
    return make_pr_data(number, f"feat-{number}", base_ref=base_ref, labels=["queue"])


def _pr_without_queue_label(number: int) -> dict:
    return make_pr_data(number, f"feat-{number}", labels=[])


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSyncMissingPrs:
    def test_pr_with_queue_label_not_in_state_gets_enqueued(self):
        """A PR that has the queue label but is absent from state gets added."""
        state = empty_state()
        client = _make_client(open_prs=[_pr_with_queue_label(10)])
        store = _make_store(state)

        with patch("merge_queue.config.get_target_branches", return_value=["main"]):
            result = _sync_missing_prs(client, state, store)

        assert len(result["queue"]) == 1
        entry = result["queue"][0]
        assert entry["stack"][0]["number"] == 10
        assert entry["position"] == 1
        assert entry["target_branch"] == "main"
        store.write.assert_called_once()

    def test_pr_already_in_queue_not_duplicated(self):
        """A PR already present in the queue is not added again."""
        existing_entry = make_queue_entry(10)
        state = make_state(queue=[existing_entry])
        client = _make_client(open_prs=[_pr_with_queue_label(10)])
        store = _make_store(state)

        with patch("merge_queue.config.get_target_branches", return_value=["main"]):
            result = _sync_missing_prs(client, state, store)

        assert len(result["queue"]) == 1
        store.write.assert_not_called()

    def test_pr_in_active_batch_not_duplicated(self):
        """A PR in the active batch is not added to the queue."""
        active_batch = {
            "stack": [
                {
                    "number": 10,
                    "head_sha": "sha",
                    "head_ref": "feat-10",
                    "base_ref": "main",
                }
            ],
            "started_at": "2026-01-01T00:00:00+00:00",
        }
        state = make_state(active_batch=active_batch)
        client = _make_client(open_prs=[_pr_with_queue_label(10)])
        store = _make_store(state)

        with patch("merge_queue.config.get_target_branches", return_value=["main"]):
            result = _sync_missing_prs(client, state, store)

        assert result["queue"] == []
        store.write.assert_not_called()

    def test_pr_without_queue_label_ignored(self):
        """A PR that does not carry the queue label is not enqueued."""
        state = empty_state()
        client = _make_client(open_prs=[_pr_without_queue_label(20)])
        store = _make_store(state)

        with patch("merge_queue.config.get_target_branches", return_value=["main"]):
            result = _sync_missing_prs(client, state, store)

        assert result["queue"] == []
        store.write.assert_not_called()

    def test_multiple_missing_prs_all_enqueued(self):
        """When several PRs are missing, every one of them gets enqueued."""
        state = empty_state()
        open_prs = [
            _pr_with_queue_label(1),
            _pr_with_queue_label(2),
            _pr_with_queue_label(3),
        ]
        client = _make_client(open_prs=open_prs)
        store = _make_store(state)

        with patch("merge_queue.config.get_target_branches", return_value=["main"]):
            result = _sync_missing_prs(client, state, store)

        assert len(result["queue"]) == 3
        numbers = {e["stack"][0]["number"] for e in result["queue"]}
        assert numbers == {1, 2, 3}
        # Positions are assigned sequentially
        positions = [e["position"] for e in result["queue"]]
        assert positions == [1, 2, 3]
        store.write.assert_called_once()

    def test_deployment_created_for_missing_pr(self):
        """create_deployment and update_deployment_status are called for each missing PR."""
        state = empty_state()
        client = _make_client(open_prs=[_pr_with_queue_label(5)])
        store = _make_store(state)

        with patch("merge_queue.config.get_target_branches", return_value=["main"]):
            result = _sync_missing_prs(client, state, store)

        client.create_deployment.assert_called_once()
        client.update_deployment_status.assert_called_once_with(
            42, "queued", "Position 1"
        )
        assert result["queue"][0]["deployment_id"] == 42

    def test_deployment_failure_does_not_abort_enqueue(self):
        """If deployment creation raises, the PR is still enqueued with deployment_id=None."""
        state = empty_state()
        client = _make_client(open_prs=[_pr_with_queue_label(7)])
        client.create_deployment.side_effect = RuntimeError("API error")
        store = _make_store(state)

        with patch("merge_queue.config.get_target_branches", return_value=["main"]):
            result = _sync_missing_prs(client, state, store)

        assert len(result["queue"]) == 1
        assert result["queue"][0]["deployment_id"] is None

    def test_comment_id_stored_on_success(self):
        """The comment ID returned by create_comment is persisted in comment_ids."""
        state = empty_state()
        client = _make_client(open_prs=[_pr_with_queue_label(8)])
        client.create_comment.return_value = 1234
        store = _make_store(state)

        with patch("merge_queue.config.get_target_branches", return_value=["main"]):
            result = _sync_missing_prs(client, state, store)

        cids = result["queue"][0]["comment_ids"]
        assert cids.get(8) == 1234

    def test_non_default_base_ref_uses_matching_target_branch(self):
        """When a PR's base ref matches a configured target branch, that branch is used."""
        state = empty_state()
        pr = make_pr_data(11, "feat-11", base_ref="release/1.0", labels=["queue"])
        client = _make_client(open_prs=[pr])
        store = _make_store(state)

        with patch(
            "merge_queue.config.get_target_branches",
            return_value=["main", "release/1.0"],
        ):
            result = _sync_missing_prs(client, state, store)

        assert result["queue"][0]["target_branch"] == "release/1.0"

    def test_unknown_base_ref_falls_back_to_default_branch(self):
        """When the PR's base ref is not a configured target, we fall back to default."""
        state = empty_state()
        pr = make_pr_data(
            12, "feat-12", base_ref="some-feature-branch", labels=["queue"]
        )
        client = _make_client(open_prs=[pr])
        store = _make_store(state)

        with patch("merge_queue.config.get_target_branches", return_value=["main"]):
            result = _sync_missing_prs(client, state, store)

        client.get_default_branch.assert_called()
        assert result["queue"][0]["target_branch"] == "main"

    def test_no_open_prs_returns_state_unchanged(self):
        """When there are no open PRs at all, state is returned unchanged."""
        state = empty_state()
        client = _make_client(open_prs=[])
        store = _make_store(state)

        with patch("merge_queue.config.get_target_branches", return_value=["main"]):
            result = _sync_missing_prs(client, state, store)

        assert result["queue"] == []
        store.write.assert_not_called()

    def test_mixed_prs_only_missing_ones_enqueued(self):
        """PRs already in the queue are skipped; only genuinely missing ones are added."""
        existing_entry = make_queue_entry(1)
        state = make_state(queue=[existing_entry])
        open_prs = [
            _pr_with_queue_label(1),  # already queued
            _pr_with_queue_label(2),  # missing
            _pr_without_queue_label(3),  # no label
        ]
        client = _make_client(open_prs=open_prs)
        store = _make_store(state)

        with patch("merge_queue.config.get_target_branches", return_value=["main"]):
            result = _sync_missing_prs(client, state, store)

        assert len(result["queue"]) == 2
        numbers = {e["stack"][0]["number"] for e in result["queue"]}
        assert numbers == {1, 2}
        store.write.assert_called_once()
