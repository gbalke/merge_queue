"""Batch lifecycle — lock, merge, CI, complete/fail.

Orchestrates GitHubClient calls for a single merge queue batch.
"""

from __future__ import annotations

import concurrent.futures
import logging
import subprocess
import time
from typing import Callable

from merge_queue.github_client import GitHubClientProtocol
from merge_queue.types import Batch, BatchStatus, Stack

log = logging.getLogger(__name__)

GitRunner = Callable[..., str]

MAX_LOCK_RETRIES = 3
LOCK_RETRY_DELAY = 2  # seconds
MAX_UNLOCK_RETRIES = 3
UNLOCK_RETRY_DELAY = 2


class BatchError(Exception):
    pass


class LockError(BatchError):
    """Failed to lock branches after retries."""

    pass


class UnlockError(BatchError):
    """Failed to unlock branches after retries."""

    pass


def run_git(*args: str) -> str:
    """Run a git command and return stdout. Raises BatchError with stderr on failure."""
    result = subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        stderr = result.stderr.strip()
        cmd = " ".join(args)
        raise BatchError(
            f"git {cmd} failed: {stderr or 'exit code ' + str(result.returncode)}"
        )
    return result.stdout


def _lock_branches(
    client: GitHubClientProtocol,
    name: str,
    branch_patterns: list[str],
    *,
    max_retries: int = MAX_LOCK_RETRIES,
    retry_delay: float = LOCK_RETRY_DELAY,
) -> int:
    """Create a ruleset and verify it's active. Retries on failure.

    Returns the ruleset ID.
    Raises LockError if all retries exhausted.
    """
    last_error: Exception | None = None

    for attempt in range(1, max_retries + 1):
        try:
            ruleset_id = client.create_ruleset(name, branch_patterns)
            log.info("Created lock ruleset %d (attempt %d)", ruleset_id, attempt)

            # Verify the ruleset is active
            ruleset = client.get_ruleset(ruleset_id)
            if ruleset.get("enforcement") != "active":
                raise LockError(
                    f"Ruleset {ruleset_id} created but enforcement is "
                    f"'{ruleset.get('enforcement')}', expected 'active'"
                )

            # Verify it covers the right branches
            conditions = ruleset.get("conditions", {}).get("ref_name", {})
            covered = set(conditions.get("include", []))
            expected = set(branch_patterns)
            if not expected.issubset(covered):
                missing = expected - covered
                raise LockError(
                    f"Ruleset {ruleset_id} missing branch patterns: {missing}"
                )

            log.info(
                "Verified lock ruleset %d is active and covers all branches", ruleset_id
            )
            return ruleset_id

        except LockError:
            raise  # Verification failures are not retryable
        except Exception as e:
            last_error = e
            log.warning("Lock attempt %d/%d failed: %s", attempt, max_retries, e)
            if attempt < max_retries:
                time.sleep(retry_delay)

    raise LockError(
        f"Failed to lock branches after {max_retries} attempts: {last_error}"
    )


def _unlock_ruleset(
    client: GitHubClientProtocol,
    ruleset_id: int | None,
    *,
    max_retries: int = MAX_UNLOCK_RETRIES,
    retry_delay: float = UNLOCK_RETRY_DELAY,
) -> None:
    """Delete a ruleset and verify it's gone. Retries on failure.

    Raises UnlockError if all retries exhausted.
    """
    if ruleset_id is None:
        return

    last_error: Exception | None = None

    for attempt in range(1, max_retries + 1):
        try:
            client.delete_ruleset(ruleset_id)
            log.info("Deleted lock ruleset %d (attempt %d)", ruleset_id, attempt)

            # Verify the ruleset is gone
            try:
                client.get_ruleset(ruleset_id)
                # If we get here, the ruleset still exists
                raise UnlockError(f"Ruleset {ruleset_id} still exists after deletion")
            except Exception as verify_err:
                # 404 means it's gone — that's what we want
                err_str = str(verify_err)
                if "404" in err_str or "Not Found" in err_str:
                    log.info("Verified ruleset %d is deleted", ruleset_id)
                    return
                # If it's our own UnlockError, re-raise
                if isinstance(verify_err, UnlockError):
                    raise
                # Other errors during verification — ruleset is probably gone
                log.info(
                    "Ruleset %d likely deleted (verify got: %s)", ruleset_id, verify_err
                )
                return

        except UnlockError:
            raise
        except Exception as e:
            last_error = e
            log.warning("Unlock attempt %d/%d failed: %s", attempt, max_retries, e)
            if attempt < max_retries:
                time.sleep(retry_delay)

    raise UnlockError(
        f"Failed to unlock ruleset {ruleset_id} after {max_retries} attempts: {last_error}"
    )


def _unlock(client: GitHubClientProtocol, batch: Batch) -> None:
    """Delete the lock ruleset for a batch."""
    _unlock_ruleset(client, batch.ruleset_id)


def create_batch(
    client: GitHubClientProtocol,
    stack: Stack,
    *,
    target_branch: str = "",
    git: GitRunner = run_git,
) -> Batch:
    """Create an mq/ branch, merge PR branches, lock branches, add locked label."""
    batch_id = str(int(time.time()))
    branch = f"mq/{target_branch}/{batch_id}" if target_branch else f"mq/{batch_id}"
    default_branch = client.get_default_branch()
    client.get_branch_sha(default_branch)  # validate branch exists

    # --- Lock branches FIRST to prevent pushes during merge ---
    branch_patterns = [f"refs/heads/{pr.head_ref}" for pr in stack.prs]
    ruleset_id = None
    try:
        ruleset_id = _lock_branches(client, f"mq-lock-{batch_id}", branch_patterns)
    except LockError as e:
        raise BatchError(f"Could not lock branches: {e}") from e

    for pr in stack.prs:
        client.add_label(pr.number, "locked")

    # --- Create mq branch and merge PRs via git CLI ---
    try:
        _git_create_and_merge(branch, stack, git=git)
    except Exception:
        # Merge failed — unlock before propagating
        try:
            _unlock_ruleset(client, ruleset_id)
        except UnlockError as ue:
            log.error("Failed to unlock after merge failure: %s", ue)
        for pr in stack.prs:
            client.remove_label(pr.number, "locked")
        raise

    return Batch(
        batch_id=batch_id,
        branch=branch,
        stack=stack,
        status=BatchStatus.RUNNING,
        ruleset_id=ruleset_id,
    )


def _git_create_and_merge(
    branch: str,
    stack: Stack,
    *,
    git: GitRunner = run_git,
) -> None:
    """Create the mq branch and merge PR branches using git CLI."""
    git("checkout", "-b", branch)

    for pr in stack.prs:
        log.info("Merging PR #%d (%s @ %s)...", pr.number, pr.head_ref, pr.head_sha)
        git("fetch", "origin", pr.head_ref)

        actual_sha = git("rev-parse", f"origin/{pr.head_ref}").strip()
        if actual_sha != pr.head_sha:
            raise BatchError(
                f"PR #{pr.number} head changed: expected {pr.head_sha}, got {actual_sha}"
            )

        git(
            "merge",
            "--no-ff",
            f"origin/{pr.head_ref}",
            "-m",
            f"Merge PR #{pr.number} (head:{pr.head_sha} ref:{pr.head_ref})",
        )

    git("push", "origin", f"HEAD:refs/heads/{branch}")
    log.info("Pushed batch branch %s", branch)


class CIResult:
    """Result of a CI run."""

    def __init__(self, passed: bool, run_url: str = ""):
        self.passed = passed
        self.run_url = run_url


def run_ci(
    client: GitHubClientProtocol, batch: Batch, timeout: int = 30 * 60
) -> CIResult:
    """Dispatch CI and poll for result. Returns CIResult with pass/fail and run URL."""
    client.dispatch_ci(batch.branch)
    passed, run_url = client.poll_ci_with_url(batch.branch, timeout)
    return CIResult(passed, run_url)


def complete_batch(
    client: GitHubClientProtocol,
    batch: Batch,
    target_branch: str | None = None,
) -> None:
    """Merge completed batch: retarget PRs, fast-forward target branch, clean up.

    Since branches are locked via ruleset, we skip SHA re-verification
    (nobody can push). After fast-forward, labels on merged PRs are inert
    so we skip label removal. Cleanup (unlock, delete, comment) runs in
    parallel to minimize API round-trips.

    Args:
        client: GitHub API client.
        batch: The batch to complete.
        target_branch: The branch to fast-forward. Defaults to the repo's
            default branch for backward compatibility.
    """
    if target_branch is None:
        target_branch = client.get_default_branch()
    batch_sha = client.get_branch_sha(batch.branch)

    # Verify target branch hasn't diverged
    status = client.compare_commits(target_branch, batch_sha)
    if status != "ahead":
        raise BatchError(
            f"{target_branch} has diverged — another commit landed while CI was running. "
            f"The batch branch is no longer a fast-forward of {target_branch}. "
            f"Re-add the `queue` label to retry."
        )

    # Retarget PRs to target_branch BEFORE fast-forward
    for pr in batch.stack.prs:
        try:
            client.update_pr_base(pr.number, target_branch)
        except Exception as e:
            log.warning("Could not retarget PR #%d: %s", pr.number, e)

    # Fast-forward target branch
    client.update_ref(target_branch, batch_sha)
    log.info("Fast-forwarded %s to %s", target_branch, batch_sha)

    # Set commit status on new HEAD so CI badge reflects the merge
    # (GITHUB_TOKEN pushes don't trigger workflows, so CI won't re-run)
    try:
        client.create_commit_status(batch_sha, "success", "Merged via merge queue")
    except Exception as e:
        log.warning("Could not set commit status: %s", e)

    time.sleep(5)

    # --- Parallel cleanup: unlock + delete branches + post comments ---
    # All independent — no ordering constraints after fast-forward.
    # Labels on merged PRs are inert, so we skip remove_label entirely.
    _parallel_cleanup(client, batch, target_branch)

    batch.status = BatchStatus.PASSED


def _parallel_cleanup(
    client: GitHubClientProtocol,
    batch: Batch,
    default_branch: str,
) -> None:
    """Run post-merge cleanup tasks in parallel."""

    def unlock():
        try:
            _unlock(client, batch)
        except UnlockError as e:
            log.error("Failed to unlock: %s", e)

    def delete_branches():
        client.delete_branch(batch.branch)
        for pr in batch.stack.prs:
            client.delete_branch(pr.head_ref)

    def post_comments():
        from merge_queue.comments import merged

        owner = getattr(client, "owner", "")
        repo = getattr(client, "repo", "")
        for pr in batch.stack.prs:
            client.create_comment(pr.number, merged(default_branch, owner, repo))

    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        futures = [
            pool.submit(unlock),
            pool.submit(delete_branches),
            pool.submit(post_comments),
        ]
        for f in concurrent.futures.as_completed(futures):
            exc = f.exception()
            if exc:
                log.warning("Cleanup task failed: %s", exc)


def fail_batch(
    client: GitHubClientProtocol,
    batch: Batch,
    reason: str,
) -> None:
    """Handle a failed batch: unlock, remove labels, notify."""
    try:
        _unlock(client, batch)
    except UnlockError as e:
        log.error("Failed to unlock during fail_batch: %s", e)

    for pr in batch.stack.prs:
        client.remove_label(pr.number, "locked")
        client.remove_label(pr.number, "queue")

    client.delete_branch(batch.branch)
    batch.status = BatchStatus.FAILED


def abort_batch(client: GitHubClientProtocol) -> None:
    """Abort any active batch: delete rulesets, remove locked labels, delete mq branches."""
    for rs in client.list_rulesets():
        if rs.get("name", "").startswith("mq-lock-"):
            try:
                _unlock_ruleset(client, rs["id"])
            except UnlockError as e:
                log.error("Failed to unlock ruleset %d during abort: %s", rs["id"], e)

    for pr_data in client.list_open_prs():
        labels = [lbl["name"] for lbl in pr_data.get("labels", [])]
        if "locked" in labels:
            client.remove_label(pr_data["number"], "locked")

    for branch in client.list_mq_branches():
        client.delete_branch(branch)
        log.info("Deleted branch %s", branch)
