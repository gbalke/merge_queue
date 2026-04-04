# Project Instructions

Merge queue for stacked PRs. Python package invoked by GitHub Actions.

## Development Workflow

ALL changes go through revup + merge queue. Never push directly to main.

- Use the `/revup-commit` skill (`.claude/skills/revup-commit/SKILL.md`) for creating commits with topic trailers
- Each change = one focused revup topic
- Stack related changes with `Relative:` trailer
- Run checks before uploading:
  ```
  pytest tests/
  ruff check merge_queue/ tests/ && ruff format --check merge_queue/ tests/
  ```
- Upload with `revup upload --skip-confirm`
- Add `queue` label to PRs to enter the merge queue

## Using Agents

For non-trivial work, spawn worktree agents:

- Use `isolation: "worktree"` to give agents their own copy of the repo
- Each agent creates its own revup topic and uploads a PR
- Multiple agents can run in parallel on independent topics
- After agents complete, queue their PRs with the `queue` label
- Agents should run lint + format + tests before uploading

## Key Commands

```
pip install -e ".[dev]"          # install
pytest tests/                     # run tests (90%+ coverage enforced)
ruff check merge_queue/ tests/   # lint
ruff format merge_queue/ tests/  # format
revup upload --skip-confirm      # upload PRs
python -m merge_queue status     # check merge queue
```

## CI Requirements

PRs must pass lint, format, and tests before the merge queue will accept them.
Use the `re-test` label to retrigger CI.

## Architecture

See [ARCHITECTURE.md](ARCHITECTURE.md) for system diagrams and [README.md](README.md) for full documentation.
