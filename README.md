# Merge Queue for Stacked PRs

[![CI](https://github.com/gbalke/merge_queue/actions/workflows/ci.yml/badge.svg)](https://github.com/gbalke/merge_queue/actions/workflows/ci.yml)
[![Coverage](https://img.shields.io/badge/coverage-96%25-brightgreen)](https://github.com/gbalke/merge_queue)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org)

A lightweight, Python-based merge queue for GitHub that understands stacked/chained PRs (e.g., created by [revup](https://github.com/Skydio/revup)).

## How It Works

1. Create stacked PRs where PR B targets PR A's branch
2. Add the `queue` label to PRs you want merged (contiguous from the bottom of the stack)
3. The merge queue processes stacks in **FIFO order**:
   - Locks PR branches via GitHub rulesets (with retry + verification)
   - Creates `mq/<batch-id>` branch, merges each PR `--no-ff`
   - Dispatches CI and polls for completion
   - On success: fast-forwards `main`, PRs auto-close as **"Merged"**
   - On failure: unlocks, notifies with failed job/step details + CI run link
4. After each batch, automatically processes the next queued stack
5. To abort: remove the `queue` label

## Architecture

```
merge_queue/
    cli.py               # Commands: enqueue, process, abort, status, check-rules
    queue.py             # Pure logic: stack detection, FIFO ordering
    batch.py             # Batch lifecycle: lock, merge, CI, complete/fail
    rules.py             # 5 invariant rules checked pre/post batch
    store.py             # Persistent state on mq/state branch
    status.py            # Markdown + terminal status rendering
    comments.py          # PR comment templates (single updating comment per PR)
    github_client.py     # GitHub API wrapper with rate limit tracking + caching
    types.py             # Dataclasses
tests/                   # 192 tests, 96% coverage (90% enforced)
integration/             # End-to-end test script (pass + fail stacks)
```

### Commands

| Command | Trigger | Description |
|---------|---------|-------------|
| `enqueue <pr>` | `queue` label added | Add stack to queue, start processing if idle |
| `process` | workflow_dispatch | Process next queued stack |
| `abort <pr>` | `queue` label removed | Abort batch or remove from queue |
| `status` | workflow_dispatch | Print current queue state |
| `check-rules` | workflow_dispatch | Run invariant rules |

### State Management

Queue state lives on the `mq/state` branch as `state.json`:
- Queue ordering (FIFO by enqueue timestamp)
- Active batch progress (locking -> running_ci -> completing)
- History with timing stats
- Comment IDs for single-comment updates

A rendered `STATUS.md` dashboard is auto-generated alongside.

### Live UI

Each batch creates a **GitHub Deployment** in the `merge-queue` environment with real-time status updates (queued -> in_progress -> success/failure). PR comments link directly to the deployment and CI run pages.

### PR Comments

One comment per PR, updated as state changes:

```
Merge Queue — Merged to main.

| Phase      | Duration |
|:-----------|:---------|
| Queue wait | 5s       |
| CI + merge | 1m 8s    |
| Total      | 1m 13s   |

Commits:
- #38 greg/revup/main/ci-coverage — Enforce coverage in CI
- #39 greg/revup/main/readme-badges — Add badges to README

View CI run ->
View merge queue ->
```

Failed comments include the job name, failed step, and link to the CI run.

### Safety

- **Branch locking**: Rulesets with retry + verification (lock before merge, rollback on failure)
- **Stale batch recovery**: Auto-clears if PRs are already merged or batch > 30min old
- **Race condition guards**: Skip merged PRs, duplicates, recently-processed PRs
- **Fast-forward only**: `main` updated via `force: false`
- **Atomic**: Entire stack merges or nothing does
- **Invariant rules**: 5 rules validated pre/post batch

### API Optimization

- Response caching: `list_open_prs`, `get_default_branch`, `list_mq_branches`, `list_rulesets`, `get_label_timestamp` cached per-run
- `QueueState` snapshot: one fetch, used by rules + queue logic (zero additional calls)
- Parallel cleanup: unlock + delete branches + post comments via ThreadPool
- Merged PRs skip label removal (inert on closed PRs)
- Rate limit tracking with low-remaining warnings
- API call budget tests enforce limits (3-PR stack <= 35 calls)

## Integration with External Repos

Install merge-queue in any GitHub repository:

1. Copy [`examples/merge-queue.yml`](examples/merge-queue.yml) to `.github/workflows/merge-queue.yml`
2. Change `MQ_CI_WORKFLOW` to your CI workflow filename (e.g., `test.yml`, `ci.yml`)
3. Create labels: `queue`, `locked`, `re-test`
4. Add `MQ_ADMIN_TOKEN` secret (fine-grained PAT with Administration: Write)
5. Add `queue` label to PRs to start using the merge queue

The merge queue auto-detects your repository's default branch.

## Setup

### 1. Repository Settings

- Enable "Automatically delete head branches"
- Actions permissions: read/write

### 2. Labels

- `queue` — triggers the merge queue
- `locked` — auto-managed, indicates branch is locked

### 3. `MQ_ADMIN_TOKEN` Secret

Branch locking requires a fine-grained PAT with **Administration: Read and Write**:

```bash
gh secret set MQ_ADMIN_TOKEN --repo <owner>/<repo>
```

### 4. Install and Test

```bash
pip install -e ".[dev]"
pytest tests/              # 192 tests, 90%+ coverage enforced
```

## Integration Testing

```bash
python integration/create_test_stacks.py run
```

Creates two stacks (one passing, one with syntax error), queues them, and verifies the pass stack merges while the fail stack is rejected.
