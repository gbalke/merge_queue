# Project Instructions

Merge queue for stacked PRs. Python package invoked by GitHub Actions.

## Development Workflow

ALL changes go through revup + merge queue. **NEVER force push or push directly to main under ANY circumstances.** This includes hotfixes, urgent fixes, and "just this once" situations. No exceptions.

- Use the `/revup-commit` skill (`.claude/skills/revup-commit/SKILL.md`) for creating commits with topic trailers
- Each change = one focused revup topic
- Stack related changes with `Relative:` trailer
- Run checks before uploading: `ci/run` (runs lint, format, test in parallel)
  - Or individually: `ci/lint`, `ci/format`, `ci/test`
- Upload with `revup upload --skip-confirm`
- Add `queue` label to PRs to enter the merge queue
- **Agents must NEVER use `git push origin main` or bypass branch protection**

## Using Agents

For non-trivial work, spawn worktree agents:

- Use `isolation: "worktree"` to give agents their own copy of the repo
- Each agent creates its own revup topic and uploads a PR
- Multiple agents can run in parallel on independent topics
- After agents complete, queue their PRs with the `queue` label
- Agents should run `ci/run` before uploading

## Key Commands

```
pip install -e ".[dev]"          # install
ci/run                            # run all CI checks (lint + format + test) in parallel
ci/lint                           # syntax check + ruff lint
ci/format                         # ruff format --check
ci/test                           # pytest with coverage
revup upload --skip-confirm      # upload PRs
python -m merge_queue status     # check merge queue
```

## Bug Fixes: TDD Required

All bug fixes MUST follow Test-Driven Development:

1. Write a test that reproduces the bug (test must FAIL)
2. Run the test to confirm it fails
3. Implement the fix
4. Run the test to confirm it passes
5. Run the full test suite

This ensures every bug has a regression test and the fix is verified.

## CI Requirements

- PRs must pass lint, format, and tests before the merge queue accepts them
- Use `re-test` label to retrigger CI
- Use `break-glass` label to bypass CI gate (only when MQ itself is broken)

## Architecture

See [ARCHITECTURE.md](ARCHITECTURE.md) for system diagrams and [README.md](README.md) for full documentation.
