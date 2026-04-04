# Merge Queue for Stacked PRs

[![CI](https://github.com/gbalke/merge_queue/actions/workflows/ci.yml/badge.svg)](https://github.com/gbalke/merge_queue/actions/workflows/ci.yml)
[![Coverage](https://img.shields.io/badge/coverage-85%25+-brightgreen)](https://github.com/gbalke/merge_queue)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

A lightweight, Python-based merge queue for GitHub that understands stacked/chained PRs (e.g., created by [revup](https://github.com/Skydio/revup)).

## How It Works

1. Create stacked PRs where PR B targets PR A's branch
2. Add the `queue` label to PRs you want merged (must be contiguous from the bottom of the stack)
3. The merge queue processes stacks in **FIFO order** (earliest-labeled first):
   - **Locks PR branches** via GitHub rulesets (prevents pushes)
   - Adds the `locked` label for visibility
   - Creates a temporary `mq/<batch-id>` branch from `main`
   - Merges each PR's branch in order (`--no-ff`)
   - Dispatches CI on the batch branch and waits for completion
   - If CI passes: fast-forwards `main`, PRs auto-close as **"Merged"** (purple)
   - If CI fails: unlocks, removes labels, notifies
4. After completion, automatically processes the next queued stack
5. To **abort**: remove the `queue` label from any locked PR

## Architecture

The merge queue is a tested Python package. GitHub Actions workflows are thin shells that invoke `python -m merge_queue <command>`.

```
merge_queue/
    types.py             # Dataclasses: PullRequest, Stack, Batch, RuleResult
    queue.py             # Pure logic: stack detection, FIFO ordering (95% test coverage)
    batch.py             # Batch lifecycle: lock, merge, CI, complete/fail
    rules.py             # Invariant rules checked pre/post batch
    github_client.py     # Thin GitHub API wrapper (requests)
    cli.py               # CLI: enqueue, process, abort, check-rules
tests/
    test_queue.py        # 26 tests — stack detection, FIFO ordering
    test_batch.py        # 10 tests — lifecycle with mocked client
    test_rules.py        # 12 tests — each invariant rule
    test_cli.py          # 8 tests — command routing
```

### Commands

| Command | Trigger | Description |
|---------|---------|-------------|
| `enqueue <pr>` | `queue` label added | Register PR in queue, start processing if idle |
| `process` | workflow_dispatch | Process next queued stack or complete active batch |
| `abort <pr>` | `queue` label removed | Abort active batch if PR is locked |
| `check-rules` | workflow_dispatch | Run all invariant rules, exit 1 if any fail |

### Invariant Rules

Rules are checked before/after each batch and via `check-rules`:

1. **single_active_batch** — at most one `mq/*` branch exists
2. **locked_prs_have_rulesets** — every locked PR has a matching lock ruleset
3. **no_orphaned_locks** — no `locked` PRs without an active batch
4. **queue_order_is_fifo** — active batch has the earliest queue timestamp
5. **stack_integrity** — each stack forms a valid chain to main

## Labels

| Label | Purpose |
|-------|---------|
| `queue` | Add to PRs to enter the merge queue. Remove to abort. |
| `locked` | Auto-added when branches are locked. Auto-removed on completion/abort. |

## Queue Ordering

Stacks are processed in **FIFO order**. The queue position is determined by when the `queue` label was first added (via GitHub's timeline API). PRs labeled together (same stack) share a position.

## Setup

### 1. Repository Settings

- **Settings > General > Pull Requests**: Enable "Automatically delete head branches"
- **Settings > Actions > General**: Ensure workflows have read/write permissions

### 2. Create Labels

- `queue` (green) — triggers the merge queue
- `locked` (red) — indicates branch is locked

### 3. Create `MQ_ADMIN_TOKEN` Secret

Branch locking requires a fine-grained PAT with **Administration: Read and Write**:

1. Go to https://github.com/settings/tokens?type=beta
2. Scope to your merge queue repository
3. Grant **Administration: Read and Write** permission
4. Add as a repository secret:
   ```bash
   gh secret set MQ_ADMIN_TOKEN --repo <owner>/<repo>
   ```

Without this secret, the merge queue still works but branches won't be locked during processing.

### 4. Install

```bash
pip install -e .         # install package
pytest tests/ -v         # run tests (73%+ coverage enforced)
```

## Safety

- **Branch locking**: PR branches locked via rulesets while in queue
- **Optimistic locking**: SHA verification catches concurrent modifications
- **Fast-forward only**: `main` updated via fast-forward (`force: false`)
- **Single batch**: One batch at a time (concurrency group)
- **Atomic**: Entire stack merges or nothing does
- **Abort**: Remove `queue` label to abort and unlock
- **Invariant rules**: Validated pre/post batch
