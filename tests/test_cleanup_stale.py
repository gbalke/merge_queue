"""Tests for _cleanup_stale_entries — removes queue entries whose queue label is gone."""

from __future__ import annotations

from unittest.mock import MagicMock

from merge_queue.cli import _cleanup_stale_entries
from merge_queue.types import empty_state

from .conftest import make_pr_data, make_queue_entry, make_v2_state


def _make_client(open_prs: list[dict] | None = None) -> MagicMock:
    client = MagicMock()
    client.list_open_prs.return_value = open_prs or []
    return client


def _make_store(state: dict) -> MagicMock:
    store = MagicMock()
    store.read.return_value = state
    return store


def _branch_queue(state: dict, branch: str = "main") -> list:
    return state.get("branches", {}).get(branch, {}).get("queue", [])


def _pr_with_label(number: int, base_ref: str = "main") -> dict:
    return make_pr_data(number, f"feat-{number}", base_ref=base_ref, labels=["queue"])


def _pr_without_label(number: int) -> dict:
    return make_pr_data(number, f"feat-{number}", labels=[])


class TestCleanupStaleEntries:
    def test_pr_without_queue_label_entry_removed(self):
        """An entry whose PR no longer has the queue label is removed."""
        entry = make_queue_entry(10)
        state = make_v2_state(queue=[entry])
        # PR 10 is open but has no queue label
        client = _make_client(open_prs=[_pr_without_label(10)])
        store = _make_store(state)

        result = _cleanup_stale_entries(client, state, store)

        assert _branch_queue(result) == []
        store.write.assert_called_once()

    def test_pr_with_queue_label_entry_kept(self):
        """An entry whose PR still has the queue label is left untouched."""
        entry = make_queue_entry(10)
        state = make_v2_state(queue=[entry])
        client = _make_client(open_prs=[_pr_with_label(10)])
        store = _make_store(state)

        result = _cleanup_stale_entries(client, state, store)

        assert len(_branch_queue(result)) == 1
        store.write.assert_not_called()

    def test_closed_pr_entry_removed(self):
        """An entry whose PR is closed/merged (absent from open PRs) is removed."""
        entry = make_queue_entry(20)
        state = make_v2_state(queue=[entry])
        # PR 20 is not in the open-PR list at all
        client = _make_client(open_prs=[])
        store = _make_store(state)

        result = _cleanup_stale_entries(client, state, store)

        assert _branch_queue(result) == []
        store.write.assert_called_once()

    def test_mixed_only_stale_removed(self):
        """Only stale entries are removed; valid ones survive."""
        entry_valid = make_queue_entry(1, position=1)
        entry_stale_no_label = make_queue_entry(2, position=2)
        entry_stale_closed = make_queue_entry(3, position=3)
        state = make_v2_state(
            queue=[entry_valid, entry_stale_no_label, entry_stale_closed]
        )
        # PR 1 has label, PR 2 is open but label removed, PR 3 is closed
        client = _make_client(open_prs=[_pr_with_label(1), _pr_without_label(2)])
        store = _make_store(state)

        result = _cleanup_stale_entries(client, state, store)

        queue = _branch_queue(result)
        assert len(queue) == 1
        assert queue[0]["stack"][0]["number"] == 1
        store.write.assert_called_once()

    def test_empty_queue_no_changes(self):
        """An already-empty queue causes no write and returns state unchanged."""
        state = make_v2_state(queue=[])
        client = _make_client(open_prs=[])
        store = _make_store(state)

        result = _cleanup_stale_entries(client, state, store)

        assert _branch_queue(result) == []
        store.write.assert_not_called()

    def test_positions_renumbered_after_removal(self):
        """After removal, remaining entries are re-numbered from 1."""
        entry1 = make_queue_entry(1, position=1)
        entry2 = make_queue_entry(2, position=2)
        entry3 = make_queue_entry(3, position=3)
        state = make_v2_state(queue=[entry1, entry2, entry3])
        # PR 1 is gone (closed), PRs 2 and 3 still have the label
        client = _make_client(open_prs=[_pr_with_label(2), _pr_with_label(3)])
        store = _make_store(state)

        result = _cleanup_stale_entries(client, state, store)

        queue = _branch_queue(result)
        assert len(queue) == 2
        assert queue[0]["position"] == 1
        assert queue[1]["position"] == 2

    def test_no_open_prs_all_entries_removed(self):
        """When no PRs are open at all, every queued entry is cleaned up."""
        entries = [make_queue_entry(n, position=n) for n in (1, 2, 3)]
        state = make_v2_state(queue=entries)
        client = _make_client(open_prs=[])
        store = _make_store(state)

        result = _cleanup_stale_entries(client, state, store)

        assert _branch_queue(result) == []
        store.write.assert_called_once()

    def test_multiple_branches_cleaned_independently(self):
        """Stale entries are cleaned per-branch; valid entries on other branches survive."""
        state = empty_state()
        # main branch: PR 10 has label (keep), PR 11 does not (remove)
        state["branches"]["main"] = {
            "queue": [
                make_queue_entry(10, position=1),
                make_queue_entry(11, position=2),
            ],
            "active_batch": None,
        }
        # release/1.0 branch: PR 20 is closed (remove)
        state["branches"]["release/1.0"] = {
            "queue": [make_queue_entry(20, position=1)],
            "active_batch": None,
        }
        client = _make_client(
            open_prs=[_pr_with_label(10), _pr_without_label(11)]
            # PR 20 absent from open list — closed
        )
        store = _make_store(state)

        result = _cleanup_stale_entries(client, state, store)

        assert len(_branch_queue(result, "main")) == 1
        assert _branch_queue(result, "main")[0]["stack"][0]["number"] == 10
        assert _branch_queue(result, "release/1.0") == []
        store.write.assert_called_once()

    def test_updated_at_set_when_changed(self):
        """When entries are removed, updated_at is refreshed on the state dict."""
        entry = make_queue_entry(5)
        state = make_v2_state(queue=[entry])
        state["updated_at"] = "2000-01-01T00:00:00+00:00"
        client = _make_client(open_prs=[_pr_without_label(5)])
        store = _make_store(state)

        result = _cleanup_stale_entries(client, state, store)

        assert result["updated_at"] != "2000-01-01T00:00:00+00:00"
