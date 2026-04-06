"""Metrics subsystem — backend-agnostic analytics for the merge queue.

Defines the ``MetricsBackend`` protocol and a ``get_backend()`` factory that
returns the configured backend (or a silent no-op when metrics are disabled).
"""

from __future__ import annotations

import logging
from typing import Protocol

log = logging.getLogger(__name__)


class MetricsBackend(Protocol):
    """Protocol that all metrics backends must satisfy."""

    def push_batch_metrics(self, batch_id: str, metrics: dict) -> None:
        """Push metrics for a completed batch.

        ``metrics`` contains keys such as ``duration_seconds``,
        ``ci_duration_seconds``, ``status``, ``pr_count``, ``retry_count``,
        ``queue_depth``, and ``target_branch``.
        """
        ...


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
