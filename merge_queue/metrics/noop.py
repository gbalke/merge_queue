"""No-op metrics backend — silently discards all metrics."""

from __future__ import annotations


class NoopBackend:
    """Metrics backend that does nothing.

    Used when no ``metrics`` section is present in ``merge-queue.yml``.
    """

    def push_batch_metrics(self, batch_id: str, metrics: dict) -> None:  # noqa: ARG002
        """Silently discard metrics."""
