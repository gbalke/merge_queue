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
