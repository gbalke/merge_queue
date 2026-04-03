# Merge Queue for Stacked PRs

A lightweight, GitHub Actions-based merge queue that understands stacked/chained PRs.

## How It Works

1. Create stacked PRs (e.g., with [revup](https://github.com/Skydio/revup)) where PR B targets PR A's branch
2. Get reviews + CI passing on each PR
3. Add the `queue` label to PRs you want merged (must be contiguous from the bottom of the stack)
4. The merge queue:
   - Detects the full stack chain
   - Creates a temporary `mq/<batch-id>` branch from `main`
   - Merges each PR's branch in order (`--no-ff`)
   - CI runs on the batch branch
   - If CI passes: fast-forwards `main`, PRs auto-close as **"Merged"** (purple)
   - If CI fails: notifies, removes labels, cleans up

## Setup

### 1. Repository Settings

- **Settings > General > Pull Requests**: Enable "Automatically delete head branches"
- **Settings > Actions > General**: Ensure workflows have read/write permissions

### 2. Create the `queue` Label

Go to **Issues > Labels** and create a label named `queue`.

### 3. Workflows

The repo includes three workflows:

| Workflow | Purpose |
|----------|---------|
| `ci.yml` | CI pipeline (replace with your real CI) |
| `merge-queue-enqueue.yml` | Triggered when `queue` label is added to a PR |
| `merge-queue-merge.yml` | Triggered when CI completes on `mq/*` branches |

## Usage

```
# Label individual PRs (must be contiguous from bottom of stack)
gh pr edit 1 --add-label queue
gh pr edit 2 --add-label queue
gh pr edit 3 --add-label queue
```

Or add the label via the GitHub UI.

## Stack Detection

The merge queue walks the PR dependency graph:

```
main <- PR #1 (feature-a) <- PR #2 (feature-b) <- PR #3 (feature-c)
```

If you label #1, #2, and #3 with `queue`, all three are batched together.
If you only label #1 and #2, only those two are merged.
If you label #2 and #3 but not #1, you get an error (non-contiguous).

## Safety

- **Optimistic locking**: If a PR branch is updated while in the queue, the batch is aborted
- **Fast-forward only**: `main` is only updated via fast-forward (`force: false`), so it's safe if main moves
- **Single batch**: Only one batch runs at a time (concurrency group)
- **Atomic**: Either the entire batch merges or nothing does

## Adapting for Production

For a real repo, you'll want to:

1. **Use a GitHub App** for elevated permissions (replace `github.token` with app token):
   ```yaml
   - uses: tibdex/github-app-token@v2
     with:
       app_id: ${{ secrets.APP_ID }}
       private_key: ${{ secrets.APP_PRIVATE_KEY }}
   ```

2. **Add approval checks** in the enqueue workflow (currently relaxed for testing)

3. **Update `ci.yml`** to match your real CI workflow name in `merge-queue-merge.yml`'s `workflow_run` trigger

4. **Add branch protection** on `main` requiring status checks
