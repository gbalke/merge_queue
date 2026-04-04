"""Shared test fixtures."""

from __future__ import annotations

import datetime
from typing import Any
from unittest.mock import MagicMock

import pytest

from merge_queue.types import PullRequest


@pytest.fixture
def mock_client():
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
    return client


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
        "labels": [{"name": l} for l in (labels or [])],
    }
