# Auto-Retry on Diverge

When the target branch moves during CI, the batch is automatically retried.

## How It Works

During batch completion, the queue fast-forwards the target branch. If the
target has advanced since the batch was created, the GitHub API returns a 422
error. This is caught as a `BatchError` in
[`merge_queue/batch.py`](../merge_queue/batch.py), and the batch is
automatically re-queued with an incremented retry counter.

The flow:

1. Batch CI passes.
2. `complete_batch` attempts to fast-forward the target branch.
3. GitHub returns 422 (not a fast-forward).
4. `BatchError` is raised, caught by the retry handler.
5. A new batch branch is created from the current target tip.
6. PRs are re-merged and CI is re-dispatched.

## Retry Limit

Up to **3 retries** are allowed. If the target branch diverges more than 3
times, the batch fails and PRs are notified. The retry count is tracked in
the batch state.

## No Configuration Needed

Auto-retry is always enabled. There is no configuration to change the retry
limit.
