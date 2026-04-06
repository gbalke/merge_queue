# Multi-Branch Queues

Each target branch gets its own independent queue, batch processing, and STATUS.md dashboard.

## How It Works

The queue maintains separate FIFO queues per target branch. When a PR targeting
`release/1.0` is enqueued, it enters a different queue than a PR targeting
`main`. Batches run independently -- a failure on one branch does not block
another.

Branch routing is handled in [`merge_queue/queue.py`](../merge_queue/queue.py).
State is stored per-branch in [`merge_queue/state.py`](../merge_queue/state.py).

## Configuration

Set `target_branches` in `merge-queue.yml` at the repo root:

```yaml
target_branches: [main, release/1.0, release/2.0]
```

When omitted, the queue defaults to `main` only.

## Behavior

- Each branch gets its own queue, batch lifecycle, and `STATUS.md`.
- Branch protection rulesets are created for each target branch.
- PRs are routed to the correct queue based on the stack's root `base_ref`.
