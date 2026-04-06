# CI Gating

PRs must pass CI before entering the merge queue.

## How It Works

When a PR is enqueued, `get_pr_ci_status` in
[`merge_queue/github_client.py`](../merge_queue/github_client.py) checks for a
passing "Final Results" check run on the PR's head commit. If CI has not passed,
the PR is rejected with a comment explaining why.

During batch processing, CI is dispatched on the batch branch and polled until
completion. The batch succeeds only if the CI workflow passes.

## Re-testing

Add the `re-test` label to a PR to retrigger CI on its head branch. The label
is automatically removed after the dispatch. Handled by the `retest` command in
[`merge_queue/cli.py`](../merge_queue/cli.py).

## Bypassing CI

Add the `break-glass` label to skip CI entirely and merge immediately. This
requires admin permissions or membership in `break_glass_users`. See
[priority-merges.md](priority-merges.md).

## Configuration

Set the CI workflow filename via the `MQ_CI_WORKFLOW` environment variable
(defaults to `ci.yml`).
