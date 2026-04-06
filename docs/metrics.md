# Metrics

Optional OTLP or Prometheus metrics export for monitoring queue health.

## How It Works

The metrics subsystem is implemented in
[`merge_queue/metrics/`](../merge_queue/metrics/). It uses a `MetricsBackend`
protocol with three implementations: `otlp`, `prometheus`, and `noop` (default
when unconfigured).

Metrics are emitted at key lifecycle points: enqueue, batch start, CI
completion, merge success/failure, and retry events.

## Configuration

Add a `metrics` section to `merge-queue.yml`:

```yaml
metrics:
  backend: otlp  # "otlp" or "prometheus"
  endpoint: https://otlp-gateway-prod-us-west-0.grafana.net/otlp/v1/metrics
```

Set authentication secrets:

```bash
gh secret set MQ_METRICS_USER --repo <owner>/<repo>
gh secret set MQ_METRICS_TOKEN --repo <owner>/<repo>
```

When `MQ_METRICS_USER` and `MQ_METRICS_TOKEN` are unset, metrics are silently
disabled (noop backend).

## Grafana Cloud

For Grafana Cloud OTLP ingestion, use your Grafana Cloud stack's OTLP gateway
URL as the endpoint. `MQ_METRICS_USER` is the instance ID and
`MQ_METRICS_TOKEN` is the API key.

## Available Metrics

All metrics flow through `MetricsCollector` (`merge_queue/metrics/__init__.py`) which accumulates metrics via typed `record_*()` methods and flushes once at end of run.

### Batch Timing (per completion)

| Metric | Description |
|--------|-------------|
| `mq_batch_queue_wait_seconds` | Enqueue to batch start |
| `mq_batch_lock_seconds` | Branch locking + merge time |
| `mq_batch_ci_seconds` | CI phase duration |
| `mq_batch_merge_seconds` | CI pass to fast-forward complete |
| `mq_batch_total_seconds` | End-to-end duration |

### Queue Health (per process run)

| Metric | Description |
|--------|-------------|
| `mq_queue_depth` | Current queue depth (per branch) |
| `mq_queue_oldest_seconds` | Age of oldest entry (detects stuck queues) |
| `mq_api_calls_total` | GitHub API calls used in this run |
| `mq_api_remaining` | Remaining API quota |

### Failure Tracking

| Metric | Description |
|--------|-------------|
| `mq_batch_failures_total` | Failure count with `reason` label (ci_failed, merge_conflict, diverged, error) |

### Labels (on all metrics)

| Label | Example |
|-------|---------|
| `target_branch` | `main`, `release/1.0` |
| `batch_id` | `1775437212` |
| `pr_numbers` | `96,97` |
| `repo` | `gbalke/merge_queue` |
| `status` | `merged`, `ci_failed`, `aborted` |
| `trigger` | `queue`, `hotfix`, `break-glass` |
