# Merge Queue for Stacked PRs

A lightweight, GitHub Actions-based merge queue that understands stacked/chained PRs.

## How It Works

1. Create stacked PRs (e.g., with [revup](https://github.com/Skydio/revup)) where PR B targets PR A's branch
2. Get reviews + CI passing on each PR
3. Add the `queue` label to PRs you want merged (must be contiguous from the bottom of the stack)
4. The merge queue:
   - Detects the full stack chain
   - **Locks PR branches** via GitHub rulesets (prevents pushes during queue)
   - Adds the `locked` label to all queued PRs
   - Creates a temporary `mq/<batch-id>` branch from `main`
   - Merges each PR's branch in order (`--no-ff`)
   - Dispatches CI on the batch branch and waits for completion
   - If CI passes: fast-forwards `main`, PRs auto-close as **"Merged"** (purple)
   - If CI fails: unlocks branches, removes `queue` and `locked` labels, cleans up
5. To **abort**: remove the `queue` label from any PR in the batch. This unlocks all branches, removes `locked` labels, and deletes the batch branch.

## Labels

| Label | Purpose |
|-------|---------|
| `queue` | Add to PRs to enter the merge queue. Remove to abort. |
| `locked` | Automatically added when branches are locked during queue processing. Automatically removed on completion or abort. |

## Setup

### 1. Repository Settings

- **Settings > General > Pull Requests**: Enable "Automatically delete head branches"
- **Settings > Actions > General**: Ensure workflows have read/write permissions
- Repository must be **public** (rulesets require GitHub Pro for private repos)

### 2. Create Labels

Create two labels in **Issues > Labels**:
- `queue` (green) — triggers the merge queue
- `locked` (red) — indicates branch is locked by merge queue

### 3. Create `MQ_ADMIN_TOKEN` Secret

Branch locking uses GitHub rulesets, which require elevated permissions that `GITHUB_TOKEN` cannot provide. Create a fine-grained Personal Access Token:

1. Go to https://github.com/settings/tokens?type=beta
2. Create a token scoped to your merge queue repository
3. Grant **Administration: Read and Write** permission
4. Add it as a repository secret:
   ```bash
   gh secret set MQ_ADMIN_TOKEN --repo <owner>/<repo>
   ```

Without this secret, the merge queue still works but branches won't be locked during processing (optimistic SHA verification is used as a fallback).

### 4. Workflows

| Workflow | Purpose |
|----------|---------|
| `ci.yml` | CI pipeline (replace with your real CI) |
| `merge-queue-enqueue.yml` | Main merge queue workflow — enqueue, lock, CI, merge, unlock |

## Usage

```bash
# Label individual PRs (must be contiguous from bottom of stack)
gh pr edit 1 --add-label queue
gh pr edit 2 --add-label queue
gh pr edit 3 --add-label queue

# To abort the merge queue, remove the label from any PR:
gh pr edit 1 --remove-label queue
```

Or add/remove labels via the GitHub UI.

## Stack Detection

The merge queue walks the PR dependency graph:

```
main <- PR #1 (feature-a) <- PR #2 (feature-b) <- PR #3 (feature-c)
```

If you label #1, #2, and #3 with `queue`, all three are batched together.
If you only label #1 and #2, only those two are merged.
If you label #2 and #3 but not #1, you get an error (non-contiguous).

## Branch Locking

When PRs enter the merge queue:

1. A GitHub **ruleset** is created that blocks `git push` to all PR branches in the batch
2. The `locked` label is added to each PR for visibility
3. Any attempt to push will be rejected with: `GH013: Repository rule violations found`

Branches are unlocked when:
- The batch **succeeds** (merged to main)
- The batch **fails** (CI failure, merge conflict, etc.)
- The `queue` label is **removed** (manual abort)

## Safety

- **Branch locking**: PR branches are locked via rulesets while in the queue — pushes are rejected
- **Optimistic locking**: SHA verification at merge time catches any edge cases
- **Fast-forward only**: `main` is only updated via fast-forward (`force: false`)
- **Single batch**: Only one batch runs at a time (concurrency group)
- **Atomic**: Either the entire batch merges or nothing does
- **Abort support**: Remove `queue` label to abort and unlock branches

## Adapting for Production

1. **Use a GitHub App** for elevated permissions (replace `github.token` with app token):
   ```yaml
   - uses: tibdex/github-app-token@v2
     with:
       app_id: ${{ secrets.APP_ID }}
       private_key: ${{ secrets.APP_PRIVATE_KEY }}
   ```

2. **Add approval checks** in the enqueue workflow (currently relaxed for testing)

3. **Update `ci.yml`** workflow name to match your real CI

4. **Add branch protection** on `main` requiring status checks

5. **For private repos**: Use branch protection API with admin PAT instead of rulesets
