# Self-Hosted

The merge queue runs entirely as a GitHub Actions workflow. No external infrastructure is needed.

## Components

- A GitHub Actions workflow (`.github/workflows/merge-queue.yml`) that triggers
  on label events, `workflow_dispatch`, and schedules.
- The `merge_queue` Python package, installed from the repo.
- A GitHub PAT (`MQ_ADMIN_TOKEN`) with Administration:Write for rulesets.

## Setup

1. Copy [`examples/merge-queue.yml`](../examples/merge-queue.yml) to
   `.github/workflows/merge-queue.yml`. Set `MQ_CI_WORKFLOW` to your CI
   workflow filename.

2. Create labels: `queue`, `locked`, `re-test`, `hotfix`, `break-glass`.

3. Set the admin token:
   ```bash
   gh secret set MQ_ADMIN_TOKEN --repo <owner>/<repo>
   ```

4. Enable "Automatically delete head branches" in repo settings. Set Actions
   permissions to read/write.

5. (Optional) Add `merge-queue.yml` config in the repo root for protected
   paths, target branches, and metrics.

## How It Runs

The workflow is defined in
[`examples/merge-queue.yml`](../examples/merge-queue.yml). The Python CLI
entry point is [`merge_queue/cli.py`](../merge_queue/cli.py). State is stored
on the `mq/state` branch via the Git Trees API -- no database required.
