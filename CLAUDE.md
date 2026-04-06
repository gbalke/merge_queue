# Project Instructions

Merge queue for stacked PRs. Python package invoked by GitHub Actions.

## Workflow Rules

- **NEVER push directly to main.** All changes go through revup + merge queue. No exceptions.
- Use `/revup-commit` skill for commits with topic trailers. One topic per change.
- Stack related changes with `Relative:` trailer.
- Run `ci/run` before uploading. Upload with `revup upload --skip-confirm`.
- Add `queue` label to PRs to enter the merge queue.
- Bug fixes require TDD: write failing test first, then fix, then verify.
- For non-trivial work, spawn worktree agents (`isolation: "worktree"`).

## Merge Priority Labels

| Label | Behavior |
|---|---|
| `queue` | Back of queue, normal CI |
| `hotfix` | Front of queue (aborts active batch), normal CI |
| `break-glass` | Immediate (aborts batch), skips CI. Last resort. |

`hotfix`/`break-glass` require admin or `break_glass_users` config membership.

## Rebasing onto Main

1. `git fetch origin && git checkout main && git reset --hard origin/main`
2. `git log --all --oneline --grep="Topic: <topic>"` to find the commit
3. `git cherry-pick <sha>` — resolve conflicts if needed
4. Run `ci/run`, then `revup upload --skip-confirm`, re-add `queue` label

If cherry-pick fails badly, re-implement from `gh pr diff <N>` with the same `Topic:` trailer.

## Key Commands

| Command | Purpose |
|---|---|
| `pip install -e ".[dev]"` | Install (requires ruff >= 0.15.9) |
| `ci/run` | All checks (lint + format + test) in parallel |
| `ci/lint` | Syntax check + ruff lint |
| `ci/format` | ruff format --check |
| `ci/test` | pytest with coverage |
| `revup upload --skip-confirm` | Upload PRs |

**Note:** CI uses ruff 0.15.9+. Check venv version with `python -m ruff version` — bare `ruff` may resolve to an older system version.

## Architecture

See [ARCHITECTURE.md](ARCHITECTURE.md) and [README.md](README.md).
