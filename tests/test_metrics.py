"""Tests for the metrics subsystem."""

from __future__ import annotations

import datetime
import logging
from unittest.mock import MagicMock, patch

from tests.conftest import make_v2_state


T0 = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)


# --- NoopBackend ---


class TestNoopBackend:
    def test_noop_backend_does_nothing(self):
        from merge_queue.metrics.noop import NoopBackend

        backend = NoopBackend()
        # Should not raise
        backend.push_batch_metrics(
            "batch-1",
            {
                "duration_seconds": 42.0,
                "ci_duration_seconds": 30.0,
                "status": "merged",
                "pr_count": 2,
                "retry_count": 0,
                "queue_depth": 3,
                "target_branch": "main",
            },
        )


# --- get_backend factory ---


class TestGetBackend:
    def test_returns_noop_when_no_config(self):
        from merge_queue.metrics import get_backend
        from merge_queue.metrics.noop import NoopBackend

        backend = get_backend(None)
        assert isinstance(backend, NoopBackend)

    def test_returns_noop_when_empty_config(self):
        from merge_queue.metrics import get_backend
        from merge_queue.metrics.noop import NoopBackend

        backend = get_backend({})
        assert isinstance(backend, NoopBackend)

    def test_returns_prometheus_when_configured(self):
        from merge_queue.metrics import get_backend
        from merge_queue.metrics.prometheus import PrometheusBackend

        config = {
            "backend": "prometheus",
            "endpoint": "https://prometheus.example.com/api/v1/push",
        }
        backend = get_backend(config)
        assert isinstance(backend, PrometheusBackend)

    def test_returns_otlp_when_configured(self):
        from merge_queue.metrics import get_backend
        from merge_queue.metrics.otlp import OtlpBackend

        config = {
            "backend": "otlp",
            "endpoint": "https://otlp-gateway-prod-us-west-0.grafana.net/otlp/v1/metrics",
        }
        backend = get_backend(config)
        assert isinstance(backend, OtlpBackend)

    def test_returns_noop_for_unknown_backend(self, caplog):
        from merge_queue.metrics import get_backend
        from merge_queue.metrics.noop import NoopBackend

        backend = get_backend({"backend": "unknown_thing"})
        assert isinstance(backend, NoopBackend)


# --- PrometheusBackend ---


class TestPrometheusBackend:
    def test_pushes_metrics(self, monkeypatch):
        from merge_queue.metrics.prometheus import PrometheusBackend

        monkeypatch.setenv("MQ_METRICS_TOKEN", "test-api-key")
        backend = PrometheusBackend(
            endpoint="https://prometheus.example.com/api/v1/push",
        )

        with patch("merge_queue.metrics.prometheus.requests") as mock_requests:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_requests.post.return_value = mock_response

            backend.push_batch_metrics(
                "batch-42",
                {
                    "duration_seconds": 120.5,
                    "ci_duration_seconds": 90.0,
                    "status": "merged",
                    "pr_count": 3,
                    "retry_count": 1,
                    "queue_depth": 5,
                    "target_branch": "main",
                },
            )

            mock_requests.post.assert_called_once()
            call_args = mock_requests.post.call_args
            url = call_args[0][0]
            assert url == "https://prometheus.example.com/api/v1/push"

            # Check auth header
            headers = call_args[1].get("headers", {})
            assert "Authorization" in headers

            # Check body contains expected metric names
            body = call_args[1].get("data", "")
            assert "mq_batch_duration_seconds" in body
            assert "mq_batch_ci_duration_seconds" in body
            assert "mq_batch_pr_count" in body
            assert "mq_batch_retry_count" in body
            assert "mq_queue_depth" in body
            # status and target_branch should appear as labels
            assert 'status="merged"' in body
            assert 'target_branch="main"' in body

    def test_failure_does_not_crash(self, monkeypatch, caplog):
        from merge_queue.metrics.prometheus import PrometheusBackend

        monkeypatch.setenv("MQ_METRICS_TOKEN", "test-api-key")
        backend = PrometheusBackend(
            endpoint="https://prometheus.example.com/api/v1/push",
        )

        with patch("merge_queue.metrics.prometheus.requests") as mock_requests:
            mock_requests.post.side_effect = Exception("Connection timeout")

            # Should NOT raise
            with caplog.at_level(logging.WARNING):
                backend.push_batch_metrics(
                    "batch-42",
                    {
                        "duration_seconds": 10.0,
                        "ci_duration_seconds": 5.0,
                        "status": "failed",
                        "pr_count": 1,
                        "retry_count": 0,
                        "queue_depth": 0,
                        "target_branch": "main",
                    },
                )

            assert any("metrics" in r.message.lower() for r in caplog.records)

    def test_missing_token_does_not_crash(self, monkeypatch, caplog):
        from merge_queue.metrics.prometheus import PrometheusBackend

        monkeypatch.delenv("MQ_METRICS_TOKEN", raising=False)
        backend = PrometheusBackend(
            endpoint="https://prometheus.example.com/api/v1/push",
        )

        with caplog.at_level(logging.WARNING):
            backend.push_batch_metrics(
                "batch-42",
                {
                    "duration_seconds": 10.0,
                    "status": "merged",
                    "pr_count": 1,
                },
            )

        assert any("MQ_METRICS_TOKEN" in r.message for r in caplog.records)


# --- OtlpBackend ---


class TestOtlpBackend:
    def test_otlp_backend_pushes_json(self, monkeypatch):
        from merge_queue.metrics.otlp import OtlpBackend

        monkeypatch.setenv("MQ_METRICS_TOKEN", "glc_test-api-key")
        monkeypatch.setenv("MQ_METRICS_USER", "1584401")
        backend = OtlpBackend(
            endpoint="https://otlp-gateway-prod-us-west-0.grafana.net/otlp/v1/metrics",
        )

        with patch("merge_queue.metrics.otlp.requests") as mock_requests:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_requests.post.return_value = mock_response

            backend.push_batch_metrics(
                "batch-42",
                {
                    "duration_seconds": 120.5,
                    "ci_duration_seconds": 90.0,
                    "status": "merged",
                    "pr_count": 3,
                    "retry_count": 1,
                    "queue_depth": 5,
                    "target_branch": "main",
                },
            )

            mock_requests.post.assert_called_once()
            call_args = mock_requests.post.call_args
            url = call_args[0][0]
            assert (
                url == "https://otlp-gateway-prod-us-west-0.grafana.net/otlp/v1/metrics"
            )

            # Check auth uses requests' auth parameter
            auth = call_args[1].get("auth")
            assert auth == ("1584401", "glc_test-api-key")
            headers = call_args[1].get("headers", {})
            assert headers["Content-Type"] == "application/json"

            # Check JSON payload structure
            payload = call_args[1].get("json", {})
            assert "resourceMetrics" in payload
            scope_metrics = payload["resourceMetrics"][0]["scopeMetrics"]
            metrics = scope_metrics[0]["metrics"]

            metric_names = [m["name"] for m in metrics]
            assert "mq_batch_duration_seconds" in metric_names
            assert "mq_batch_ci_duration_seconds" in metric_names
            assert "mq_batch_pr_count" in metric_names
            assert "mq_batch_retry_count" in metric_names
            assert "mq_queue_depth" in metric_names

            # Check a data point has correct structure
            dp = metrics[0]["gauge"]["dataPoints"][0]
            assert "asDouble" in dp
            assert "timeUnixNano" in dp
            assert "attributes" in dp
            attr_keys = [a["key"] for a in dp["attributes"]]
            assert "status" in attr_keys
            assert "target_branch" in attr_keys
            assert "batch_id" in attr_keys

    def test_otlp_failure_does_not_crash(self, monkeypatch, caplog):
        from merge_queue.metrics.otlp import OtlpBackend

        monkeypatch.setenv("MQ_METRICS_TOKEN", "glc_test-api-key")
        monkeypatch.setenv("MQ_METRICS_USER", "1584401")
        backend = OtlpBackend(
            endpoint="https://otlp-gateway-prod-us-west-0.grafana.net/otlp/v1/metrics",
        )

        with patch("merge_queue.metrics.otlp.requests") as mock_requests:
            mock_requests.post.side_effect = Exception("Connection timeout")

            # Should NOT raise
            with caplog.at_level(logging.WARNING):
                backend.push_batch_metrics(
                    "batch-42",
                    {
                        "duration_seconds": 10.0,
                        "ci_duration_seconds": 5.0,
                        "status": "failed",
                        "pr_count": 1,
                        "retry_count": 0,
                        "queue_depth": 0,
                        "target_branch": "main",
                    },
                )

            assert any("metrics" in r.message.lower() for r in caplog.records)

    def test_otlp_missing_token_does_not_crash(self, monkeypatch, caplog):
        from merge_queue.metrics.otlp import OtlpBackend

        monkeypatch.delenv("MQ_METRICS_TOKEN", raising=False)
        backend = OtlpBackend(
            endpoint="https://otlp-gateway-prod-us-west-0.grafana.net/otlp/v1/metrics",
        )

        with caplog.at_level(logging.WARNING):
            backend.push_batch_metrics(
                "batch-42",
                {
                    "duration_seconds": 10.0,
                    "status": "merged",
                    "pr_count": 1,
                },
            )

        assert any("MQ_METRICS_TOKEN" in r.message for r in caplog.records)


# --- Config parsing ---


class TestMetricsConfig:
    def test_parse_metrics_section(self):
        from merge_queue.config import parse_metrics_config

        content = (
            "target_branches:\n"
            "  - main\n"
            "metrics:\n"
            "  backend: prometheus\n"
            "  endpoint: https://prom.example.com/api/v1/push\n"
            "break_glass_users:\n"
            "  - alice\n"
        )
        result = parse_metrics_config(content)
        assert result == {
            "backend": "prometheus",
            "endpoint": "https://prom.example.com/api/v1/push",
        }

    def test_parse_metrics_missing(self):
        from merge_queue.config import parse_metrics_config

        content = "target_branches:\n  - main\n"
        result = parse_metrics_config(content)
        assert result is None

    def test_parse_metrics_empty(self):
        from merge_queue.config import parse_metrics_config

        content = "metrics:\nbreak_glass_users:\n  - alice\n"
        result = parse_metrics_config(content)
        # Empty metrics section returns empty dict (no keys)
        assert result == {}


# --- Integration: metrics called after batch merge ---


class TestMetricsIntegration:
    def _pr_data(self, number: int = 1) -> dict:
        return {
            "number": number,
            "head": {"ref": f"feat-{number}", "sha": f"sha-{number}"},
            "base": {"ref": "main"},
            "labels": [{"name": "queue"}],
            "title": "PR title",
        }

    def _queue_entry(self, number: int = 1) -> dict:
        return {
            "position": 1,
            "queued_at": T0.isoformat(),
            "stack": [
                {
                    "number": number,
                    "head_sha": f"sha-{number}",
                    "head_ref": "feat-a",
                    "base_ref": "main",
                }
            ],
            "deployment_id": None,
        }

    @patch("merge_queue.cli.get_metrics_backend")
    @patch("merge_queue.cli.QueueState")
    @patch("merge_queue.cli.batch_mod")
    def test_metrics_called_after_batch_merge(
        self, batch_mod, QS, mock_get_backend, mock_client, mock_store
    ):
        from merge_queue.state import QueueState as QSType
        from merge_queue.types import Batch, PullRequest, Stack

        mock_store.read.return_value = make_v2_state(queue=[self._queue_entry()])
        mock_client.list_open_prs.return_value = [self._pr_data(1)]

        qs = QSType(
            default_branch="main", mq_branches=[], rulesets=[], prs=[], all_pr_data=[]
        )
        QS.fetch.return_value = qs

        pr = PullRequest(1, "sha-1", "feat-a", "main", ("queue",))
        stack = Stack(prs=(pr,), queued_at=T0)
        batch = Batch("123", "mq/main/123", stack)
        batch_mod.create_batch.return_value = batch
        ci_result = MagicMock()
        ci_result.passed = True
        ci_result.run_url = ""
        batch_mod.run_ci.return_value = ci_result
        batch_mod.BatchError = Exception

        mock_backend = MagicMock()
        mock_get_backend.return_value = mock_backend

        from merge_queue.cli import do_process

        result = do_process(mock_client)
        assert result == "merged"

        mock_backend.push_batch_metrics.assert_called_once()
        call_args = mock_backend.push_batch_metrics.call_args
        batch_id = call_args[0][0]
        metrics = call_args[0][1]
        assert batch_id == "123"
        assert metrics["status"] == "merged"
        assert metrics["pr_count"] == 1
        assert metrics["target_branch"] == "main"
        assert "duration_seconds" in metrics
        assert "ci_duration_seconds" in metrics
        assert "queue_depth" in metrics

    @patch("merge_queue.cli.get_metrics_backend")
    @patch("merge_queue.cli.QueueState")
    @patch("merge_queue.cli.batch_mod")
    def test_metrics_not_called_when_not_configured(
        self, batch_mod, QS, mock_get_backend, mock_client, mock_store
    ):
        from merge_queue.metrics.noop import NoopBackend
        from merge_queue.state import QueueState as QSType
        from merge_queue.types import Batch, Stack

        mock_store.read.return_value = make_v2_state(queue=[self._queue_entry()])
        mock_client.list_open_prs.return_value = [self._pr_data(1)]

        qs = QSType(
            default_branch="main", mq_branches=[], rulesets=[], prs=[], all_pr_data=[]
        )
        QS.fetch.return_value = qs

        stack = Stack(prs=(), queued_at=T0)
        batch = Batch("123", "mq/main/123", stack)
        batch_mod.create_batch.return_value = batch
        ci_result = MagicMock()
        ci_result.passed = True
        ci_result.run_url = ""
        batch_mod.run_ci.return_value = ci_result
        batch_mod.BatchError = Exception

        noop = NoopBackend()
        mock_get_backend.return_value = noop

        from merge_queue.cli import do_process

        result = do_process(mock_client)
        assert result == "merged"
        # NoopBackend was used — no crash, no real push
        mock_get_backend.assert_called_once()
