# Merge Queue Architecture

## System Overview

```
  USER ACTION                    GITHUB ACTIONS                    GITHUB API
  ───────────                    ──────────────                    ──────────
                                 ┌──────────────────────┐
  Add 'queue'  ───────────────>  │  merge-queue.yml      │
  label to PR                    │  (workflow trigger)    │
                                 │                        │
  Add 'hotfix' ───────────────>  │  Determines command:   │
  label to PR                    │  labeled  -> enqueue   │
                                 │  labeled  -> hotfix    │
  Add 're-test' ──────────────>  │  labeled  -> retest    │
  label to PR                    │  unlabel  -> abort     │
                                 │  dispatch -> process   │
  Remove 'queue' ─────────────>  │  dispatch -> status    │
  label from PR                  │  dispatch -> check-rules│
                                 └──────────┬─────────────┘
                                            │
                                            v
                                 ┌──────────────────────┐
                                 │  python -m merge_queue │
                                 │  $MQ_CMD (via env var) │
                                 └──────────┬─────────────┘
                                            │
                 ┌──────────────────────────┼──────────────────────────┐
                 │                          │                          │
                 v                          v                          v
          ┌─────────────┐          ┌──────────────┐          ┌──────────────┐
          │  enqueue     │          │  process      │          │  abort        │
          │  hotfix      │          │  (cli.py)     │          │  (cli.py)     │
          │  (cli.py)    │          │               │          │               │
          └──────┬──────┘          └──────┬───────┘          └──────┬───────┘
                 │                        │                         │
                 v                        v                         v
          Comment on PR           See flow below              Delete rulesets
          _sync_missing_prs()                                 Remove 'locked'
          Check if idle ──yes──>  do_process()                Delete mq/* branch
            │
            no
            │
            v
          "Waiting for
           processor"
```

## Processing Flow (do_process)

```
do_process(client)
│
├─ 1. LOAD CONFIG
│     │
│     ├── config.load_config(client)
│     │     └── Read merge-queue.yml from default branch
│     │         Parse: break_glass_users, target_branches
│     │
├─ 2. CHECK ACTIVE BATCH (per target branch)
│     │
│     ├── mq/<target>/* branch exists? ──yes──> return "batch_active"
│     │                                         (another run is handling it)
│     no
│     │
├─ 3. RUN PRE-CONDITION RULES
│     │
│     ├── rules.check_all()
│     │     ├── single_active_batch     (at most 1 mq/ branch per target)
│     │     ├── locked_prs_have_rulesets (locked PRs covered by ruleset)
│     │     ├── no_orphaned_locks       (no locked PRs without mq/ branch)
│     │     ├── queue_order_is_fifo     (active batch is earliest-queued)
│     │     └── stack_integrity         (stacks form valid chains)
│     │
│     ├── Any rule fails? ──yes──> return "rules_failed"
│     │
│     no
│     │
├─ 4. FIND NEXT STACK (FIFO, per-branch queue)
│     │
│     ├── fetch_queued_prs()
│     │     └── List open PRs with 'queue' label
│     │         For each: get label timestamp via Timeline API
│     │
│     ├── detect_stacks()
│     │     └── Group PRs by base_ref chains:
│     │           main <─ PR#1 <─ PR#2 <─ PR#3  = one stack
│     │           release/1.0 <─ PR#4            = another stack (different queue)
│     │
│     ├── order_queue() per target branch
│     │     └── Sort stacks by earliest queued_at (FIFO)
│     │
│     ├── select_next()
│     │     └── Pick first stack from first available branch queue
│     │
│     ├── No stacks? ──yes──> return "no_stacks"
│     │
│     no
│     │
├─ 5. CI GATE CHECK
│     │
│     ├── All PRs in stack must have passing CI
│     │     └── Unless break-glass label applied by authorized user
│     │
│     ├── CI not passing? ──yes──> Comment, skip stack
│     │
│     no
│     │
├─ 6. CREATE BATCH (batch.create_batch)
│     │
│     │   ┌──────────────────────────────────────────────────────┐
│     │   │  a. Create ruleset locking PR branches               │
│     │   │     (MQ_ADMIN_TOKEN -> GitHub Rulesets API)          │
│     │   │     Pushes to PR branches now rejected: GH013        │
│     │   │                                                      │
│     │   │  b. Add 'locked' label to each PR                   │
│     │   │                                                      │
│     │   │  c. git checkout -b mq/<target>/<batch_id>          │
│     │   │                                                      │
│     │   │  d. For each PR in stack (bottom to top):            │
│     │   │       git fetch origin <head_ref>                    │
│     │   │       verify SHA matches (optimistic lock)           │
│     │   │       git merge --no-ff origin/<head_ref>            │
│     │   │                                                      │
│     │   │  e. git push origin mq/<target>/<batch_id>          │
│     │   │                                                      │
│     │   │  If c-e fail: delete ruleset, remove 'locked' labels │
│     │   └──────────────────────────────────────────────────────┘
│     │
│     ├── BatchError? ──yes──> Comment, remove 'queue', return "batch_error"
│     │
│     no
│     │
├─ 7. RUN CI (batch.run_ci)
│     │
│     │   ┌──────────────────────────────────────────────────────┐
│     │   │  a. Dispatch CI workflow on mq/<target>/<batch_id>   │
│     │   │     (workflow_dispatch -> $MQ_CI_WORKFLOW)            │
│     │   │                                                      │
│     │   │  b. Poll for CI run to appear (5s intervals)         │
│     │   │                                                      │
│     │   │  c. Poll for completion (15s intervals, 30min max)   │
│     │   └──────────────────────────────────────────────────────┘
│     │
│     ├── CI failed? ──yes──> fail_batch() -> return "ci_failed"
│     │
│     no
│     │
├─ 8. COMPLETE BATCH (batch.complete_batch)
│     │
│     │   ┌──────────────────────────────────────────────────────┐
│     │   │  a. Verify optimistic locks (PR SHAs unchanged)      │
│     │   │                                                      │
│     │   │  b. Verify target branch hasn't diverged             │
│     │   │     └── If diverged: auto-retry (up to 3 times)     │
│     │   │         Recreate mq/ branch from new target tip      │
│     │   │         Re-merge stack, re-run CI                    │
│     │   │                                                      │
│     │   │  c. Retarget all PRs to target branch                │
│     │   │     (so GitHub sees "new commits" on each PR)        │
│     │   │                                                      │
│     │   │  d. Fast-forward target branch to mq/ tip            │
│     │   │     (git.updateRef, force=false)                     │
│     │   │                                                      │
│     │   │  e. GitHub detects PR commits reachable from target  │
│     │   │     -> PRs marked MERGED (purple)                    │
│     │   │                                                      │
│     │   │  f. Delete lock ruleset (branches unlocked)          │
│     │   │                                                      │
│     │   │  g. Remove 'locked' + 'queue' labels                │
│     │   │                                                      │
│     │   │  h. Comment "Successfully merged"                    │
│     │   │                                                      │
│     │   │  i. Delete mq/ branch + PR head branches             │
│     │   └──────────────────────────────────────────────────────┘
│     │
│     ├── BatchError? ──yes──> fail_batch() -> return "complete_error"
│     │
│     no
│     │
├─ 9. CHECK FOR MORE QUEUED STACKS
│     │
│     ├── More stacks with 'queue' label?
│     │     └── yes -> Dispatch merge-queue.yml (command=process)
│     │                (self-dispatch for next batch)
│     │
│     └── return "merged"
```

## Failure & Abort Flows

```
fail_batch(client, batch, reason)        abort_batch(client)
│                                        │
├── Delete lock ruleset                  ├── Find all mq-lock-* rulesets
├── Remove 'locked' label from PRs       │     └── Delete each
├── Remove 'queue' label from PRs        ├── Find all PRs with 'locked'
├── Comment with failure reason           │     └── Remove label
├── Delete mq/* branch                   └── Delete all mq/* branches
└── batch.status = FAILED

                              ABORT TRIGGER
                              ─────────────
                    User removes 'queue' label from locked PR
                                    │
                                    v
                             merge-queue.yml
                             (unlabeled event)
                                    │
                                    v
                         python -m merge_queue abort <pr>
                                    │
                              ┌─────┴──────┐
                              │ PR locked?  │
                              └─────┬──────┘
                               no   │  yes
                               │    │
                               v    v
                            noop    abort_batch()
                                    + comment "Aborted"
```

## Queue Ordering (FIFO, Per-Branch)

```
EXAMPLE: Two stacks queued to different target branches

  Time ──────────────────────────────────────────────>

  T=0: User labels PR#4 with 'queue'    (stack B -> main, position 1)
  T=1: User labels PR#1 with 'queue'    (stack A -> main, position 2)
  T=2: User labels PR#7 with 'queue'    (stack C -> release/1.0, position 1)
  T=3: User labels PR#2 with 'queue'    (stack A -> main, same position)

  Stack detection:
    Stack A: main <─ PR#1 <─ PR#2            queued_at = T=1
    Stack B: main <─ PR#4                    queued_at = T=0
    Stack C: release/1.0 <─ PR#7             queued_at = T=2

  Per-branch FIFO:
    main queue:         Stack B (T=0), then Stack A (T=1)
    release/1.0 queue:  Stack C (T=2)

  Processing:
    1. Stack B: mq/main/<id>, merge PR#4, CI, merge to main
    2. Stack C: mq/release/1.0/<id>, merge PR#7, CI, merge to release/1.0
       (can run after Stack B since it targets a different branch)
    3. Stack A: mq/main/<id>, merge PR#1 + PR#2, CI, merge to main
```

## Branch State During Merge

```
BEFORE:
  main:               A───B───C
  release/1.0:        A───B───X───Y
  feat-a:             A───B───C───D         (PR#1 targets main)
  feat-b:             A───B───C───D───E     (PR#2 targets feat-a)

DURING (per-branch mq/ branch created):
  main:               A───B───C
  mq/main/abc123:     A───B───C───M1────M2  (M1 = merge feat-a, M2 = merge feat-b)
  feat-a:             LOCKED (ruleset)
  feat-b:             LOCKED (ruleset)

AFTER (main fast-forwarded):
  main:               A───B───C───M1────M2
  feat-a:             DELETED
  feat-b:             DELETED
  mq/main/abc123:     DELETED
  PR#1:               MERGED (purple)
  PR#2:               MERGED (purple)

PER-BRANCH STATE BRANCHES:
  mq/state            state.json (v2), STATUS.md (root index)
                      + STATUS/<branch>.md (per-branch dashboard)
```

## State Schema (v2)

The queue state is stored as `state.json` on the `mq/state` branch. Version 2
introduced per-branch queues:

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
          "stack": [{"number": 42, "head_sha": "abc123", "head_ref": "feat-a", "base_ref": "main", "title": "Add feature A"}],
          "deployment_id": 12345
        }
      ],
      "active_batch": null
    },
    "release/1.0": {
      "queue": [],
      "active_batch": null
    }
  },
  "history": [
    {"batch_id": "prev123", "status": "merged", "prs": [40, 41], "merged_at": "2026-04-03T11:50:00Z"}
  ]
}
```

Automatic migration from v1 (flat queue/active_batch) to v2 (branches dict) happens
transparently in `store.py` on first read via `_migrate_v1_to_v2()`.

## Module Dependencies

```
  ┌───────────────────────────────────────────────────────┐
  │                    merge-queue.yml                     │
  │               (GitHub Actions workflow)                │
  │            Command via $MQ_CMD env var                 │
  └───────────────────────┬───────────────────────────────┘
                          │ python -m merge_queue
                          v
                    ┌───────────┐
                    │  cli.py   │  argparse routing
                    └─────┬─────┘
                          │
          ┌───────────────┼───────────────────┐
          │               │                   │
          v               v                   v
    ┌───────────┐  ┌──────────┐        ┌───────────┐
    │ queue.py  │  │ batch.py │        │ rules.py  │
    │           │  │          │        │           │
    │ Pure logic│  │ Lifecycle│        │ Invariant │
    │ No I/O   │  │ + git    │        │ checks    │
    └─────┬─────┘  └────┬─────┘        └─────┬─────┘
          │              │                    │
          v              v                    v
    ┌─────────────────────────────────────────────┐
    │               types.py                      │
    │  PullRequest, Stack, Batch, QueueEntry,     │
    │  BatchStatus, RuleResult                    │
    └──────────────────┬──────────────────────────┘
                       │
          ┌────────────┼────────────┐
          v            v            v
    ┌──────────┐ ┌──────────┐ ┌──────────┐
    │ store.py │ │config.py │ │status.py │
    │ state    │ │ YAML cfg │ │ Markdown │
    │ persist  │ │ parser   │ │ render   │
    └────┬─────┘ └────┬─────┘ └──────────┘
         │            │
         v            v
    ┌───────────────────────────────────┐
    │       github_client.py            │
    │  GitHubClientProtocol interface   │
    │  requests-based GitHub API        │
    │  GITHUB_TOKEN + MQ_ADMIN_TOKEN    │
    └───────────────┬───────────────────┘
                    │
                    │  (alternative impl for testing)
                    v
    ┌───────────────────────────────────┐
    │     providers/local.py            │
    │  LocalGitProvider                 │
    │  Bare git repo + in-memory state  │
    │  No GitHub API calls              │
    └───────────────────────────────────┘
```

## Test Coverage

```
  Module             Tests   What's tested
  ──────────────────────────────────────────────────────────────────
  types.py           -       Covered transitively
  queue.py           28      Stack detection, FIFO, validation
  batch.py           36      Create, complete, fail, abort, unlock, auto-retry
  rules.py           16      All 5 invariant rules
  cli.py             43      All commands, process loop, error paths
  comments.py        27      Comment rendering, edge cases
  config.py          8       Configurable CI workflow dispatch
  status.py          18      Markdown + terminal status rendering
  store.py           17      State persistence, v1->v2 migration, concurrency
  state.py           -       Covered via cli/store tests
  github_client.py   22      API calls, rate limiting, caching
  providers/local.py -       Used as test infrastructure
  ──────────────────────────────────────────────────────────────────

  Additional test suites:
    test_auto_rebase.py        4   Auto-retry on diverge
    test_branch_protection.py  19  Ruleset creation and verification
    test_break_glass.py        26  Break-glass authorization
    test_ci_gate.py            10  CI gate enforcement
    test_ci_gate_tdd.py        9   CI gate TDD scenarios
    test_multi_branch.py       18  Multi-target-branch queues
    test_per_branch.py         11  Per-branch state and STATUS.md
    test_sync_missing.py       12  _sync_missing_prs auto-enqueue

  TOTAL              337+ tests, 90%+ coverage enforced
```
