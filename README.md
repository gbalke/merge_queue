# Merge Queue for Stacked PRs

[![CI](https://github.com/gbalke/merge_queue/actions/workflows/ci.yml/badge.svg)](https://github.com/gbalke/merge_queue/actions/workflows/ci.yml)
[![Coverage](https://img.shields.io/badge/coverage-90%25+-brightgreen)](https://github.com/gbalke/merge_queue)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org)

A lightweight, Python-based merge queue for GitHub that understands stacked/chained PRs (e.g., created by [revup](https://github.com/Skydio/revup)).

## Features

- **Per-branch queues** -- independent queues for each target branch (`main`, `release/1.0`, etc.)
- **Stacked PR support** -- merges entire stacks (PR chains) atomically
- **Atomic state writes** -- `state.json` + `STATUS.md` written in a single commit via Git Trees API (no more 409 conflicts)
- **CI gate** -- PRs must pass CI before the merge queue accepts them
- **Hotfix label** -- priority enqueue at front of queue for urgent fixes (CI still runs)
- **Break-glass label** -- skip CI entirely and merge immediately (admin-only, last resort)
- **Protected paths** -- file patterns that require authorized approval before entering the queue, with per-path approvers
- **Auto-retry on diverge** -- if the target branch moves during CI, the batch auto-retries (up to 3 times)
- **Stuck completing detection** -- batches stuck in "completing" state are detected and resumed
- **Branch protection rulesets** -- auto-created for target branches and `mq/*` branches; admin bypass for MQ operations
- **Auto-enqueue missed PRs** -- `_sync_missing_prs` picks up PRs whose enqueue was cancelled by concurrency
- **464+ tests, 90%+ coverage enforced**

## How It Works

1. Create stacked PRs where PR B targets PR A's branch
2. Add the `queue` label to PRs you want merged (contiguous from the bottom of the stack)
3. The merge queue processes stacks in **FIFO order** per target branch:
   - Locks PR branches via GitHub rulesets (with retry + verification)
   - Creates `mq/<target>/<batch-id>` branch, merges each PR `--no-ff`
   - Dispatches CI and polls for completion
   - On success: fast-forwards the target branch (via admin token to bypass protection), PRs auto-close as **"Merged"**
   - On failure: unlocks, notifies with failed job/step details + CI run link
   - On diverge: auto-retries up to 3 times (recreates batch branch from new target tip)
4. After each batch, automatically processes the next queued stack
5. To abort: remove the `queue` label

## Setup

### 1. Install the Workflow

Copy [`examples/merge-queue.yml`](examples/merge-queue.yml) to `.github/workflows/merge-queue.yml` in your repository. Update:

- `MQ_CI_WORKFLOW` -- set to your CI workflow filename (e.g., `ci.yml`, `test.yml`)
- Add trigger conditions for `hotfix` and `break-glass` labels (see the actual workflow in this repo for a complete example)

### 2. Create Labels

| Label | Purpose |
|-------|---------|
| `queue` | Triggers merge queue -- add to PRs you want merged |
| `locked` | Auto-managed -- indicates branch is locked during merge |
| `re-test` | Retrigger CI on a PR's head branch |
| `hotfix` | Priority enqueue at front of queue (admin/authorized only) |
| `break-glass` | Skip CI entirely, merge immediately (admin/authorized only, last resort) |

### 3. Create `MQ_ADMIN_TOKEN` Secret

Branch locking and `update_ref` (fast-forwarding protected branches) require a fine-grained PAT with **Administration: Read and Write**:

```bash
gh secret set MQ_ADMIN_TOKEN --repo <owner>/<repo>
```

### 4. Repository Settings

- Enable "Automatically delete head branches"
- Actions permissions: read/write

### 5. Optional: `merge-queue.yml` Config

Place a `merge-queue.yml` file in the repository root to configure advanced behavior:

```yaml
# Users authorized to use break-glass and hotfix labels.
# Repo admins can always use these labels.
break_glass_users:
  - gbalke
  - deploy-bot

# Target branches (each gets its own independent queue).
# Default branch is always included.
target_branches:
  - main
  - release/1.0

# File patterns that require authorized approval before entering the queue.
protected_paths:
  - merge-queue.yml
  - .github/workflows/
  - path: merge_queue/
    approvers:
      - gbalke
      - security-team-lead
```

## Label Hierarchy

The three action labels have distinct behaviors:

| Label | Who Can Use | CI Runs? | Queue Position | Active Batch |
|-------|-------------|----------|----------------|--------------|
| `queue` | Anyone | Yes (must pass gate) | Back of queue (FIFO) | Waits its turn |
| `hotfix` | Admins + `break_glass_users` | Yes | Front of queue (position 0) | Aborts active batch, re-queues its PRs behind hotfix |
| `break-glass` | Admins + `break_glass_users` | **No** (skipped entirely) | Immediate | Aborts active batch, re-queues its PRs, merges without CI |

- **`hotfix`**: Use when you need priority but CI is functional. The PR still goes through the full MQ pipeline (lock, merge, CI, complete) but jumps ahead of everything else.
- **`break-glass`**: Use as a last resort when CI itself is broken. Creates a batch branch, skips CI, and fast-forwards the target branch immediately.

## CI Scripts

All CI checks live in `ci/` and are used both locally and in GitHub Actions:

```bash
ci/run              # Run all checks (lint + format + test) in parallel
ci/lint             # Python syntax check + ruff lint
ci/format           # ruff format --check
ci/test             # pytest with coverage
```

`ci/run` launches all jobs in parallel, reports pass/fail for each, and exits non-zero if any fail. The GitHub Actions workflow calls these same scripts.

## Multi-Branch Queues

Each target branch gets its own independent queue. Stacks are automatically routed to the correct queue based on their base branch:

```
main queue:           Stack A (T=0), Stack B (T=1)
release/1.0 queue:    Stack C (T=2)
```

Configure target branches in `merge-queue.yml`:

```yaml
target_branches:
  - main
  - release/1.0
```

The default branch is always included. Branch protection rulesets are auto-created for each target branch and cleaned up when branches are removed from the config.

## Protected Paths

PRs that modify files matching `protected_paths` patterns require an approving review from an authorized user before entering the merge queue.

```yaml
protected_paths:
  - merge-queue.yml           # exact file match
  - .github/workflows/        # directory match (any file under it)
  - path: merge_queue/        # directory match with per-path approvers
    approvers:
      - alice
      - bob
```

- Simple string entries fall back to `break_glass_users` + repo admins for approval.
- Entries with an `approvers` list require approval from one of those specific users (or a repo admin).
- When a PR touches a protected path without the required approval, the MQ removes the `queue` label and posts a comment listing the matched paths. Re-add `queue` after an authorized user approves.

## Architecture

```
merge_queue/
    cli.py               # Commands: enqueue, process, abort, retest, hotfix, break-glass, status, summary, check-rules
    queue.py             # Pure logic: stack detection, FIFO ordering
    batch.py             # Batch lifecycle: lock, merge, CI, complete/fail, auto-retry
    rules.py             # 5 invariant rules checked pre/post batch
    store.py             # Persistent state on mq/state branch (atomic writes via Git Trees API)
    state.py             # Queue state snapshot dataclass (fetched once per run)
    status.py            # Markdown + terminal status rendering (per-branch)
    comments.py          # PR comment templates (single updating comment per PR)
    config.py            # merge-queue.yml parser (break_glass_users, target_branches, protected_paths)
    github_client.py     # GitHub API wrapper with rate limit tracking + caching
    types.py             # Dataclasses: PullRequest, Stack, Batch, QueueEntry, etc.
    providers/
        local.py         # LocalGitProvider for integration testing (bare repo + in-memory state)
tests/                   # 464+ tests, 90%+ coverage enforced
integration/             # End-to-end test script (pass + fail stacks)
ci/                      # CI scripts (run, lint, format, test)
```

### Commands

| Command | Trigger | Description |
|---------|---------|-------------|
| `enqueue <pr>` | `queue` label added | Add stack to queue, start processing if idle |
| `process` | workflow_dispatch | Process next queued stack |
| `abort <pr>` | `queue` label removed | Abort batch or remove from queue |
| `retest <pr>` | `re-test` label added | Retrigger CI on a PR's head branch |
| `hotfix <pr>` | `hotfix` label added | Priority-enqueue at front, abort active batch |
| `break-glass <pr>` | `break-glass` label added | Skip CI, merge immediately (admin-only) |
| `status` | workflow_dispatch | Print current queue state |
| `summary` | Always (job summary step) | Render queue status to GitHub step summary |
| `check-rules` | workflow_dispatch | Run invariant rules |

### State Management

Queue state lives on the `mq/state` branch as `state.json` (v2 schema):
- **Per-branch queues** -- `branches` dict keyed by target branch name
- Queue ordering (FIFO by enqueue timestamp)
- Active batch progress (locking -> running_ci -> completing)
- History with timing stats
- Comment IDs for single-comment updates
- Automatic v1 -> v2 migration on first read

State writes are **atomic**: `state.json` and all `STATUS.md` files are committed in a single Git Trees API call, eliminating SHA conflicts between sequential file writes.

A rendered `STATUS.md` dashboard is auto-generated per target branch alongside a root `STATUS.md` index.

### Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `GITHUB_TOKEN` | Yes | Auto-provided by GitHub Actions. Used for PR, branch, and deployment operations. |
| `MQ_ADMIN_TOKEN` | Yes | Fine-grained PAT with **Administration: Write**. Used for branch protection rulesets and `update_ref` (bypassing branch protection to fast-forward target branches). |
| `MQ_CI_WORKFLOW` | No | CI workflow filename to dispatch (default: `ci.yml`). |
| `MQ_SENDER` | Auto | Set by the workflow to `github.event.sender.login`. Used for hotfix/break-glass authorization. |
| `GITHUB_RUN_URL` | Auto | Set by the workflow to the current Actions run URL. Included in PR comments. |
| `GITHUB_REPOSITORY` | Auto | Set by GitHub Actions (`owner/repo`). Determines target repository. |
| `GITHUB_EVENT_TIME` | Auto | Set by the workflow to the PR event timestamp. |

### Safety

- **Branch locking**: Rulesets with retry + verification (lock before merge, rollback on failure)
- **Branch protection rulesets**: Auto-created for target branches and `mq/*` branches; admin token bypasses for MQ operations
- **CI gate**: PRs must pass CI before merge queue accepts them
- **Auto-retry on diverge**: Up to 3 retries when target branch moves during CI
- **Stuck completing recovery**: Batches stuck in "completing" state are detected and resumed on next run
- **Stale batch recovery**: Auto-clears if PRs are already merged or batch > 30min old
- **Race condition guards**: Skip merged PRs, duplicates, recently-processed PRs
- **Auto-enqueue missed PRs**: `_sync_missing_prs` catches PRs missed due to concurrency cancellation
- **Fast-forward only**: Target branch updated via `force: false`
- **Atomic state writes**: `state.json` + `STATUS.md` committed in a single Git Trees API call
- **Atomic merge**: Entire stack merges or nothing does
- **Invariant rules**: 5 rules validated pre/post batch
- **Shell injection protection**: Workflow command passed via `$MQ_CMD` env var, not shell interpolation

### API Optimization

- Response caching: `list_open_prs`, `get_default_branch`, `list_mq_branches`, `list_rulesets`, `get_label_timestamp` cached per-run
- `QueueState` snapshot: one fetch, used by rules + queue logic (zero additional calls)
- Parallel cleanup: unlock + delete branches + post comments via ThreadPool
- Merged PRs skip label removal (inert on closed PRs)
- Rate limit tracking with low-remaining warnings
- API call budget tests enforce limits (3-PR stack <= 35 calls)

## Development

```bash
pip install -e ".[dev]"
ci/run                    # run all CI checks in parallel
```

See [CLAUDE.md](CLAUDE.md) for development workflow.

## Integration Testing

```bash
python integration/create_test_stacks.py run
```

Creates two stacks (one passing, one with syntax error), queues them, and verifies the pass stack merges while the fail stack is rejected.

The `LocalGitProvider` (`merge_queue/providers/local.py`) enables full integration testing against a local bare git repo without any GitHub API calls. It implements the same `GitHubClientProtocol` interface with in-memory PR metadata, labels, comments, rulesets, and deployments.

## Security

See [SECURITY_AUDIT.md](SECURITY_AUDIT.md) for the full security audit report.

Key mitigations in place:
- **No PR-branch code execution** -- workflow always installs from `main`, never from PR branches
- **Shell injection protection** -- commands passed via `$MQ_CMD` env var, not shell interpolation
- **Admin-only emergency labels** -- `break-glass` and `hotfix` restricted to repo admins + configured allowlist
- **Protected paths** -- sensitive files require authorized approval before entering the queue
