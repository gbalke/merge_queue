"""Shared test fixtures and helpers."""

from __future__ import annotations

import datetime
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from merge_queue.state import QueueState
from merge_queue.types import Batch, BatchStatus, PullRequest, Stack, empty_state

# ---------------------------------------------------------------------------
# Shared timestamp constants
# ---------------------------------------------------------------------------

T0 = datetime.datetime(2026, 1, 1, 0, 0, 0, tzinfo=datetime.timezone.utc)
T1 = datetime.datetime(2026, 1, 1, 0, 1, 0, tzinfo=datetime.timezone.utc)
T2 = datetime.datetime(2026, 1, 1, 0, 2, 0, tzinfo=datetime.timezone.utc)
T3 = datetime.datetime(2026, 1, 1, 0, 3, 0, tzinfo=datetime.timezone.utc)


def now_iso() -> str:
    """Return the current UTC time as an ISO 8601 string."""
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# PullRequest / Stack / Batch factories
# ---------------------------------------------------------------------------


def make_pr(
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


def make_stack(*prs: PullRequest, queued_at: datetime.datetime = T0) -> Stack:
    return Stack(prs=tuple(prs), queued_at=queued_at)


def make_batch(
    stack: Stack,
    batch_id: str = "123",
    branch: str = "mq/123",
    status: BatchStatus = BatchStatus.RUNNING,
    ruleset_id: int | None = 42,
) -> Batch:
    return Batch(
        batch_id=batch_id,
        branch=branch,
        stack=stack,
        status=status,
        ruleset_id=ruleset_id,
    )


def make_pr_data(
    number: int,
    head_ref: str,
    base_ref: str = "main",
    labels: list[str] | None = None,
    head_sha: str = "",
) -> dict[str, Any]:
    """Create a PR dict as returned by the GitHub API."""
    return {
        "number": number,
        "head": {"ref": head_ref, "sha": head_sha or f"sha-{number}"},
        "base": {"ref": base_ref},
        "labels": [{"name": lbl} for lbl in (labels or [])],
    }


def make_queue_entry(
    number: int,
    head_ref: str = "feat-a",
    base_ref: str = "main",
    position: int = 1,
    deployment_id: int | None = 99,
    comment_ids: dict | None = None,
    queued_at: datetime.datetime = T0,
) -> dict[str, Any]:
    """Create a queue entry dict as stored in state.json."""
    return {
        "position": position,
        "queued_at": queued_at.isoformat(),
        "stack": [
            {
                "number": number,
                "head_sha": f"sha-{number}",
                "head_ref": head_ref,
                "base_ref": base_ref,
                "title": "PR title",
            }
        ],
        "deployment_id": deployment_id,
        "comment_ids": comment_ids or {number: 1000 + number},
    }


def make_state(**overrides: Any) -> dict[str, Any]:
    """Return empty_state() with any fields overridden."""
    s = empty_state()
    s.update(overrides)
    return s


def make_v2_state(
    branch: str = "main",
    queue: list | None = None,
    active_batch: dict | None = None,
    history: list | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    """Return a v2 state dict with the given branch populated."""
    s = empty_state()
    s["branches"][branch] = {
        "queue": queue or [],
        "active_batch": active_batch,
    }
    if history is not None:
        s["history"] = history
    s.update(overrides)
    return s


def make_api_state(
    prs: list[PullRequest] | None = None,
    mq_branches: list[str] | None = None,
    rulesets: list[dict] | None = None,
) -> QueueState:
    """Return a minimal QueueState for use in CLI/rules tests."""
    return QueueState(
        default_branch="main",
        mq_branches=mq_branches or [],
        rulesets=rulesets or [],
        prs=prs or [],
        all_pr_data=[],
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_client() -> MagicMock:
    """Create a mock GitHubClient with sensible defaults."""
    client = MagicMock()
    client.get_default_branch.return_value = "main"
    client.list_mq_branches.return_value = []
    client.list_rulesets.return_value = []
    client.list_open_prs.return_value = []
    client.get_branch_sha.return_value = "abc123"
    client.compare_commits.return_value = "ahead"
    client.create_ruleset.return_value = 42
    client.poll_ci.return_value = True
    client.get_pr_ci_status.return_value = (True, "")
    return client


@pytest.fixture
def mock_store() -> MagicMock:
    """Patch StateStore so tests don't touch the filesystem."""
    with patch("merge_queue.cli.StateStore") as cls:
        store = MagicMock()
        store.read.return_value = empty_state()
        cls.return_value = store
        yield store
