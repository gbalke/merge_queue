# Priority Merges

Three labels control merge priority: `queue`, `hotfix`, and `break-glass`.

## Label Hierarchy

| Label | Queue Position | CI Required | Active Batch | Who Can Use |
|-------|---------------|-------------|--------------|-------------|
| `queue` | Back (FIFO) | Yes | Waits | Anyone |
| `hotfix` | Front | Yes | Aborts, re-queues behind | Admins + `break_glass_users` |
| `break-glass` | Immediate | No | Aborts, merges now | Admins + `break_glass_users` |

## How It Works

- **`queue`**: standard FIFO enqueue. CI must pass before and after batching.
- **`hotfix`**: jumps to the front of the queue. If a batch is active, it is
  aborted and its PRs are re-queued behind the hotfix. CI still runs on the
  hotfix batch.
- **`break-glass`**: skips CI entirely and merges immediately. The active batch
  (if any) is aborted and re-queued. This is a last resort for emergencies.

Authorization is checked in [`merge_queue/cli.py`](../merge_queue/cli.py)
(`hotfix` and `break_glass` commands). The `break_glass_users` list is
configured in `merge-queue.yml`:

```yaml
break_glass_users: [gbalke, deploy-bot]
```

Repo admins always have access regardless of this list.
