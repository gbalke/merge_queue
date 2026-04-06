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

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `mq_batch_queue_wait_seconds` | gauge | repo, trigger, batch_id, target_branch, status, pr_numbers | Time from enqueue to batch start |
| `mq_batch_lock_seconds` | gauge | (same) | Branch locking + merge prep time |
| `mq_batch_ci_seconds` | gauge | (same) | CI phase duration |
| `mq_batch_merge_seconds` | gauge | (same) | CI pass to fast-forward complete |
| `mq_batch_total_seconds` | gauge | (same) | End-to-end batch duration |
| `mq_batch_pr_count` | gauge | (same) | Number of PRs in the batch |
| `mq_batch_retry_count` | gauge | (same) | Number of retries |
| `mq_queue_depth` | gauge | repo, trigger, target_branch | Current queue depth per branch |
| `mq_queue_oldest_entry_seconds` | gauge | (same) | Age of oldest queued entry |
| `mq_api_calls_total` | gauge | repo, trigger, section (optional) | API calls used |
| `mq_api_remaining` | gauge | repo, trigger, section (optional) | Remaining API quota |
| `mq_batch_failure` | gauge | repo, trigger, target_branch, batch_id, reason, pr_numbers | Failure event counter |

### Per-Section API Tracking

`mq_api_calls_total` and `mq_api_remaining` support an optional `section`
label that breaks down API consumption by phase:

| Section | Command | Description |
|---------|---------|-------------|
| `enqueue` | `do_enqueue` | PR validation, stack detection, state writes |
| `process_setup` | `do_process` | Stale batch cleanup, queue scan, batch creation |
| `ci_poll` | `do_process` | CI dispatch and polling loop |
| `batch_complete` | `do_process` | Merge completion, label removal, comments |
| `hotfix` | `do_hotfix` | Hotfix setup before delegating to `do_process` |
| `break_glass` | `do_break_glass` | Full break-glass merge (no CI) |
| *(empty)* | `do_process` | Total API calls for the entire run (backward compat) |
