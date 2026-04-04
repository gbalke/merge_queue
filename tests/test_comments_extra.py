"""Tests for comments.py — covering all templates and missing branches."""

from __future__ import annotations

import merge_queue.comments as comments


_STACK = [
    {"number": 1, "head_ref": "feat-a", "title": "Add feature A"},
    {"number": 2, "head_ref": "feat-b", "title": ""},
]


# --- _mq_link ---


def test_mq_link_with_owner_repo() -> None:
    result = comments.queued(1, 1, _STACK, owner="myorg", repo="myrepo")
    assert "https://github.com/myorg/myrepo/deployments/merge-queue" in result


def test_mq_link_without_owner_repo() -> None:
    result = comments.queued(1, 1, _STACK)
    assert "deployments/merge-queue" not in result


# --- already_queued ---


def test_already_queued_contains_position() -> None:
    result = comments.already_queued(3)
    assert "position 3" in result
    assert "Merge Queue" in result


def test_already_queued_with_owner_repo() -> None:
    result = comments.already_queued(2, owner="o", repo="r")
    assert "https://github.com/o/r/deployments/merge-queue" in result


# --- batch_started ---


def test_batch_started_without_ci_url() -> None:
    result = comments.batch_started("mq/123", _STACK)
    assert "mq/123" in result
    assert "CI Running" in result
    assert "View CI run" not in result


def test_batch_started_with_ci_url() -> None:
    result = comments.batch_started(
        "mq/123", _STACK, ci_run_url="https://actions.example.com/run/1"
    )
    assert "[View CI run →](https://actions.example.com/run/1)" in result


# --- merged ---


def test_merged_minimal() -> None:
    """merged() with no optional args produces a simple message."""
    result = comments.merged("main")
    assert "Merged" in result
    assert "main" in result
    assert "Queue wait" not in result


def test_merged_with_timestamps_no_ci_started() -> None:
    """merged() with queued_at and completed_at but no ci_started_at shows Total only."""
    result = comments.merged(
        "main",
        queued_at="2026-01-01T00:00:00+00:00",
        completed_at="2026-01-01T00:02:00+00:00",
    )
    assert "**Total**" in result
    assert "2m 0s" in result
    assert "Queue wait" not in result


def test_merged_with_all_timestamps_shows_full_stats_table() -> None:
    """merged() with all timestamps renders the three-row stats table."""
    result = comments.merged(
        "main",
        queued_at="2026-01-01T00:00:00+00:00",
        ci_started_at="2026-01-01T00:01:00+00:00",
        completed_at="2026-01-01T00:03:00+00:00",
    )
    assert "Queue wait" in result
    assert "CI + merge" in result
    assert "**Total**" in result
    assert "1m 0s" in result  # queue wait = 60s
    assert "2m 0s" in result  # CI = 120s


def test_merged_with_stack() -> None:
    """merged() with a stack renders the commits section."""
    result = comments.merged("main", stack=_STACK)
    assert "**Commits:**" in result
    assert "feat-a" in result


def test_merged_without_stack() -> None:
    """merged() with no stack omits the commits section."""
    result = comments.merged("main", stack=None)
    assert "**Commits:**" not in result


def test_merged_with_ci_run_url() -> None:
    """merged() with ci_run_url renders a link."""
    result = comments.merged("main", ci_run_url="https://ci.example.com/42")
    assert "[View CI run →](https://ci.example.com/42)" in result


def test_merged_with_bad_timestamps_omits_stats() -> None:
    """If timestamps are unparseable the stats block is silently omitted."""
    result = comments.merged("main", queued_at="not-a-date", completed_at="also-bad")
    assert "**Total**" not in result
    assert "Merged" in result


# --- failed ---


def test_failed_minimal() -> None:
    result = comments.failed("CI failed")
    assert "Failed" in result
    assert "CI failed" in result
    assert "View failed CI run" not in result
    assert "**Job:**" not in result


def test_failed_with_job_and_step() -> None:
    result = comments.failed("CI failed", failed_job="build", failed_step="compile")
    assert "**Job:** build" in result
    assert "**Step:** compile" in result


def test_failed_with_ci_run_url() -> None:
    result = comments.failed("CI failed", ci_run_url="https://ci.example.com/fail")
    assert "[View failed CI run →](https://ci.example.com/fail)" in result


def test_failed_with_job_only() -> None:
    result = comments.failed("CI failed", failed_job="integration-tests")
    assert "**Job:** integration-tests" in result
    assert "**Step:**" not in result


# --- _fmt_duration ---


def test_fmt_duration_under_60s() -> None:
    # Indirectly tested via merged() with short duration
    result = comments.merged(
        "main",
        queued_at="2026-01-01T00:00:00+00:00",
        completed_at="2026-01-01T00:00:45+00:00",
    )
    assert "45s" in result


def test_fmt_duration_over_60s() -> None:
    result = comments.merged(
        "main",
        queued_at="2026-01-01T00:00:00+00:00",
        completed_at="2026-01-01T00:02:30+00:00",
    )
    assert "2m 30s" in result


# --- batch_error / aborted / removed_from_queue ---


def test_batch_error() -> None:
    result = comments.batch_error("merge conflict on branch feat-a")
    assert "Batch Creation Failed" in result
    assert "merge conflict on branch feat-a" in result


def test_aborted() -> None:
    result = comments.aborted()
    assert "Aborted" in result


def test_removed_from_queue() -> None:
    result = comments.removed_from_queue()
    assert "Removed" in result
