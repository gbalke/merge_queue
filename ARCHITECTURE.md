# Merge Queue Architecture

## System Overview

```
  USER ACTION                    GITHUB ACTIONS                    GITHUB API
  -----------                    --------------                    ----------
                                 merge-queue.yml
  Add 'queue'  ----------------> labeled  -> enqueue
  Add 'hotfix' ----------------> labeled  -> hotfix
  Add 'break-glass' -----------> labeled  -> break-glass
  Add 're-test' ---------------> labeled  -> retest
  Remove 'queue' --------------> unlabeled -> abort
                                 dispatch  -> process | status | check-rules
                                            |
                                            v
                                 python -m merge_queue $MQ_CMD
                                            |
                 +--------------------------+---------------------------+
                 |                          |                           |
            enqueue/hotfix/          process                     abort
            break-glass              (cli.py)                    (cli.py)
            (cli.py)                    |                           |
                 |                      v                           v
                 v                 See flow below             Delete rulesets
            Comment on PR                                    Remove 'locked'
            _sync_missing_prs()                              Delete mq/* branch
            Check if idle --yes--> do_process()
```

## Processing Flow (do_process)

```
do_process(client)
|
+- 1. LOAD STATE
|     store.read() -> state.json from mq/state branch
|     config.get_target_branches() -> merge-queue.yml from default branch
|
+- 2. DETECT STUCK BATCHES (per target branch)
|     |
|     +-- active_batch exists?
|           |
|           +-- All PRs merged/closed? -> clear stale state
|           +-- Age > 30min? -> abort_batch(), clear state
|           +-- progress="completing"? -> _resume_completion()
|                 (previous run was cancelled mid-merge; reconstruct Batch, retry)
|           +-- Otherwise -> skip (another run is handling it)
|
+- 3. SYNC STATE
|     _sync_missing_prs() -- auto-enqueue PRs with 'queue' label not in state
|     _cleanup_stale_entries() -- remove entries for PRs without 'queue' label
|
+- 4. FIND NEXT BRANCH TO PROCESS (FIFO, per-branch queue)
|     First branch with non-empty queue and no active_batch
|
+- 5. ENSURE BRANCH PROTECTION
|     ensure_branch_protection() -> creates mq-protect-* rulesets
|     _ensure_mq_branches_protected() -> creates mq-branches-protect
|
+- 6. RUN PRE-CONDITION RULES
|     rules.check_all() -> single_active_batch, locked_prs_have_rulesets,
|                           no_orphaned_locks, queue_order_is_fifo, stack_integrity
|
+- 7. POP FIRST STACK (FIFO), CREATE BATCH
|     batch.create_batch():
|       a. Lock PR branches via ruleset (MQ_ADMIN_TOKEN)
|       b. Add 'locked' label to each PR
|       c. git checkout -b mq/<target>/<batch_id>
|       d. For each PR: fetch, verify SHA, merge --no-ff
|       e. git push
|       On failure: delete ruleset, remove 'locked' labels
|
+- 8. RUN CI
|     batch.run_ci():
|       a. Dispatch CI workflow on mq/<target>/<batch_id>
|       b. Poll for run to appear (5s intervals)
|       c. Poll for completion (15s intervals, 30min timeout)
|
+- 9. COMPLETE OR FAIL
      |
      +-- CI passed:
      |     Set progress="completing" in state (crash recovery marker)
      |     batch.complete_batch():
      |       a. Verify target branch hasn't diverged (compare_commits)
      |       b. Retarget all PRs to target branch
      |       c. Fast-forward target branch (update_ref via admin token)
      |       d. Set commit status on new HEAD
      |       e. Parallel cleanup: unlock ruleset, delete branches, post comments
      |     On 422 from update_ref -> BatchError("diverged") -> auto-retry
      |
      +-- CI failed:
      |     fail_batch(): unlock, remove labels, delete branch
      |
      +-- Diverged (target branch moved):
            Re-queue entry with retry_count+1 (max 3 retries)
            Recursive do_process()
```

## Hotfix and Break-Glass Flows

```
HOTFIX (do_hotfix)                    BREAK-GLASS (do_break_glass)
-----------------                     --------------------------
Auth check (admin/break_glass_users)  Auth check (same)
       |                                     |
       v                                     v
Abort active batch if any             Abort active batch if any
Re-queue aborted PRs behind hotfix   Re-queue aborted PRs
       |                                     |
       v                                     v
Insert at front of queue              Create batch (merge PR into target)
       |                                     |
       v                                     v
do_process() (normal CI pipeline)     Skip CI entirely
                                             |
                                             v
                                      complete_batch() (fast-forward)
```

## Atomic State Writes

State is stored as `state.json` + `STATUS.md` files on the `mq/state` branch.

**Problem**: Sequential writes via the Contents API (one file per commit) caused
409 SHA races when concurrent workflows updated state.

**Solution**: `commit_files()` uses the Git Trees API to bundle `state.json` +
all `STATUS.md` files into a single atomic commit (blobs -> tree -> commit ->
update ref).

```
store.write_with_retry(mutate_fn):
  loop (up to 7 retries, exponential backoff):
    1. read() -> state.json + cache SHA
    2. mutate_fn(state)
    3. _atomic_write(state):
         - Create blobs for state.json + STATUS.md files
         - Create tree with base_tree (inherits unchanged files)
         - Create commit with single parent
         - Update ref (force=false)
    4. On 409/422 conflict -> retry from step 1
```

The `mq/state` branch is explicitly excluded from the `mq-branches-protect`
ruleset because branch protection blocks the Git Data API writes. The GitHub
Actions concurrency group provides single-writer safety instead.

## Branch Protection

`ensure_branch_protection()` manages three kinds of rulesets:

| Ruleset | Pattern | Purpose |
|---------|---------|---------|
| `mq-protect-<branch>` | `refs/heads/main` etc. | Require PRs + CI for target branches. Admin role bypass lets MQ fast-forward. |
| `mq-lock-<batch_id>` | `refs/heads/<pr_branch>` | Lock PR branches during batch. Prevents pushes (GH013). Deleted after merge/fail. |
| `mq-branches-protect` | `refs/heads/mq/*` (excludes `mq/state`) | Only admin token can push to mq/* batch branches. |

All ruleset operations use `MQ_ADMIN_TOKEN` (admin session) to bypass protection.
`update_ref` also uses the admin token so the fast-forward succeeds despite rulesets.

## Auto-Retry on Diverge

When `complete_batch()` detects the target branch has moved:

1. `compare_commits` returns status != "ahead" -> raise BatchError
2. If `update_ref` returns 422 -> wrap as BatchError("diverged")
3. `cli.py` catches "diverged", calls `fail_batch()`, re-queues entry with `retry_count + 1`
4. Recursive `do_process()` rebuilds the batch from the new target tip
5. After 3 retries (4 total attempts), gives up and fails

## State Schema (v2)

```json
{
  "version": 2,
  "updated_at": "2026-04-03T12:00:00Z",
  "branches": {
    "main": {
      "queue": [
        {
          "position": 1,
          "queued_at": "2026-04-03T11:55:00Z",
          "stack": [{"number": 42, "head_sha": "abc", "head_ref": "feat-a", "base_ref": "main", "title": "Add feature A"}],
          "deployment_id": 12345,
          "comment_ids": {42: 99999},
          "target_branch": "main",
          "retry_count": 0
        }
      ],
      "active_batch": {
        "batch_id": "1712160000",
        "branch": "mq/main/1712160000",
        "ruleset_id": 789,
        "started_at": "...",
        "progress": "running_ci",
        "stack": [...],
        "deployment_id": 12345,
        "comment_ids": {...},
        "queued_at": "...",
        "ci_started_at": "...",
        "target_branch": "main"
      }
    }
  },
  "history": [
    {"batch_id": "prev123", "status": "merged", "prs": [40, 41], "completed_at": "...", "target_branch": "main"}
  ]
}
```

`active_batch.progress` values: `locking` -> `running_ci` -> `completing`.
The `completing` state is the crash-recovery marker: if a run is cancelled
mid-merge, the next `do_process` call detects it and resumes via `_resume_completion`.

Auto-migration from v1 (flat queue/active_batch) to v2 (branches dict) happens
transparently in `store.py` via `_migrate_v1_to_v2()`.

## Module Dependencies

```
                    merge-queue.yml (GitHub Actions workflow)
                          | python -m merge_queue
                          v
                    +----------+
                    |  cli.py  |  argparse routing + orchestration
                    +----+-----+
                         |
         +---------------+-------------------+
         |               |                   |
         v               v                   v
   +-----------+  +-----------+        +-----------+
   | queue.py  |  | batch.py  |        | rules.py  |
   | Pure logic|  | Lifecycle |        | Invariant |
   | No I/O    |  | + git     |        | checks    |
   +-----------+  +-----------+        +-----------+
         |               |                   |
         v               v                   v
   +---------------------------------------------+
   |               types.py                       |
   |  PullRequest, Stack, Batch, QueueEntry,      |
   |  BatchStatus, RuleResult                     |
   +----------------------+-----------------------+
                          |
         +----------------+----------------+
         v                v                v
   +-----------+   +-----------+   +-----------+
   | store.py  |   | config.py |   | status.py |
   | state     |   | YAML cfg  |   | Markdown  |
   | persist   |   | parser    |   | render    |
   +-----+-----+   +-----+-----+   +-----------+
         |               |
         v               v
   +---------------------------------------+
   |       github_client.py                |
   |  GitHubClientProtocol interface       |
   |  requests-based GitHub API            |
   |  GITHUB_TOKEN + MQ_ADMIN_TOKEN        |
   +-------------------+-------------------+
                       |
                       v
   +---------------------------------------+
   |     providers/local.py                |
   |  LocalGitProvider                     |
   |  Bare git repo + in-memory state      |
   |  No GitHub API calls                  |
   +---------------------------------------+
```

### Key design decisions

- **Two tokens**: `GITHUB_TOKEN` for most operations, `MQ_ADMIN_TOKEN` for
  rulesets and `update_ref` (bypasses branch protection).
- **Config without PyYAML**: `config.py` parses `merge-queue.yml` with a
  hand-rolled line parser. Sections: `break_glass_users`, `target_branches`,
  `protected_paths` (with optional per-path `approvers`).
- **Caching**: `github_client.py` caches open PRs, default branch, mq branches,
  and rulesets within a single process run. `invalidate_cache()` clears after writes.
- **Parallel cleanup**: `complete_batch` runs unlock, branch deletion, and
  comment posting in parallel via ThreadPoolExecutor after the fast-forward.

## ci/ Scripts

The `ci/` directory is the single source of truth for CI checks, used by both
local development and GitHub Actions:

| Script | What it does |
|--------|-------------|
| `ci/run` | Runs all jobs in parallel, reports pass/fail |
| `ci/lint` | `py_compile` syntax check + `ruff check` |
| `ci/format` | `ruff format --check` |
| `ci/test` | `pytest tests/ -x -q` with 90%+ coverage enforcement |

## Test Coverage

```
  Module                    Tests   What's tested
  -------------------------------------------------------------------
  test_cli.py                 19    Core commands, process loop
  test_cli_extra.py           24    Edge cases, error paths
  test_batch.py               36    Create, complete, fail, abort, unlock
  test_queue.py               28    Stack detection, FIFO, validation
  test_rules.py               16    All 5 invariant rules
  test_comments_extra.py      27    Comment rendering
  test_store.py               10    State persistence, v1->v2 migration
  test_store_extra.py         13    Concurrency, conflict retry
  test_status.py              14    Markdown + terminal rendering
  test_status_extra.py         4    Additional status edge cases
  test_configurable_ci.py     11    CI workflow dispatch config
  test_api_calls.py           16    API calls, rate limiting
  test_rate_limit.py           6    Rate limit tracking
  test_sanitize.py            15    Input sanitization
  test_branch_protection.py   41    Ruleset creation, verification, mq-branches-protect
  test_break_glass.py         28    Break-glass authorization
  test_break_glass_merge.py    5    Break-glass end-to-end merge
  test_ci_gate.py             12    CI gate enforcement
  test_ci_gate_tdd.py          9    CI gate TDD scenarios
  test_multi_branch.py        25    Multi-target-branch queues
  test_per_branch.py          12    Per-branch state and STATUS.md
  test_sync_missing.py        12    _sync_missing_prs auto-enqueue
  test_cleanup_stale.py        9    Stale entry cleanup
  test_auto_rebase.py          6    Auto-retry on diverge
  test_hotfix_queue.py         3    Hotfix front-of-queue + abort
  test_stuck_completing.py     6    Stuck completion detection + resume
  test_diverged_complete.py    2    Diverged complete_batch paths
  test_protected_paths.py     56    Protected paths + per-path approvers
  test_metrics.py             17    Metrics backends + push_batch_metrics
  -------------------------------------------------------------------
  TOTAL                      482 tests, 90%+ coverage enforced
```
