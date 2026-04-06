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

There's nothing special required to create stacked PRs. The merge queue works
with any chain of PRs where each PR's base branch is another PR's head branch.
You can create these with any tool or workflow:

- **[revup](https://github.com/Skydio/revup)**: Use the `Relative:` trailer
- **Manual**: Create branches `feature-a` → `feature-b` → `feature-c`, open PRs targeting each previous branch
- **Any stacking tool**: [ghstack](https://github.com/ezyang/ghstack), [spr](https://github.com/ejoffe/spr), etc.

As long as the PRs form a chain that resolves to a configured target branch
(e.g. `main`), the merge queue detects and merges them atomically. Add the
`queue` label to the PRs you want merged (contiguous from the stack bottom).

## Example

```
PR A  (base: main)       <- queue label
PR B  (base: PR A)       <- queue label
PR C  (base: PR B)       <- queue label
```

All three are merged atomically in A, B, C order.
