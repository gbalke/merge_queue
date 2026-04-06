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

Metrics include queue depth, batch duration, CI wait time, merge
success/failure counts, and retry counts. See the backend implementations in
`merge_queue/metrics/` for the full list.
