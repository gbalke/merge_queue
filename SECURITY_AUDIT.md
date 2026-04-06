# Security Audit — merge-queue

**Date:** 2026-04-05 | **Auditor:** Claude (claude-opus-4-6) | **Scope:** `merge_queue/`, `.github/workflows/`, `pyproject.toml`

All prior Critical/High findings resolved. Five open findings below.

## Findings

| ID | Severity | Description | Location | Recommended Fix |
|---|---|---|---|---|
| MEDIUM-1 | Medium | Exception logging may echo bearer token fragments via `requests` error chains | `cli.py:79,124,207,700,941,1045,1476` `batch.py:108,162` `config.py:377` `github_client.py:610` | Catch `HTTPError` separately; log only status code/reason. Add `_safe_error()` wrapper to redact `Bearer` tokens. |
| MEDIUM-2 | Medium | Raw exception strings rendered in PR comments without sanitization — Markdown injection via crafted branch names | `cli.py:960-963,1477` `comments.py:255,276-277` | Apply `_sanitize()` to all error strings before Markdown rendering in `batch_error()` and `failed()`. |
| MEDIUM-3 | Medium | CI workflow `delay` input interpolated directly in shell context | `.github/workflows/ci.yml:78` | Pass via `env:` variable instead of `${{ }}` interpolation in `run:`. |
| LOW-1 | Low | `dispatch_ci_on_ref` dispatches workflow on PR-supplied branch ref | `cli.py:1207` `github_client.py:689` | No action needed now; review if CI workflow changes. |
| LOW-2 | Low | Concurrency group gap allows queue DoS via rapid label events | `.github/workflows/merge-queue.yml:13-15` | No action needed; mitigated by stale-batch recovery and state guards. |

## Security Posture

- Workflow installs from `main` only, not PR branches.
- MQ command passed via `$MQ_CMD` env var (safe against injection).
- Admin token lazily used only for admin ops (rulesets, `update_ref`).
- `mq/*` branches protected by auto-created ruleset.
- `break-glass`/`hotfix` gated by `_is_break_glass_authorized()`.
- All `subprocess.run()` calls use list-form args (no `shell=True`).
- PR titles/branch names sanitized via `_sanitize()` before Markdown rendering; error strings are the remaining gap (MEDIUM-2).

*Automated analysis — does not substitute for a penetration test.*
