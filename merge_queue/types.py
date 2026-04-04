from __future__ import annotations

import dataclasses
import datetime
import enum


class BatchStatus(enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"


@dataclasses.dataclass(frozen=True)
class PullRequest:
    number: int
    head_sha: str
    head_ref: str
    base_ref: str
    labels: tuple[str, ...]
    queued_at: datetime.datetime | None = None


@dataclasses.dataclass(frozen=True)
class Stack:
    prs: tuple[PullRequest, ...]  # Ordered bottom-to-top: [A->main, B->A, C->B]
    queued_at: datetime.datetime


@dataclasses.dataclass
class Batch:
    batch_id: str
    branch: str  # "mq/<batch_id>"
    stack: Stack
    status: BatchStatus = BatchStatus.PENDING
    ruleset_id: int | None = None


@dataclasses.dataclass(frozen=True)
class RuleResult:
    name: str
    passed: bool
    message: str


# --- State store types ---


@dataclasses.dataclass
class QueueEntry:
    """A stack waiting in the queue."""
    position: int
    queued_at: str  # ISO 8601
    stack: list[dict]  # [{number, head_sha, head_ref, base_ref, title}]
    deployment_id: int | None = None


@dataclasses.dataclass
class ActiveBatch:
    """The currently processing batch."""
    batch_id: str
    branch: str
    ruleset_id: int | None
    started_at: str  # ISO 8601
    progress: str  # "locking", "merging", "running_ci", "completing"
    stack: list[dict]
    deployment_id: int | None = None


@dataclasses.dataclass
class HistoryEntry:
    """A completed batch."""
    batch_id: str
    status: str  # "merged", "failed", "aborted"
    completed_at: str  # ISO 8601
    prs: list[int]
    duration_seconds: float


def empty_state() -> dict:
    """Return a fresh empty state dict."""
    return {
        "version": 1,
        "updated_at": "",
        "queue": [],
        "active_batch": None,
        "history": [],
    }
