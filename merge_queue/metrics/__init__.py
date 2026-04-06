"""Metrics subsystem -- backend-agnostic analytics for the merge queue.

Defines the ``MetricsBackend`` protocol, a ``MetricsCollector`` that
accumulates typed metrics, and a ``get_backend()`` factory that returns
the configured backend (or a silent no-op when metrics are disabled).
"""

from __future__ import annotations

import logging
import time
from typing import Protocol

log = logging.getLogger(__name__)


class MetricsBackend(Protocol):
    """Protocol that all metrics backends must satisfy."""

    def push_batch_metrics(self, batch_id: str, metrics: dict) -> None:
        """Push metrics for a completed batch (legacy API).

        ``metrics`` contains keys such as ``duration_seconds``,
        ``ci_duration_seconds``, ``status``, ``pr_count``, ``retry_count``,
        ``queue_depth``, and ``target_branch``.
        """
        ...

    def push_metrics(self, metrics: list[dict]) -> None:
        """Push a list of metric dicts.

        Each dict has ``name``, ``value``, ``labels`` (dict), and
        ``timestamp_ns`` (int, epoch nanoseconds).
        """
        ...


class MetricsCollector:
    """Accumulate typed metrics and flush them to a backend in one push.

    Create once per run with context labels, call ``record_*()`` at each
    phase, then call ``flush()`` at the end.  All failures are logged as
    warnings -- the collector never crashes the merge queue.
    """

    def __init__(
        self, backend: MetricsBackend, repo: str = "", trigger: str = ""
    ) -> None:
        self._backend = backend
        self._repo = repo
        self._trigger = trigger  # "queue", "hotfix", "break-glass"
        self._metrics: list[dict] = []
        self._flushed = False

    def record_batch_complete(
        self,
        batch_id: str,
        target_branch: str,
        pr_numbers: list[int],
        status: str,
        queue_wait_seconds: float | None = None,
        lock_seconds: float | None = None,
        ci_seconds: float | None = None,
        merge_seconds: float | None = None,
        total_seconds: float | None = None,
        retry_count: int = 0,
    ) -> None:
        """Record metrics for a completed batch."""
        labels = {
            "repo": self._repo,
            "trigger": self._trigger,
            "batch_id": batch_id,
            "target_branch": target_branch,
            "status": status,
            "pr_numbers": ",".join(str(n) for n in pr_numbers),
        }
        now_ns = time.time_ns()
        pairs: list[tuple[str, float | None]] = [
            ("mq_batch_queue_wait_seconds", queue_wait_seconds),
            ("mq_batch_lock_seconds", lock_seconds),
            ("mq_batch_ci_seconds", ci_seconds),
            ("mq_batch_merge_seconds", merge_seconds),
            ("mq_batch_total_seconds", total_seconds),
            ("mq_batch_pr_count", float(len(pr_numbers))),
            ("mq_batch_retry_count", float(retry_count)),
        ]
        for name, value in pairs:
            if value is not None:
                self._metrics.append(
                    {
                        "name": name,
                        "value": value,
                        "labels": labels,
                        "timestamp_ns": now_ns,
                    }
                )

    def record_queue_health(
        self,
        target_branch: str,
        queue_depth: int,
        oldest_entry_seconds: float | None = None,
    ) -> None:
        """Record current queue health metrics."""
        labels = {
            "repo": self._repo,
            "trigger": self._trigger,
            "target_branch": target_branch,
        }
        now_ns = time.time_ns()
        self._metrics.append(
            {
                "name": "mq_queue_depth",
                "value": float(queue_depth),
                "labels": labels,
                "timestamp_ns": now_ns,
            }
        )
        if oldest_entry_seconds is not None:
            self._metrics.append(
                {
                    "name": "mq_queue_oldest_entry_seconds",
                    "value": oldest_entry_seconds,
                    "labels": labels,
                    "timestamp_ns": now_ns,
                }
            )

    def record_api_usage(
        self,
        calls_total: int,
        remaining: int,
    ) -> None:
        """Record GitHub API usage metrics."""
        labels = {
            "repo": self._repo,
            "trigger": self._trigger,
        }
        now_ns = time.time_ns()
        self._metrics.append(
            {
                "name": "mq_api_calls_total",
                "value": float(calls_total),
                "labels": labels,
                "timestamp_ns": now_ns,
            }
        )
        self._metrics.append(
            {
                "name": "mq_api_remaining",
                "value": float(remaining),
                "labels": labels,
                "timestamp_ns": now_ns,
            }
        )

    def record_failure(
        self,
        target_branch: str,
        batch_id: str,
        reason: str,
        pr_numbers: list[int],
    ) -> None:
        """Record a failure event."""
        labels = {
            "repo": self._repo,
            "trigger": self._trigger,
            "target_branch": target_branch,
            "batch_id": batch_id,
            "reason": reason,
            "pr_numbers": ",".join(str(n) for n in pr_numbers),
        }
        now_ns = time.time_ns()
        self._metrics.append(
            {
                "name": "mq_batch_failure",
                "value": 1.0,
                "labels": labels,
                "timestamp_ns": now_ns,
            }
        )

    def flush(self) -> None:
        """Push all accumulated metrics to the backend.

        Safe to call multiple times -- subsequent calls after the first are
        no-ops.  Failures are logged as warnings and never raised.
        """
        if self._flushed or not self._metrics:
            return
        self._flushed = True
        try:
            self._backend.push_metrics(list(self._metrics))
        except Exception:
            log.warning("Failed to flush metrics", exc_info=True)


def get_backend(config: dict | None) -> MetricsBackend:
    """Return the configured metrics backend, or :class:`NoopBackend`.

    *config* is the ``metrics`` section from ``merge-queue.yml`` (may be
    ``None`` or empty when metrics are not configured).
    """
    from merge_queue.metrics.noop import NoopBackend

    if not config or not config.get("backend"):
        return NoopBackend()

    backend_type = config["backend"]

    if backend_type == "prometheus":
        from merge_queue.metrics.prometheus import PrometheusBackend

        endpoint = config.get("endpoint", "")
        return PrometheusBackend(endpoint=endpoint)

    if backend_type == "otlp":
        from merge_queue.metrics.otlp import OtlpBackend

        endpoint = config.get("endpoint", "")
        return OtlpBackend(endpoint=endpoint)

    log.warning("Unknown metrics backend %r, using no-op", backend_type)
    return NoopBackend()
