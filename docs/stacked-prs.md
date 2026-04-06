# Stacked PR Support

The merge queue detects and merges entire PR stacks atomically.

## How It Works

When a PR is enqueued, the queue follows the `base_ref` chain to discover the
full stack. If PR C targets PR B, and PR B targets PR A, and PR A targets
`main`, enqueuing any of them resolves the full chain `[A, B, C]`. The entire
stack is merged in order via `--no-ff` commits onto a single batch branch, and
the target branch is fast-forwarded only when all merges and CI succeed.

Stack detection is implemented in [`merge_queue/queue.py`](../merge_queue/queue.py)
(`_resolve_stack`). Batch merging is in [`merge_queue/batch.py`](../merge_queue/batch.py).

## How to Use

Create stacked PRs using [revup](https://github.com/Skydio/revup) with the
`Relative:` trailer to set each PR's base branch. Then add the `queue` label
to the PRs you want merged (contiguous from the stack bottom). The merge queue
detects the stack automatically -- no extra configuration needed.

## Example

```
PR A  (base: main)       <- queue label
PR B  (base: PR A)       <- queue label
PR C  (base: PR B)       <- queue label
```

All three are merged atomically in A, B, C order.
