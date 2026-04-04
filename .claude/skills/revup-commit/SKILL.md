---
name: revup-commit
description: Create git commits with revup topic tags for stacked PRs. Use when the user wants to commit changes as part of a revup workflow, create stacked PRs, or assign commits to topics. Triggers include "commit with topic", "revup commit", "stack this", "add to topic".
argument-hint: [topic-name] [--relative parent-topic]
allowed-tools: Bash Read Grep Glob
---

Create a git commit with revup topic trailers for stacked PR workflows.

## Arguments

`$ARGUMENTS` may contain:
- A topic name (required) — the revup topic this commit belongs to
- `--relative <parent-topic>` or `-r <parent-topic>` — make this topic's PR target the parent topic's branch (stacked PR)
- `--reviewers <user1,user2>` or `-R <user1,user2>` — assign reviewers
- `--labels <label1,label2>` — add GitHub labels
- `--draft` — mark the PR as draft
- `--message <msg>` or `-m <msg>` — commit message (if not provided, generate from staged changes)

If no arguments are provided, ask the user for the topic name.

## Workflow

1. **Check for staged changes**: Run `git diff --cached --stat` to see what's staged. If nothing is staged, show `git status` and ask the user what to stage.

2. **Determine commit message**: If `-m` was provided, use it. Otherwise, read the staged diff and generate a concise, descriptive commit message (imperative mood, under 72 chars).

3. **Build trailers**: Construct the trailer block as a single contiguous block with NO blank lines between trailers:
   - Always include: `Topic: <topic-name>`
   - If `--relative`: `Relative: <parent-topic>`
   - If `--reviewers`: `Reviewers: <user1>, <user2>`
   - If `--labels`: `Labels: <label1>, <label2>`
   - If `--draft`: `Labels: draft`

4. **Create the commit**: Run `git commit` with the subject as the first `-m` and ALL trailers combined in a single second `-m` (newline-separated, no blank lines between them):
   ```
   git commit -m "<subject line>" -m "Topic: <topic>
   Relative: <parent>
   Reviewers: <users>"
   ```
   This ensures trailers appear as a contiguous block in the commit body with no blank lines between them. Revup requires this format.

5. **Show result**: Display the commit log entry so the user can verify.

## Revup Trailer Reference

All trailers go in a single `-m` argument as a contiguous block (no blank lines between them):

| Trailer | Purpose | Example |
|---------|---------|---------|
| `Topic: <name>` | Assigns commit to a named PR | `Topic: auth-refactor` |
| `Relative: <parent>` | PR targets parent topic's branch | `Relative: auth-refactor` |
| `Reviewers: <users>` | Comma-separated GitHub usernames | `Reviewers: alice, bob` |
| `Assignees: <users>` | Assign users to the PR | `Assignees: carol` |
| `Labels: <labels>` | GitHub labels (use `draft` for draft PRs) | `Labels: draft, bug` |
| `Branches: <branches>` | Target specific base branches | `Branches: main, release-1.0` |

## Example Usage

User: `/revup-commit auth-login --relative auth-base --reviewers alice`

Result:
```
git commit -m "Add login endpoint with session management" -m "Topic: auth-login
Relative: auth-base
Reviewers: alice"
```

## Important Notes

- Multiple commits can share the same topic — they combine into one PR
- Commits for a child topic (Relative) must come AFTER the parent topic's commits in git history
- After committing, remind the user to run `revup upload` to create/update PRs
- If the user wants to modify a previous commit in the stack, suggest `revup amend <topic-name>`

## Best Practices

- **Keep changes small and focused.** Each topic should represent one logical change.
- **Use Relative stacking** when changes build on each other, but keep each topic independently reviewable.
- **Separate concerns:** lint fixes, feature code, tests, and config changes should be different topics.
- **Run tests before uploading:** Always verify `pytest tests/` passes before `revup upload`.
- **Run lint before uploading:** Always verify `ruff check merge_queue/ tests/` passes.
