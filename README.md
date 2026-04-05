# Merge Queue for Stacked PRs

[![CI](https://github.com/gbalke/merge_queue/actions/workflows/ci.yml/badge.svg)](https://github.com/gbalke/merge_queue/actions/workflows/ci.yml)
[![Coverage](https://img.shields.io/badge/coverage-90%25+-brightgreen)](https://github.com/gbalke/merge_queue)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org)

A lightweight, Python-based merge queue for GitHub that understands stacked/chained PRs (e.g., created by [revup](https://github.com/Skydio/revup)).

## Features

- **Per-branch queues** — independent queues for each target branch (state.json v2 schema)
- **Per-branch STATUS.md** — rendered dashboards with relative timestamps for each target branch
- **Auto-protect target branches** — creates GitHub rulesets to enforce branch protection
- **CI gate** — PRs must pass CI before the merge queue accepts them
- **Break-glass label** — bypass CI gate for emergencies (admin-only + configurable allowlist)
- **Hotfix label** — priority enqueue for urgent fixes
- **Re-test label** — retrigger CI on a PR's head branch
- **Auto-retry on diverge** — automatically retries if `main` diverges during CI (max 3 retries)
- **Auto-enqueue missed PRs** — `_sync_missing_prs` picks up PRs whose enqueue was cancelled by concurrency
- **Configurable CI workflow** — set `MQ_CI_WORKFLOW` to dispatch any workflow file
- **Security hardened** — shell injection fix (#60), admin-only emergency labels
- **336+ tests, 90%+ coverage enforced**

## How It Works

1. Create stacked PRs where PR B targets PR A's branch
2. Add the `queue` label to PRs you want merged (contiguous from the bottom of the stack)
3. The merge queue processes stacks in **FIFO order**:
   - Locks PR branches via GitHub rulesets (with retry + verification)
   - Creates `mq/<target>/<batch-id>` branch, merges each PR `--no-ff`
   - Dispatches CI and polls for completion
   - On success: fast-forwards the target branch, PRs auto-close as **"Merged"**
   - On failure: unlocks, notifies with failed job/step details + CI run link
   - On diverge: auto-retries up to 3 times
4. After each batch, automatically processes the next queued stack
5. To abort: remove the `queue` label

## Architecture

```
merge_queue/
    cli.py               # Commands: enqueue, process, abort, retest, hotfix, status, summary, check-rules
    queue.py             # Pure logic: stack detection, FIFO ordering
    batch.py             # Batch lifecycle: lock, merge, CI, complete/fail, auto-retry
    rules.py             # 5 invariant rules checked pre/post batch
    store.py             # Persistent state on mq/state branch (v1->v2 migration)
    state.py             # Queue state snapshot dataclass (fetched once per run)
    status.py            # Markdown + terminal status rendering (per-branch)
    comments.py          # PR comment templates (single updating comment per PR)
    config.py            # merge-queue.yml parser (break_glass_users, target_branches)
    github_client.py     # GitHub API wrapper with rate limit tracking + caching
    types.py             # Dataclasses: PullRequest, Stack, Batch, QueueEntry, etc.
    providers/
        local.py         # LocalGitProvider for integration testing (bare repo + in-memory state)
tests/                   # 337+ tests, 90%+ coverage enforced
    conftest.py          # Shared fixtures
    test_api_calls.py    # API call budget enforcement
    test_auto_rebase.py  # Auto-retry on diverge
    test_batch.py        # Batch create, complete, fail, abort, unlock
    test_branch_protection.py  # Ruleset creation and verification
    test_break_glass.py  # Break-glass label authorization
    test_ci_gate.py      # CI gate enforcement
    test_ci_gate_tdd.py  # CI gate TDD scenarios
    test_cli.py          # CLI command routing and process loop
    test_cli_extra.py    # Additional CLI edge cases
    test_comments_extra.py  # Comment rendering edge cases
    test_configurable_ci.py # MQ_CI_WORKFLOW dispatch
    test_multi_branch.py # Multi-target-branch queues
    test_per_branch.py   # Per-branch state and STATUS.md
    test_queue.py        # Stack detection, FIFO ordering, validation
    test_rate_limit.py   # Rate limit tracking and warnings
    test_rules.py        # All 5 invariant rules
    test_status.py       # Status rendering
    test_status_extra.py # Additional status edge cases
    test_store.py        # State persistence and v1->v2 migration
    test_store_extra.py  # Store concurrency and edge cases
    test_sync_missing.py # _sync_missing_prs auto-enqueue
integration/             # End-to-end test script (pass + fail stacks)
```

### Commands

| Command | Trigger | Description |
|---------|---------|-------------|
| `enqueue <pr>` | `queue` label added | Add stack to queue, start processing if idle |
| `process` | workflow_dispatch | Process next queued stack |
| `abort <pr>` | `queue` label removed | Abort batch or remove from queue |
| `retest <pr>` | `re-test` label added | Retrigger CI on a PR's head branch |
| `hotfix <pr>` | `hotfix` label added | Priority-enqueue a PR for urgent fixes |
| `status` | workflow_dispatch | Print current queue state |
| `summary` | Always (job summary step) | Render queue status to GitHub step summary |
| `check-rules` | workflow_dispatch | Run invariant rules |

### State Management

Queue state lives on the `mq/state` branch as `state.json` (v2 schema):
- **Per-branch queues** — `branches` dict keyed by target branch name
- Queue ordering (FIFO by enqueue timestamp)
- Active batch progress (locking -> running_ci -> completing)
- History with timing stats
- Comment IDs for single-comment updates
- Automatic v1 -> v2 migration on first read

A rendered `STATUS.md` dashboard is auto-generated per target branch alongside a root `STATUS.md` index.

### Live UI

Each batch creates a **GitHub Deployment** in the `merge-queue` environment with real-time status updates (queued -> in_progress -> success/failure). PR comments link directly to the deployment and CI run pages.

### PR Comments

One comment per PR, updated as state changes:

```
Merge Queue -- Merged to main.

| Phase      | Duration |
|:-----------|:---------|
| Queue wait | 5s       |
| CI + merge | 1m 8s    |
| Total      | 1m 13s   |

Commits:
- #38 greg/revup/main/ci-coverage -- Enforce coverage in CI
- #39 greg/revup/main/readme-badges -- Add badges to README

View CI run ->
View merge queue ->
```

Failed comments include the job name, failed step, and link to the CI run.

### Safety

- **Branch locking**: Rulesets with retry + verification (lock before merge, rollback on failure)
- **Auto-protect target branches**: Rulesets created automatically via `MQ_ADMIN_TOKEN`
- **CI gate**: PRs must pass CI before merge queue accepts them
- **Auto-retry on diverge**: Up to 3 retries when target branch moves during CI
- **Stale batch recovery**: Auto-clears if PRs are already merged or batch > 30min old
- **Race condition guards**: Skip merged PRs, duplicates, recently-processed PRs
- **Auto-enqueue missed PRs**: `_sync_missing_prs` catches PRs missed due to concurrency cancellation
- **Fast-forward only**: Target branch updated via `force: false`
- **Atomic**: Entire stack merges or nothing does
- **Invariant rules**: 5 rules validated pre/post batch
- **Shell injection protection**: Workflow command passed via `$MQ_CMD` env var, not shell interpolation

### API Optimization

- Response caching: `list_open_prs`, `get_default_branch`, `list_mq_branches`, `list_rulesets`, `get_label_timestamp` cached per-run
- `QueueState` snapshot: one fetch, used by rules + queue logic (zero additional calls)
- Parallel cleanup: unlock + delete branches + post comments via ThreadPool
- Merged PRs skip label removal (inert on closed PRs)
- Rate limit tracking with low-remaining warnings
- API call budget tests enforce limits (3-PR stack <= 35 calls)

## Configuration

### `merge-queue.yml` (repo root)

Optional configuration file placed in the repository root. The merge queue reads
this from the default branch at runtime.

```yaml
# Optional configuration file. Place in the repository root.

# Users authorized to use the break-glass label (bypass CI gate).
# In addition to these users, repository admins can always use break-glass.
break_glass_users:
  - gbalke
  - deploy-bot

# Target branches for the merge queue (per-branch queues).
target_branches:
  - main
  - release/1.0
```

Supported keys:
- `break_glass_users` — GitHub usernames allowed to apply the `break-glass` label
- `target_branches` — branches the merge queue manages (each gets its own queue)

The file is parsed without PyYAML (simple line-based parser in
`merge_queue/config.py`), so stick to the exact format shown above.

### Environment Variables

The workflow (`.github/workflows/merge-queue.yml`) passes these environment
variables to the merge queue CLI:

| Variable | Required | Description |
|----------|----------|-------------|
| `GITHUB_TOKEN` | Yes | Auto-provided by GitHub Actions. Used for PR, branch, and deployment operations. |
| `MQ_ADMIN_TOKEN` | Yes | Fine-grained PAT with **Administration: Write**. Used for branch-locking rulesets. |
| `MQ_CI_WORKFLOW` | No | CI workflow filename to dispatch (default: `ci.yml`). |
| `MQ_SENDER` | Auto | Set by the workflow to `github.event.sender.login`. Used for break-glass authorization. |
| `GITHUB_RUN_URL` | Auto | Set by the workflow to the current Actions run URL. Included in PR comments. |
| `GITHUB_REPOSITORY` | Auto | Set by GitHub Actions (`owner/repo`). Determines target repository. |
| `GITHUB_EVENT_TIME` | Auto | Set by the workflow to the PR event timestamp. |

For local development / testing, you can also set `GITHUB_OWNER` and
`GITHUB_REPO` separately instead of `GITHUB_REPOSITORY`.

## Integration with External Repos

Install merge-queue in any GitHub repository:

1. Copy [`examples/merge-queue.yml`](examples/merge-queue.yml) to `.github/workflows/merge-queue.yml`
2. Change `MQ_CI_WORKFLOW` to your CI workflow filename (e.g., `test.yml`, `ci.yml`)
3. Create labels: `queue`, `locked`, `re-test`, `break-glass`, `hotfix`
4. Add `MQ_ADMIN_TOKEN` secret (fine-grained PAT with Administration: Write)
5. Add `queue` label to PRs to start using the merge queue

The `MQ_CI_WORKFLOW` env var controls which workflow is dispatched for CI checks. PRs must pass CI before the merge queue will accept them.

The merge queue auto-detects your repository's default branch.

## Setup

### 1. Repository Settings

- Enable "Automatically delete head branches"
- Actions permissions: read/write

### 2. Labels

- `queue` — triggers the merge queue
- `locked` — auto-managed, indicates branch is locked
- `re-test` — retrigger CI on a PR's head branch
- `break-glass` — bypass CI gate (emergency use only, admin + allowlist)
- `hotfix` — priority enqueue for urgent fixes

### 3. `MQ_ADMIN_TOKEN` Secret

Branch locking requires a fine-grained PAT with **Administration: Read and Write**:

```bash
gh secret set MQ_ADMIN_TOKEN --repo <owner>/<repo>
```

### 4. Install and Test

```bash
pip install -e ".[dev]"
pytest tests/              # 337+ tests, 90%+ coverage enforced
```

## Integration Testing

```bash
python integration/create_test_stacks.py run
```

Creates two stacks (one passing, one with syntax error), queues them, and verifies the pass stack merges while the fail stack is rejected.

The `LocalGitProvider` (`merge_queue/providers/local.py`) enables full integration testing against a local bare git repo without any GitHub API calls. It implements the same `GitHubClientProtocol` interface with in-memory PR metadata, labels, comments, rulesets, and deployments.

## Security

See [SECURITY_AUDIT.md](SECURITY_AUDIT.md) for the full security audit report.

Key mitigations in place:
- **No PR-branch code execution** — workflow always installs from `main`, never from PR branches
- **Shell injection protection** — commands passed via `$MQ_CMD` env var, not shell interpolation
- **Admin-only emergency labels** — `break-glass` restricted to repo admins + configured allowlist

## Development

See [CLAUDE.md](CLAUDE.md) for development workflow.
