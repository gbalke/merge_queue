# merge_queue — Internal Cleanup Plan

## Current Layout

```
merge_queue/
  cli.py              # 1725 lines — CLI parsing + all orchestration (too big)
  batch.py            # Batch creation, CI, completion
  store.py            # State persistence (mq/state branch)
  comments.py         # PR comment templates
  config.py           # merge-queue.yml parsing + ruleset management
  queue.py            # Stack detection
  rules.py            # Pre-merge validation rules
  state.py            # QueueState fetch helper
  status.py           # STATUS.md rendering
  types.py            # Dataclasses (PullRequest, Stack, Batch, etc.)
  providers/
    __init__.py        # GitHubClientProtocol + RateLimitInfo
    github.py          # GitHub API client
    local.py           # Local git provider for testing
  lib/
    __init__.py        # Package init
    formatting.py      # _fmt_duration and shared formatting
    time.py            # _now_iso, _event_time_or_now — shared time utilities
    state.py           # get_branch_state() helper
  metrics/
    __init__.py        # MetricsBackend protocol + factory
    noop.py            # No-op backend
    otlp.py            # OTLP JSON backend (Grafana Cloud)
    prometheus.py      # Prometheus push gateway backend
```

## Cleanup Tasks

### Done
- [x] **Move `github_client.py` → `providers/github.py`**: Extracted `GitHubClientProtocol` to `providers/__init__.py`.
- [x] **Create `lib/formatting.py`**: Moved `_fmt_duration` (was duplicated in cli.py and comments.py).
- [x] **Create `lib/time.py`**: Moved `_now_iso`, `_event_time_or_now` — shared time utilities.
- [x] **Extract `get_branch_state()` helper**: Replaced inline `setdefault` chains with `lib/state.py`.

### Quick Wins
- [x] **Deduplicate active batch re-queue**: Extracted `_abort_and_requeue_active()` used by both `do_hotfix` and `do_break_glass`.

### Medium Refactors
- [ ] **Extract `_notify()` helper**: Consolidate "post comment + update deployment" pattern (6+ occurrences in cli.py).
- [ ] **Break up `do_enqueue` (287 lines)**: Extract guard clauses → `_validate_pr()`, stack detection → `_resolve_stack()`, CI gate → `_check_ci_gate()`, protected paths → `_check_protected_paths()`.
- [ ] **Break up `do_process` (463 lines)**: Extract `_handle_stale_batches()`, `_run_batch_ci()`, `_complete_or_retry()`.
- [ ] **Consolidate test pairs**: Merge test_store.py + test_store_extra.py, test_cli.py + test_cli_extra.py, test_status.py + test_status_extra.py.

### Larger Refactors (hold for quiet period)
- [ ] **Split cli.py**: `cli.py` (argument parsing only) + `orchestration.py` (do_* functions) + `validation.py` (auth/path checks).
- [ ] **Type the state machine**: Replace string-based `progress` field with enum. Validate transitions.
- [ ] **Centralize error handling**: Consistent try/except patterns for label removal, deployment updates, comment posting.

## Metrics Improvements

### Architecture (done)
All metrics flow through `MetricsCollector` (`metrics/__init__.py`):
- Typed `record_*()` methods accumulate metrics throughout a run
- `flush()` pushes all to the configured backend once at end of run
- Labels include repo, target_branch, trigger type, batch_id, pr_numbers

### Batch Timing (per completion) — done
- [x] `mq_batch_queue_wait_seconds` — enqueue to batch start
- [x] `mq_batch_lock_seconds` — branch locking + merge time
- [x] `mq_batch_ci_seconds` — CI phase
- [x] `mq_batch_merge_seconds` — CI pass to fast-forward complete
- [x] `mq_batch_total_seconds` — end-to-end

### Queue Health (per process run) — done
- [x] `mq_queue_depth` — per branch
- [x] `mq_queue_oldest_seconds` — age of oldest entry
- [x] `mq_api_calls_total` — API calls used in this run
- [x] `mq_api_remaining` — remaining API quota

### Failure Tracking — done
- [x] `mq_batch_failures_total` — with `reason` label (ci_failed, merge_conflict, diverged, error)

### Labels/Attributes (on all metrics) — done
- [x] `target_branch`, `batch_id`, `pr_numbers`, `repo`, `status`, `trigger`
