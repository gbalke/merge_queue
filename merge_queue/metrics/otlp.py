"""OTLP JSON metrics backend.

Posts metrics in OTLP JSON format (``resourceMetrics``) to an HTTP endpoint
such as Grafana Cloud's OTLP gateway.  Authentication uses basic auth with
``MQ_METRICS_USER`` (instance ID) and ``MQ_METRICS_TOKEN`` (API key).
"""

from __future__ import annotations

import logging
import os
import time

import requests

log = logging.getLogger(__name__)


def _build_otlp_payload(batch_id: str, metrics: dict) -> dict:
    """Build an OTLP JSON payload for the given batch metrics.

    Returns a dict matching the ``resourceMetrics`` schema expected by
    Grafana Cloud and other OTLP-compatible collectors.
    """
    time_unix_nano = int(time.time() * 1_000_000_000)

    attributes = [
        {"key": "status", "value": {"stringValue": metrics.get("status", "unknown")}},
        {
            "key": "target_branch",
            "value": {"stringValue": metrics.get("target_branch", "unknown")},
        },
        {"key": "batch_id", "value": {"stringValue": batch_id}},
    ]

    gauge_metrics = [
        ("mq_batch_duration_seconds", "duration_seconds", "s"),
        ("mq_batch_ci_duration_seconds", "ci_duration_seconds", "s"),
        ("mq_batch_pr_count", "pr_count", "1"),
        ("mq_batch_retry_count", "retry_count", "1"),
        ("mq_queue_depth", "queue_depth", "1"),
    ]

    otlp_metrics: list[dict] = []
    for metric_name, key, unit in gauge_metrics:
        value = metrics.get(key)
        if value is not None:
            otlp_metrics.append(
                {
                    "name": metric_name,
                    "unit": unit,
                    "gauge": {
                        "dataPoints": [
                            {
                                "asDouble": float(value),
                                "timeUnixNano": time_unix_nano,
                                "attributes": attributes,
                            }
                        ]
                    },
                }
            )

    return {
        "resourceMetrics": [
            {
                "scopeMetrics": [
                    {
                        "metrics": otlp_metrics,
                    }
                ]
            }
        ]
    }


class OtlpBackend:
    """Push metrics to an OTLP HTTP endpoint in JSON format.

    The endpoint URL comes from ``merge-queue.yml``.  Authentication uses
    basic auth with ``MQ_METRICS_USER`` (instance ID) and ``MQ_METRICS_TOKEN``
    (API key) from environment variables.
    """

    def __init__(self, endpoint: str) -> None:
        self._endpoint = endpoint

    def push_batch_metrics(self, batch_id: str, metrics: dict) -> None:
        """Push batch metrics as OTLP JSON to the configured endpoint."""
        token = os.environ.get("MQ_METRICS_TOKEN", "")
        if not token:
            log.warning("MQ_METRICS_TOKEN not set, skipping metrics push")
            return

        user = os.environ.get("MQ_METRICS_USER", "")

        payload = _build_otlp_payload(batch_id, metrics)

        try:
            resp = requests.post(
                self._endpoint,
                json=payload,
                auth=(user, token) if user else None,
                headers={"Content-Type": "application/json"},
                timeout=10,
            )
            resp.raise_for_status()
            log.info(
                "Pushed OTLP metrics for batch %s (HTTP %s)",
                batch_id,
                resp.status_code,
            )
        except Exception:
            log.warning(
                "Failed to push OTLP metrics for batch %s", batch_id, exc_info=True
            )
