# Project Instructions

Merge queue for stacked PRs. Python package invoked by GitHub Actions.

## Development Workflow

ALL changes go through revup + merge queue. **NEVER force push or push directly to main under ANY circumstances.** This includes hotfixes, urgent fixes, break-glass emergencies, and "just this once" situations. No exceptions. Hotfixes and break-glass PRs both go through the merge queue — they just get different priority and CI treatment.

- Use the `/revup-commit` skill (`.claude/skills/revup-commit/SKILL.md`) for creating commits with topic trailers
- Each change = one focused revup topic
- Stack related changes with `Relative:` trailer
- Run checks before uploading: `ci/run` (runs lint, format, test in parallel)
  - Or individually: `ci/lint`, `ci/format`, `ci/test`
- Upload with `revup upload --skip-confirm`
- Add `queue` label to PRs to enter the merge queue
- **Agents must NEVER use `git push origin main` or bypass branch protection**

### Merge Priority Labels

Three ways to land a PR, from normal to emergency:

| Label | Queue position | CI | When to use |
|-------|---------------|-----|-------------|
| `queue` | Back of queue | Runs normally | Default — all normal changes |
| `hotfix` | Front of queue (aborts active batch, re-queues its PRs behind) | Runs normally | Urgent fix, but CI is functional |
| `break-glass` | Immediate (aborts active batch) | Skipped entirely | Last resort — CI itself is broken |

Only authorized users (admins or `break_glass_users` in merge-queue.yml config) can use `hotfix` or `break-glass`. All three labels go through the merge queue — nothing is ever pushed directly to main.

## Rebasing PRs onto Main

When main has moved ahead and a PR has conflicts or stale CI:

1. Fetch latest: `git fetch origin && git checkout main && git reset --hard origin/main`
2. Find the topic commit: `git log --all --oneline --grep="Topic: <topic-name>"`
3. Cherry-pick onto main: `git cherry-pick <sha>`
4. If conflicts, resolve manually — read the diff, understand the intent, apply to current code
5. Run lint + format + tests to verify
6. Upload: `revup upload --skip-confirm`
7. Re-add `queue` label to the PR

If cherry-pick has too many conflicts, re-implement manually: read `gh pr diff <N>`, apply the intent to current code as a new commit with the same `Topic:` trailer.

## Using Agents

For non-trivial work, spawn worktree agents:

- Use `isolation: "worktree"` to give agents their own copy of the repo
- Each agent creates its own revup topic and uploads a PR
- Multiple agents can run in parallel on independent topics
- After agents complete, queue their PRs with the `queue` label
- Agents should run `ci/run` before uploading

## Key Commands

```
pip install -e ".[dev]"          # install (requires ruff >=0.15.9)
ci/run                            # run all CI checks (lint + format + test) in parallel
ci/lint                           # syntax check + ruff lint
ci/format                         # ruff format --check
ci/test                           # pytest with coverage
revup upload --skip-confirm      # upload PRs
python -m merge_queue status     # check merge queue
```

**Note on ruff:** CI uses ruff 0.15.9+. Your local version must match. Use `python -m ruff version` to check the venv version — bare `ruff` may resolve to an older system/bazel version.

## Bug Fixes: TDD Required

All bug fixes MUST follow Test-Driven Development:

1. Write a test that reproduces the bug (test must FAIL)
2. Run the test to confirm it fails
3. Implement the fix
4. Run the test to confirm it passes
5. Run the full test suite

This ensures every bug has a regression test and the fix is verified.

## Architecture

See [ARCHITECTURE.md](ARCHITECTURE.md) for system diagrams and [README.md](README.md) for full documentation.
