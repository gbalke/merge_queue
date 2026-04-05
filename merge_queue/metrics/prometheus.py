"""Prometheus push gateway / remote write metrics backend.

Pushes metrics in Prometheus text exposition format to a push gateway or
Grafana Cloud endpoint.  Authentication uses the ``MQ_METRICS_TOKEN``
environment variable (basic auth with empty username for Grafana Cloud,
or bearer token for other gateways).
"""

from __future__ import annotations

import base64
import logging
import os
import time

import requests

log = logging.getLogger(__name__)


def _build_text_payload(batch_id: str, metrics: dict) -> str:
    """Build a Prometheus text exposition format payload.

    Label-valued fields (``status``, ``target_branch``) are attached as labels
    on every gauge line.  Numeric fields become individual gauge values.
    """
    labels = {
        "batch_id": batch_id,
        "status": metrics.get("status", "unknown"),
        "target_branch": metrics.get("target_branch", "unknown"),
    }
    label_str = ",".join(f'{k}="{v}"' for k, v in labels.items())

    lines: list[str] = []
    gauge_metrics = [
        ("mq_batch_duration_seconds", "duration_seconds"),
        ("mq_batch_ci_duration_seconds", "ci_duration_seconds"),
        ("mq_batch_pr_count", "pr_count"),
        ("mq_batch_retry_count", "retry_count"),
        ("mq_queue_depth", "queue_depth"),
    ]
    ts_ms = int(time.time() * 1000)
    for prom_name, key in gauge_metrics:
        value = metrics.get(key)
        if value is not None:
            lines.append(f"# TYPE {prom_name} gauge")
            lines.append(f"{prom_name}{{{label_str}}} {value} {ts_ms}")

    return "\n".join(lines) + "\n"


class PrometheusBackend:
    """Push metrics to a Prometheus push gateway or Grafana Cloud.

    The endpoint URL and backend type come from ``merge-queue.yml``.
    The API token comes from the ``MQ_METRICS_TOKEN`` environment variable.

    For Grafana Cloud, the endpoint accepts Prometheus remote write with
    basic auth (user ID in ``MQ_METRICS_USER``, API key in ``MQ_METRICS_TOKEN``).
    """

    def __init__(self, endpoint: str) -> None:
        self._endpoint = endpoint

    def push_batch_metrics(self, batch_id: str, metrics: dict) -> None:
        """Push batch metrics to the configured Prometheus endpoint."""
        token = os.environ.get("MQ_METRICS_TOKEN", "")
        if not token:
            log.warning("MQ_METRICS_TOKEN not set, skipping metrics push")
            return

        user = os.environ.get("MQ_METRICS_USER", "")
        if user:
            # Grafana Cloud style: basic auth with user:token
            creds = base64.b64encode(f"{user}:{token}".encode()).decode()
            auth_header = f"Basic {creds}"
        else:
            # Bearer token for other push gateways
            auth_header = f"Bearer {token}"

        payload = _build_text_payload(batch_id, metrics)

        try:
            resp = requests.post(
                self._endpoint,
                data=payload,
                headers={
                    "Authorization": auth_header,
                    "Content-Type": "text/plain",
                },
                timeout=10,
            )
            resp.raise_for_status()
            log.info(
                "Pushed metrics for batch %s (HTTP %s)", batch_id, resp.status_code
            )
        except Exception:
            log.warning("Failed to push metrics for batch %s", batch_id, exc_info=True)
