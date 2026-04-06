# Merge Queue for Stacked PRs

[![CI](https://github.com/gbalke/merge_queue/actions/workflows/ci.yml/badge.svg)](https://github.com/gbalke/merge_queue/actions/workflows/ci.yml)
[![Coverage](https://img.shields.io/badge/coverage-90%25+-brightgreen)](https://github.com/gbalke/merge_queue)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org)

A lightweight merge queue for GitHub that handles stacked/chained PRs (e.g., from [revup](https://github.com/Skydio/revup)). Self-hosted via GitHub Actions.

## Features

- **Per-branch queues** -- independent queue per target branch
- **Stacked PR support** -- merges entire PR chains atomically
- **CI gate** -- PRs must pass CI before merging
- **Hotfix label** -- priority enqueue (CI still runs)
- **Break-glass label** -- skip CI, merge immediately (admin-only)
- **Protected paths** -- file patterns requiring authorized approval, with per-path approvers
- **Auto-retry on diverge** -- up to 3 retries when target branch moves during CI
- **Stuck batch recovery** -- detects and resumes batches stuck in "completing" state
- **Branch protection rulesets** -- auto-created for target and `mq/*` branches
- **Auto-enqueue missed PRs** -- catches PRs missed due to concurrency cancellation
- **Metrics** -- optional OTLP or Prometheus backend (Grafana Cloud ready)
- **Atomic state writes** -- `state.json` + `STATUS.md` via single Git Trees API commit

## How It Works

1. Create stacked PRs where PR B targets PR A's branch
2. Add the `queue` label to PRs you want merged (contiguous from stack bottom)
3. The queue processes stacks in FIFO order per target branch:
   - Locks PR branches via rulesets, creates `mq/<target>/<batch-id>` branch
   - Merges each PR `--no-ff`, dispatches CI, polls for completion
   - **Success**: fast-forwards target branch, PRs auto-close as "Merged"
   - **Failure**: unlocks, notifies with failed job/step details + CI link
   - **Diverge**: auto-retries up to 3 times from new target tip
4. To abort: remove the `queue` label

## Comparison

| Feature | [GitHub MQ](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/configuring-pull-request-merges/managing-a-merge-queue) | [Graphite](https://graphite.com/docs/graphite-merge-queue) | [Mergify](https://docs.mergify.com/merge-queue/) | [Aviator](https://docs.aviator.co/mergequeue) | [Bors-ng](https://bors.tech/documentation/) | **This MQ** |
|---|---|---|---|---|---|---|
| **Open source** | [No](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/configuring-pull-request-merges/managing-a-merge-queue) | [No](https://graphite.com/docs/graphite-merge-queue) | [On-premise (paid)](https://mergify.com/pricing) | [Enterprise only](https://aviator.co/pricing) | [Yes (Apache 2.0)](https://github.com/bors-ng/bors-ng) | [**Yes (MIT)**](docs/self-hosted.md) |
| **Stacked PRs** | [No](https://github.com/orgs/community/discussions/133871) | [Yes](https://graphite.com/blog/the-first-stack-aware-merge-queue) | — | [Yes](https://docs.aviator.co/mergequeue/how-to-guides/merging-stacked-prs) | No | [**Yes**](docs/stacked-prs.md) |
| **Multi-branch** | [Yes](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/about-protected-branches#require-merge-queue) | — | — | — | No | [**Yes**](docs/multi-branch.md) |
| **CI gating** | [Yes](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/configuring-pull-request-merges/managing-a-merge-queue#configuring-continuous-integration-ci-workflows-for-merge-queues) | [Yes](https://graphite.com/features/merge-queue) | [Yes](https://docs.mergify.com/merge-queue/) | [Yes](https://docs.aviator.co/mergequeue) | [Yes](https://bors.tech/documentation/) | [**Yes**](docs/ci-gating.md) |
| **Priority merges** | [Limited](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/configuring-pull-request-merges/managing-a-merge-queue#jumping-to-the-top-of-the-queue) | [Partial](https://graphite.com/docs/graphite-merge-queue) | [Yes](https://docs.mergify.com/merge-queue/priority/) | [Yes](https://docs.aviator.co/mergequeue/concepts/priority-merges) | [Yes](https://bors.tech/documentation/) | [**Yes**](docs/priority-merges.md) |
| **Protected paths** | No | — | [Scoped queues](https://docs.mergify.com/merge-queue/) | [Affected targets](https://docs.aviator.co/mergequeue/concepts/affected-targets) | [CODEOWNERS](https://bors.tech/documentation/) | [**Yes**](docs/protected-paths.md) |
| **Auto-retry** | [No](https://docs.github.com/en/pull-requests/collaborating-with-pull-requests/incorporating-changes-from-a-pull-request/merging-a-pull-request-with-a-merge-queue#understanding-why-your-pull-request-was-removed-from-the-merge-queue) | [Partial](https://graphite.com/docs/graphite-merge-queue) | [Yes](https://docs.mergify.com/merge-queue/batches/) | [Partial](https://docs.aviator.co/mergequeue/concepts/parallel-mode) | [Manual](https://bors.tech/documentation/) | [**Yes**](docs/auto-retry.md) |
| **Self-hosted** | [No](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/configuring-pull-request-merges/managing-a-merge-queue) | [No](https://graphite.com/docs/graphite-merge-queue) | [Paid](https://mergify.com/pricing) | [Enterprise](https://aviator.co/pricing) | [Yes](https://github.com/bors-ng/bors-ng) | [**Yes**](docs/self-hosted.md) |
| **Pricing** | [Free (public) / $21/user](https://docs.github.com/en/pull-requests/collaborating-with-pull-requests/incorporating-changes-from-a-pull-request/merging-a-pull-request-with-a-merge-queue) | [$40/user/mo](https://graphite.com/pricing) | [Free (5 users) / $21/seat](https://mergify.com/pricing) | [Free (<15) / $12/user](https://aviator.co/pricing) | [Free](https://github.com/bors-ng/bors-ng) | **Free** |

Bors-ng was [archived April 2024](https://github.com/bors-ng/bors-ng). `—` = undocumented/unverified.

## Setup

1. Copy [`examples/merge-queue.yml`](examples/merge-queue.yml) to `.github/workflows/merge-queue.yml`. Set `MQ_CI_WORKFLOW` to your CI workflow filename.

2. Create labels:

   | Label | Purpose |
   |-------|---------|
   | `queue` | Enqueue PR for merging |
   | `locked` | Auto-managed -- branch locked during merge |
   | `re-test` | Retrigger CI on PR head branch |
   | `hotfix` | Priority enqueue (admin + allowlist) |
   | `break-glass` | Skip CI, merge immediately (admin + allowlist) |

3. Create the admin token secret (needs **Administration: Read and Write** PAT):
   ```bash
   gh secret set MQ_ADMIN_TOKEN --repo <owner>/<repo>
   ```

4. Repository settings: enable "Automatically delete head branches", set Actions permissions to read/write.

5. (Optional) Add `merge-queue.yml` config in repo root:
   ```yaml
   break_glass_users: [gbalke, deploy-bot]
   target_branches: [main, release/1.0]
   protected_paths:
     - merge-queue.yml
     - .github/workflows/
     - path: merge_queue/
       approvers: [gbalke, security-team-lead]
   metrics:
     backend: otlp  # "otlp", "prometheus", or omit
     endpoint: https://otlp-gateway-prod-us-west-0.grafana.net/otlp/v1/metrics
   ```

6. (Optional) For metrics, set auth secrets:
   ```bash
   gh secret set MQ_METRICS_USER --repo <owner>/<repo>
   gh secret set MQ_METRICS_TOKEN --repo <owner>/<repo>
   ```
   When unset, metrics are silently disabled.

## Label Hierarchy

| Label | Who | CI? | Queue Position | Active Batch |
|-------|-----|-----|----------------|--------------|
| `queue` | Anyone | Yes | Back (FIFO) | Waits |
| `hotfix` | Admins + allowlist | Yes | Front | Aborts active, re-queues behind |
| `break-glass` | Admins + allowlist | **No** | Immediate | Aborts active, merges now |

## Architecture

```
merge_queue/
    cli.py              # Commands: enqueue, process, abort, retest, hotfix, break-glass, status, summary, check-rules
    queue.py            # Stack detection, FIFO ordering
    batch.py            # Batch lifecycle: lock, merge, CI, complete/fail, auto-retry
    rules.py            # 5 invariant rules checked pre/post batch
    store.py            # Persistent state on mq/state branch (atomic Git Trees API writes)
    state.py            # Queue state snapshot dataclass
    status.py           # Markdown + terminal status rendering
    comments.py         # PR comment templates (single updating comment per PR)
    config.py           # merge-queue.yml parser
    github_client.py    # GitHub API wrapper with rate limit tracking + caching
    types.py            # Dataclasses: PullRequest, Stack, Batch, QueueEntry, etc.
    metrics/            # MetricsBackend protocol + otlp/prometheus/noop backends
    providers/local.py  # LocalGitProvider for integration testing (bare repo, no API)
tests/                  # 482+ tests, 90%+ coverage enforced
integration/            # End-to-end test script
ci/                     # CI scripts: run, lint, format, test
```

### Commands

| Command | Trigger | Description |
|---------|---------|-------------|
| `enqueue <pr>` | `queue` label added | Add stack to queue |
| `process` | workflow_dispatch | Process next queued stack |
| `abort <pr>` | `queue` label removed | Abort batch or dequeue |
| `retest <pr>` | `re-test` label added | Retrigger CI |
| `hotfix <pr>` | `hotfix` label added | Priority enqueue, abort active batch |
| `break-glass <pr>` | `break-glass` label added | Skip CI, merge immediately |
| `status` | workflow_dispatch | Print queue state |
| `summary` | Always | Render status to job summary |
| `check-rules` | workflow_dispatch | Run invariant rules |

### Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `GITHUB_TOKEN` | Yes | Auto-provided. PR/branch/deployment operations. |
| `MQ_ADMIN_TOKEN` | Yes | PAT with Administration:Write. Rulesets + `update_ref`. |
| `MQ_CI_WORKFLOW` | No | CI workflow filename (default: `ci.yml`). |
| `MQ_SENDER` | Auto | `github.event.sender.login`. For hotfix/break-glass auth. |
| `MQ_METRICS_USER` | No | OTLP/Prometheus instance ID. |
| `MQ_METRICS_TOKEN` | No | OTLP/Prometheus API key. |
| `GITHUB_RUN_URL` | Auto | Current Actions run URL. |
| `GITHUB_REPOSITORY` | Auto | `owner/repo`. |
| `GITHUB_EVENT_TIME` | Auto | PR event timestamp. |

### State Management

Queue state lives on the `mq/state` branch as `state.json` (v2 schema) with per-branch queues, batch progress tracking, and history. Writes are atomic: `state.json` + `STATUS.md` committed in a single Git Trees API call. A rendered `STATUS.md` dashboard is auto-generated per target branch.

### Safety

- **Branch locking**: rulesets with retry + verification + rollback on failure
- **Fast-forward only**: target branch updated via `force: false`
- **Atomic merge**: entire stack merges or nothing does
- **Invariant rules**: 5 rules validated pre/post batch
- **Race guards**: skip merged PRs, duplicates, recently-processed PRs
- **Shell injection protection**: commands via `$MQ_CMD` env var, not interpolation

## Development

```bash
pip install -e ".[dev]"
ci/run                    # lint + format + test in parallel
```

## Security

See [SECURITY_AUDIT.md](SECURITY_AUDIT.md). Key points: no PR-branch code execution, shell injection protection, admin-only emergency labels, protected paths with authorized approvers.
