# Merge Queue Architecture

## System Overview

```
USER LABEL              WORKFLOW EVENT         COMMAND
──────────              ──────────────         ───────
+queue          ──►     labeled       ──►      enqueue
+hotfix         ──►     labeled       ──►      hotfix
+break-glass    ──►     labeled       ──►      break-glass
+re-test        ──►     labeled       ──►      retest
-queue          ──►     unlabeled     ──►      abort
(cron/dispatch) ──►     dispatch      ──►      process | status | check-rules
                                                  │
                                      python -m merge_queue $MQ_CMD
```

## Modules

| Module | Role |
|--------|------|
| `cli.py` | Argparse routing, orchestration, top-level error handling |
| `queue.py` | Pure logic: stack detection, FIFO ordering (no I/O) |
| `batch.py` | Batch lifecycle: lock, merge, CI dispatch/poll, complete/fail |
| `rules.py` | 5 invariant checks run pre/post batch |
| `store.py` | State persistence on `mq/state` branch (atomic writes) |
| `state.py` | `QueueState` snapshot dataclass (fetched once per run) |
| `status.py` | Markdown + terminal status rendering |
| `comments.py` | PR comment templates (single updating comment per PR) |
| `config.py` | Hand-rolled `merge-queue.yml` parser (no PyYAML) |
| `github_client.py` | GitHub API wrapper, caching, rate-limit tracking |
| `types.py` | Dataclasses: `PullRequest`, `Stack`, `Batch`, `QueueEntry`, etc. |
| `metrics/` | Optional OTLP or Prometheus push (factory pattern, noop default) |
| `providers/local.py` | `GitHubClientProtocol` impl for testing (bare repo, in-memory state) |

## Processing Flow (`do_process`)

1. **Load** -- `store.read()` state from `mq/state`; `config.get_target_branches()`
2. **Stuck batches** -- per target branch:
   - All PRs merged/closed → clear stale state
   - Age > 30min → `abort_batch()`, clear
   - `progress="completing"` → `_resume_completion()` (crash recovery)
   - Otherwise → skip (another run owns it)
3. **Sync** -- `_sync_missing_prs()` auto-enqueues labeled PRs not in state; `_cleanup_stale_entries()` removes unlabeled
4. **Pick branch** -- first branch with non-empty queue and no `active_batch`
5. **Protect** -- `ensure_branch_protection()` creates/verifies rulesets
6. **Rules** -- `rules.check_all()` validates 5 invariants
7. **Create batch** -- pop first stack (FIFO), lock branches via ruleset, `git merge --no-ff` each PR onto `mq/<target>/<batch_id>`, push
8. **Run CI** -- dispatch workflow, poll 15s intervals, 30min timeout
9. **Complete or fail**:
   - **Pass** → set `progress="completing"`, verify target hasn't diverged, retarget PRs, fast-forward target via `update_ref`, parallel cleanup
   - **Fail** → unlock, remove labels, delete branch, post failure details
   - **Diverged** → re-queue with `retry_count+1` (max 3), recursive `do_process()`

## Hotfix & Break-Glass Flows

```
HOTFIX                                BREAK-GLASS
──────                                ───────────
1. Auth check (admin/break_glass)     1. Auth check (admin/break_glass)
2. Abort active batch if any          2. Abort active batch if any
3. Re-queue aborted PRs               3. Re-queue aborted PRs
4. Insert hotfix at front of queue    4. Create batch, merge PR into target
5. do_process() (normal CI pipeline)  5. Skip CI entirely
                                      6. complete_batch() (fast-forward)
```

## State Schema (v2)

```jsonc
{
  "version": 2,
  "updated_at": "2026-04-03T12:00:00Z",
  "branches": {                          // keyed by target branch name
    "main": {
      "queue": [{                        // FIFO by queued_at
        "position": 1,
        "queued_at": "...",
        "stack": [{                      // ordered bottom-to-top
          "number": 42, "head_sha": "abc", "head_ref": "feat-a",
          "base_ref": "main", "title": "Add feature A"
        }],
        "deployment_id": 12345,
        "comment_ids": {"42": 99999},    // PR# → comment ID
        "target_branch": "main",
        "retry_count": 0
      }],
      "active_batch": {                  // null when idle
        "batch_id": "1712160000",
        "branch": "mq/main/1712160000",
        "ruleset_id": 789,
        "progress": "running_ci",        // locking → running_ci → completing
        "stack": [],
        "started_at": "...", "ci_started_at": "...", "queued_at": "...",
        "deployment_id": 12345,
        "comment_ids": {},
        "target_branch": "main"
      }
    }
  },
  "history": [                           // last N completed batches
    {"batch_id": "prev123", "status": "merged", "prs": [40, 41],
     "completed_at": "...", "target_branch": "main"}
  ]
}
```

`completing` is the crash-recovery marker -- `_resume_completion` detects and retries on next run. Auto-migration from v1 (flat) to v2 (branches dict) in `store.py`.

## Atomic State Writes

```
store.write_with_retry(mutate_fn)  ──  up to 7 retries, exponential backoff
  1. read() → state.json + cache SHA
  2. mutate_fn(state)
  3. _atomic_write: blobs → tree (base_tree) → commit → update ref (force=false)
  4. On 409/422 conflict → retry from 1
```

`mq/state` is excluded from branch protection (Git Data API needs direct writes). GitHub Actions concurrency group provides single-writer safety.

## Branch Protection Rulesets

| Ruleset | Pattern | Purpose |
|---------|---------|---------|
| `mq-protect-<branch>` | `refs/heads/main` etc. | Require PRs + CI; admin bypass for MQ fast-forward |
| `mq-lock-<batch_id>` | `refs/heads/<pr_branch>` | Lock PR branches during batch (deleted after) |
| `mq-branches-protect` | `refs/heads/mq/*` (excl. `mq/state`) | Only admin token can push batch branches |

All ruleset + `update_ref` operations use `MQ_ADMIN_TOKEN` to bypass protection.

## Two-Token Model

| Token | Used for |
|-------|----------|
| `GITHUB_TOKEN` | PR ops, comments, labels, deployments, CI dispatch |
| `MQ_ADMIN_TOKEN` | Rulesets, `update_ref` (fast-forward), branch locking |

## Tests

482+ tests, 90%+ line coverage enforced via `ci/test`.
