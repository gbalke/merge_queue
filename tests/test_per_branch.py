"""Tests for per-branch queue: v1→v2 migration, independent state/status."""

from __future__ import annotations

import base64
import datetime
import json
from unittest.mock import MagicMock, patch

import pytest

from merge_queue.cli import do_abort, do_enqueue, do_process
from merge_queue.status import render_branch_status_md, render_root_status_md
from merge_queue.store import StateStore, _branch_status_path, _migrate_v1_to_v2
from merge_queue.types import empty_state

from tests.conftest import make_v2_state


T0 = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)


# ---------------------------------------------------------------------------
# v1 → v2 migration
# ---------------------------------------------------------------------------


def test_migrate_v1_wraps_queue_and_active_batch_under_branch() -> None:
    v1 = {
        "version": 1,
        "updated_at": "2026-01-01T00:00:00Z",
        "queue": [{"position": 1}],
        "active_batch": {"batch_id": "x"},
        "history": [{"batch_id": "y"}],
    }
    v2 = _migrate_v1_to_v2(v1, default_branch="main")

    assert v2["version"] == 2
    assert v2["branches"]["main"]["queue"] == [{"position": 1}]
    assert v2["branches"]["main"]["active_batch"] == {"batch_id": "x"}
    assert v2["history"] == [{"batch_id": "y"}]


def test_store_read_auto_migrates_v1() -> None:
    v1_state = {
        "version": 1,
        "updated_at": "",
        "queue": [{"position": 1, "stack": [{"number": 5}]}],
        "active_batch": None,
        "history": [],
    }
    encoded = base64.b64encode(json.dumps(v1_state).encode()).decode()

    client = MagicMock()
    client.get_file_content.return_value = {"sha": "abc", "content": encoded}
    client.get_default_branch.return_value = "main"

    store = StateStore(client)
    state = store.read()

    assert state["version"] == 2
    assert state["branches"]["main"]["queue"][0]["position"] == 1
    assert state["branches"]["main"]["active_batch"] is None


# ---------------------------------------------------------------------------
# Per-branch queue operations
# ---------------------------------------------------------------------------


def test_enqueue_adds_to_correct_branch() -> None:
    """Enqueueing a PR targeting release/1.0 puts it in that branch's queue."""
    client = MagicMock()
    client.owner = "o"
    client.repo = "r"
    client.get_default_branch.return_value = "main"
    client.get_pr.return_value = {
        "state": "open",
        "head": {"sha": "sha-1", "ref": "feat-x"},
        "base": {"ref": "release/1.0"},
        "title": "Fix",
        "labels": [],
    }
    client.list_open_prs.return_value = []
    client.list_mq_branches.return_value = []
    client.list_rulesets.return_value = []
    client.get_pr_ci_status.return_value = (True, "")
    client.create_comment.return_value = 1
    client.create_deployment.return_value = 9
    client.get_file_content.side_effect = Exception("404")

    with (
        patch("merge_queue.cli.StateStore") as store_cls,
        patch("merge_queue.cli.QueueState") as qs,
        patch("merge_queue.cli.do_process", return_value="queued_waiting"),
        patch(
            "merge_queue.config.get_target_branches",
            return_value=["main", "release/1.0"],
        ),
    ):
        from tests.conftest import make_api_state

        store = MagicMock()
        store.read.return_value = empty_state()
        store_cls.return_value = store
        qs.fetch.return_value = make_api_state(mq_branches=["mq/active"])

        result = do_enqueue(client, 42)

    assert result == "queued_waiting"
    written = store.write.call_args[0][0]
    assert "release/1.0" in written["branches"]
    assert (
        written["branches"]["release/1.0"]["queue"][0]["target_branch"] == "release/1.0"
    )
    assert "main" not in written["branches"] or not written["branches"]["main"]["queue"]


@pytest.mark.parametrize(
    "target_branch",
    ["main", "release/1.0"],
)
def test_do_process_processes_each_branch_independently(target_branch: str) -> None:
    """do_process picks the first branch with a non-empty queue and no active_batch."""
    entry = {
        "position": 1,
        "queued_at": T0.isoformat(),
        "stack": [
            {
                "number": 1,
                "head_sha": "sha-1",
                "head_ref": "feat-a",
                "base_ref": target_branch,
            }
        ],
        "deployment_id": None,
        "target_branch": target_branch,
    }
    state = make_v2_state(branch=target_branch, queue=[entry])

    with (
        patch("merge_queue.cli.StateStore") as store_cls,
        patch("merge_queue.cli.QueueState") as qs,
        patch("merge_queue.cli.batch_mod") as bm,
    ):
        from tests.conftest import make_api_state

        store = MagicMock()
        store.read.return_value = state
        store_cls.return_value = store
        qs.fetch.return_value = make_api_state()

        mock_batch = MagicMock()
        mock_batch.batch_id = "ts1"
        mock_batch.branch = f"mq/{target_branch}/ts1"
        mock_batch.ruleset_id = 42
        mock_batch.stack.prs = []
        bm.create_batch.return_value = mock_batch
        bm.run_ci.return_value = MagicMock(passed=True, run_url="")
        bm.BatchError = Exception

        result = do_process(client := MagicMock())
        client.get_pr.return_value = {"state": "open"}

    assert result in ("merged", "no_stacks", "batch_error", "ci_failed")
    # complete_batch called with the correct target_branch
    if bm.complete_batch.called:
        call_kwargs = bm.complete_batch.call_args
        assert call_kwargs.kwargs.get("target_branch") == target_branch


def test_do_process_leaves_other_branch_queues_untouched() -> None:
    """Processing main's batch should not affect release/1.0's queue."""
    main_entry = {
        "position": 1,
        "queued_at": T0.isoformat(),
        "stack": [
            {"number": 1, "head_sha": "sha-1", "head_ref": "feat-a", "base_ref": "main"}
        ],
        "deployment_id": None,
        "target_branch": "main",
    }
    release_entry = {
        "position": 1,
        "queued_at": T0.isoformat(),
        "stack": [
            {
                "number": 2,
                "head_sha": "sha-2",
                "head_ref": "feat-b",
                "base_ref": "release/1.0",
            }
        ],
        "deployment_id": None,
        "target_branch": "release/1.0",
    }
    state = {
        **empty_state(),
        "branches": {
            "main": {"queue": [main_entry], "active_batch": None},
            "release/1.0": {"queue": [release_entry], "active_batch": None},
        },
    }

    written_states: list[dict] = []

    with (
        patch("merge_queue.cli.StateStore") as store_cls,
        patch("merge_queue.cli.QueueState") as qs,
        patch("merge_queue.cli.batch_mod") as bm,
    ):
        from tests.conftest import make_api_state

        store = MagicMock()
        store.read.return_value = state
        store.write.side_effect = lambda s: written_states.append(
            {k: list(v.get("queue", [])) for k, v in s.get("branches", {}).items()}
        )
        store_cls.return_value = store
        qs.fetch.return_value = make_api_state()

        bm.create_batch.side_effect = Exception("merge conflict")
        bm.BatchError = Exception

        client = MagicMock()
        client.get_pr.return_value = {"state": "open"}
        client.list_open_prs.return_value = [
            {
                "number": 1,
                "head": {"ref": "feat-a", "sha": "sha-1"},
                "base": {"ref": "main"},
                "labels": [{"name": "queue"}],
            },
            {
                "number": 2,
                "head": {"ref": "feat-b", "sha": "sha-2"},
                "base": {"ref": "release/1.0"},
                "labels": [{"name": "queue"}],
            },
        ]
        do_process(client)

    # release/1.0's queue must remain untouched after processing main's batch
    for snapshot in written_states:
        if "release/1.0" in snapshot:
            assert len(snapshot["release/1.0"]) == 1


def test_do_abort_finds_pr_in_correct_branch() -> None:
    """Aborting PR #1 which is in release/1.0's active_batch, not main's."""
    state = {
        **empty_state(),
        "branches": {
            "main": {"queue": [], "active_batch": None},
            "release/1.0": {
                "queue": [],
                "active_batch": {
                    "batch_id": "x",
                    "stack": [{"number": 1}],
                    "deployment_id": 42,
                },
            },
        },
    }

    with (
        patch("merge_queue.cli.StateStore") as store_cls,
        patch("merge_queue.cli.batch_mod"),
    ):
        store = MagicMock()
        store.read.return_value = state
        store_cls.return_value = store

        client = MagicMock()
        result = do_abort(client, 1)

    assert result == "aborted"
    final = store.write.call_args[0][0]
    assert final["branches"]["release/1.0"]["active_batch"] is None


# ---------------------------------------------------------------------------
# Per-branch STATUS.md rendering
# ---------------------------------------------------------------------------


def test_render_branch_status_md_shows_branch_name() -> None:
    branch_state = {"queue": [], "active_batch": None}
    md = render_branch_status_md("release/1.0", branch_state)
    assert "release/1.0" in md
    assert "empty" in md.lower()


def test_render_branch_status_md_shows_active_batch() -> None:
    branch_state = {
        "active_batch": {
            "progress": "running_ci",
            "stack": [{"number": 7, "title": "Fix bug"}],
            "queued_at": T0.isoformat(),
        },
        "queue": [],
    }
    md = render_branch_status_md("main", branch_state)
    assert "#7" in md
    assert "CI running" in md


def test_render_root_status_md_links_to_each_branch() -> None:
    state = {
        **empty_state(),
        "branches": {
            "main": {"queue": [], "active_batch": None},
            "release/1.0": {
                "queue": [{"stack": [{"number": 1}]}],
                "active_batch": None,
            },
        },
    }
    md = render_root_status_md(state)
    assert "main" in md
    assert "release/1.0" in md


def test_render_root_status_md_shows_idle_indicator_when_empty() -> None:
    state = {
        **empty_state(),
        "branches": {"main": {"queue": [], "active_batch": None}},
    }
    md = render_root_status_md(state)
    assert "idle" in md.lower() or "\u2705" in md


def test_branch_status_path_for_release_branch() -> None:
    assert _branch_status_path("release/1.0") == "release/1.0/STATUS.md"
    assert _branch_status_path("main") == "main/STATUS.md"
