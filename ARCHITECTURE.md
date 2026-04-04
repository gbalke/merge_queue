# Merge Queue Architecture

## System Overview

```
  USER ACTION                    GITHUB ACTIONS                    GITHUB API
  ───────────                    ──────────────                    ──────────
                                 ┌──────────────────────┐
  Add 'queue'  ───────────────>  │  merge-queue.yml      │
  label to PR                    │  (workflow trigger)    │
                                 │                        │
                                 │  Determines command:   │
                                 │  labeled  -> enqueue   │
                                 │  unlabel  -> abort     │
                                 │  dispatch -> process   │
                                 └──────────┬─────────────┘
                                            │
                                            v
                                 ┌──────────────────────┐
                                 │  python -m merge_queue │
                                 │  <command> <args>      │
                                 └──────────┬─────────────┘
                                            │
                 ┌──────────────────────────┼──────────────────────────┐
                 │                          │                          │
                 v                          v                          v
          ┌─────────────┐          ┌──────────────┐          ┌──────────────┐
          │  enqueue     │          │  process      │          │  abort        │
          │  (cli.py)    │          │  (cli.py)     │          │  (cli.py)     │
          └──────┬──────┘          └──────┬───────┘          └──────┬───────┘
                 │                        │                         │
                 v                        v                         v
          Comment on PR           See flow below              Delete rulesets
          Check if idle ──yes──>  do_process()                Remove 'locked'
            │                                                 Delete mq/* branch
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
├─ 1. CHECK ACTIVE BATCH
│     │
│     ├── mq/* branch exists? ──yes──> return "batch_active"
│     │                                (another run is handling it)
│     no
│     │
├─ 2. RUN PRE-CONDITION RULES
│     │
│     ├── rules.check_all()
│     │     ├── single_active_batch     (at most 1 mq/ branch)
│     │     ├── locked_prs_have_rulesets (locked PRs covered by ruleset)
│     │     ├── no_orphaned_locks       (no locked PRs without mq/ branch)
│     │     ├── queue_order_is_fifo     (active batch is earliest-queued)
│     │     └── stack_integrity         (stacks form valid chains)
│     │
│     ├── Any rule fails? ──yes──> return "rules_failed"
│     │
│     no
│     │
├─ 3. FIND NEXT STACK (FIFO)
│     │
│     ├── fetch_queued_prs()
│     │     └── List open PRs with 'queue' label
│     │         For each: get label timestamp via Timeline API
│     │
│     ├── detect_stacks()
│     │     └── Group PRs by base_ref chains:
│     │           main <─ PR#1 <─ PR#2 <─ PR#3  = one stack
│     │           main <─ PR#4                   = another stack
│     │
│     ├── order_queue()
│     │     └── Sort stacks by earliest queued_at (FIFO)
│     │
│     ├── select_next()
│     │     └── Pick first stack
│     │
│     ├── No stacks? ──yes──> return "no_stacks"
│     │
│     no
│     │
├─ 4. CREATE BATCH (batch.create_batch)
│     │
│     │   ┌──────────────────────────────────────────────────────┐
│     │   │  a. git checkout -b mq/<batch_id>                   │
│     │   │                                                      │
│     │   │  b. For each PR in stack (bottom to top):            │
│     │   │       git fetch origin <head_ref>                    │
│     │   │       verify SHA matches (optimistic lock)           │
│     │   │       git merge --no-ff origin/<head_ref>            │
│     │   │                                                      │
│     │   │  c. git push origin mq/<batch_id>                   │
│     │   │                                                      │
│     │   │  d. Create ruleset locking PR branches               │
│     │   │     (MQ_ADMIN_TOKEN -> GitHub Rulesets API)          │
│     │   │     Pushes to PR branches now rejected: GH013        │
│     │   │                                                      │
│     │   │  e. Add 'locked' label to each PR                   │
│     │   └──────────────────────────────────────────────────────┘
│     │
│     ├── BatchError? ──yes──> Comment, remove 'queue', return "batch_error"
│     │
│     no
│     │
├─ 5. RUN CI (batch.run_ci)
│     │
│     │   ┌──────────────────────────────────────────────────────┐
│     │   │  a. Dispatch CI workflow on mq/<batch_id> branch     │
│     │   │     (workflow_dispatch -> ci.yml)                     │
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
├─ 6. COMPLETE BATCH (batch.complete_batch)
│     │
│     │   ┌──────────────────────────────────────────────────────┐
│     │   │  a. Verify optimistic locks (PR SHAs unchanged)      │
│     │   │                                                      │
│     │   │  b. Verify main hasn't diverged                      │
│     │   │                                                      │
│     │   │  c. Retarget all PRs to main                         │
│     │   │     (so GitHub sees "new commits" on each PR)        │
│     │   │                                                      │
│     │   │  d. Fast-forward main to mq/<batch_id> tip           │
│     │   │     (git.updateRef, force=false)                     │
│     │   │                                                      │
│     │   │  e. GitHub detects PR commits reachable from main    │
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
├─ 7. CHECK FOR MORE QUEUED STACKS
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

## Queue Ordering (FIFO)

```
EXAMPLE: Two stacks queued at different times

  Time ──────────────────────────────────────────────>

  T=0: User labels PR#4 with 'queue'    (stack B, position 1)
  T=1: User labels PR#1 with 'queue'    (stack A, position 2)
  T=2: User labels PR#2 with 'queue'    (stack A, same position as PR#1)
  T=3: User labels PR#5 with 'queue'    (stack B, same position as PR#4)

  Stack detection:
    Stack A: main <─ PR#1 <─ PR#2       queued_at = T=1 (min of T=1, T=2)
    Stack B: main <─ PR#4 <─ PR#5       queued_at = T=0 (min of T=0, T=3)

  FIFO order: Stack B first (T=0), then Stack A (T=1)

  Processing:
    1. Stack B: create mq/ branch, merge PR#4 + PR#5, run CI, merge
    2. Self-dispatch
    3. Stack A: create mq/ branch, merge PR#1 + PR#2, run CI, merge
```

## Branch State During Merge

```
BEFORE:
  main:      A───B───C
  feat-a:    A───B───C───D         (PR#1 targets main)
  feat-b:    A───B───C───D───E     (PR#2 targets feat-a)

DURING (mq/ branch created):
  main:      A───B───C
  mq/123:    A───B───C───M1────M2  (M1 = merge feat-a, M2 = merge feat-b)
  feat-a:    LOCKED (ruleset)
  feat-b:    LOCKED (ruleset)

AFTER (main fast-forwarded):
  main:      A───B───C───M1────M2
  feat-a:    DELETED
  feat-b:    DELETED
  mq/123:    DELETED
  PR#1:      MERGED (purple)
  PR#2:      MERGED (purple)
```

## Module Dependencies

```
  ┌───────────────────────────────────────────────────────┐
  │                    merge-queue.yml                     │
  │               (GitHub Actions workflow)                │
  └───────────────────────┬───────────────────────────────┘
                          │ python -m merge_queue
                          v
                    ┌───────────┐
                    │  cli.py   │  argparse routing
                    └─────┬─────┘
                          │
            ┌─────────────┼─────────────┐
            v             v             v
      ┌───────────┐ ┌──────────┐ ┌───────────┐
      │ queue.py  │ │ batch.py │ │ rules.py  │
      │           │ │          │ │           │
      │ Pure logic│ │ Lifecycle│ │ Invariant │
      │ No I/O   │ │ + git    │ │ checks    │
      └─────┬─────┘ └────┬─────┘ └─────┬─────┘
            │             │             │
            v             v             v
      ┌─────────────────────────────────────┐
      │           types.py                  │
      │  PullRequest, Stack, Batch,         │
      │  BatchStatus, RuleResult            │
      └─────────────────────────────────────┘
            │             │
            v             v
      ┌───────────────────────────────────┐
      │       github_client.py            │
      │  requests-based GitHub API        │
      │  GITHUB_TOKEN + MQ_ADMIN_TOKEN    │
      └───────────────────────────────────┘
```

## Test Coverage

```
  Module             Coverage   Tests   What's tested
  ──────────────────────────────────────────────────────────────────
  types.py           100%       -       Covered transitively
  queue.py            98%       28      Stack detection, FIFO, validation
  batch.py            92%       20      Create, complete, fail, abort, unlock
  rules.py           100%       12      All 5 invariant rules
  cli.py             100%       37      All commands, process loop, error paths
  ──────────────────────────────────────────────────────────────────
  TOTAL               98%       97      85% threshold enforced
```
