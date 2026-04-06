# Protected Paths

File paths can be protected so that changes require approval from designated approvers.

## How It Works

When a PR modifies files matching a protected path pattern, the queue checks
that at least one approver for that path has approved the PR. If not, the PR
is rejected with a comment listing the required approvers.

Path matching and approval checking are implemented in
[`merge_queue/config.py`](../merge_queue/config.py) (path parsing) and
[`merge_queue/queue.py`](../merge_queue/queue.py) (approval validation).

## Configuration

Add `protected_paths` to `merge-queue.yml`:

```yaml
protected_paths:
  # Simple path -- any repo admin can approve
  - merge-queue.yml
  - .github/workflows/

  # Path with explicit approvers
  - path: merge_queue/
    approvers: [gbalke, security-team-lead]
```

## Behavior

- Patterns are prefix-matched against changed file paths.
- When `approvers` is specified, only those users (or repo admins) can approve.
- When `approvers` is omitted, any repo admin can approve.
- A single approving review from an authorized user satisfies the requirement.
