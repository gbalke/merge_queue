# merge_queue — Internal Cleanup Plan

## Current Layout

```
merge_queue/
  cli.py              # 1725 lines — CLI parsing + all orchestration (too big)
  batch.py            # Batch creation, CI, completion
  store.py            # State persistence (mq/state branch)
  comments.py         # PR comment templates
  config.py           # merge-queue.yml parsing + ruleset management
  github_client.py    # GitHub API client (should be a provider)
  queue.py            # Stack detection
  rules.py            # Pre-merge validation rules
  state.py            # QueueState fetch helper
  status.py           # STATUS.md rendering
  types.py            # Dataclasses (PullRequest, Stack, Batch, etc.)
  providers/
    local.py           # Local git provider for testing
  metrics/
    __init__.py        # MetricsBackend protocol + factory
    noop.py            # No-op backend
    otlp.py            # OTLP JSON backend (Grafana Cloud)
    prometheus.py      # Prometheus push gateway backend
```

## Cleanup Tasks

### Done
- [ ] Items below

### Quick Wins
- [ ] **Move `github_client.py` → `providers/github.py`**: It implements the same protocol as `local.py`. Extract `GitHubClientProtocol` to `providers/__init__.py`.
- [ ] **Create `lib/formatting.py`**: Move `_fmt_duration` (duplicated in cli.py and comments.py) here. Both import from lib.
- [ ] **Create `lib/time.py`**: Move `_now_iso`, `_event_time_or_now`, `_fmt_duration` — shared time utilities.
- [ ] **Extract `_get_branch_state()` helper**: Replace 8+ occurrences of `state.setdefault("branches", {}).setdefault(target, empty_branch_state())`.
- [ ] **Deduplicate active batch re-queue**: `do_hotfix` and `do_break_glass` both abort active batch and re-queue its PRs — extract `_abort_and_requeue_active()`.

### Medium Refactors
- [ ] **Extract `_notify()` helper**: Consolidate "post comment + update deployment" pattern (6+ occurrences in cli.py).
- [ ] **Break up `do_enqueue` (287 lines)**: Extract guard clauses → `_validate_pr()`, stack detection → `_resolve_stack()`, CI gate → `_check_ci_gate()`, protected paths → `_check_protected_paths()`.
- [ ] **Break up `do_process` (463 lines)**: Extract `_handle_stale_batches()`, `_run_batch_ci()`, `_complete_or_retry()`.
- [ ] **Consolidate test pairs**: Merge test_store.py + test_store_extra.py, test_cli.py + test_cli_extra.py, test_status.py + test_status_extra.py.

### Larger Refactors (hold for quiet period)
- [ ] **Split cli.py**: `cli.py` (argument parsing only) + `orchestration.py` (do_* functions) + `validation.py` (auth/path checks).
- [ ] **Type the state machine**: Replace string-based `progress` field with enum. Validate transitions.
- [ ] **Centralize error handling**: Consistent try/except patterns for label removal, deployment updates, comment posting.
