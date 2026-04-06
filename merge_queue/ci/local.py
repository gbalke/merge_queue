"""Local CI provider — thin wrapper around LocalGitProvider CI methods."""

from __future__ import annotations


class LocalCIProvider:
    """Delegates CI operations to the underlying LocalGitProvider.

    Used for integration testing without GitHub API calls.
    """

    def __init__(self, local_provider) -> None:
        self._provider = local_provider

    def dispatch_ci(self, branch: str) -> None:
        self._provider.dispatch_ci(branch)

    def poll_ci_with_url(self, branch: str, timeout_seconds: int) -> tuple[bool, str]:
        return self._provider.poll_ci_with_url(branch, timeout_seconds)

    def poll_ci(self, branch: str, timeout_seconds: int) -> bool:
        return self._provider.poll_ci(branch, timeout_seconds)

    def get_pr_ci_status(self, pr_number: int) -> tuple[bool | None, str]:
        return self._provider.get_pr_ci_status(pr_number)

    def dispatch_ci_on_ref(self, ref: str) -> None:
        self._provider.dispatch_ci_on_ref(ref)

    def get_failed_job_info(self, run_url: str) -> tuple[str, str]:
        return self._provider.get_failed_job_info(run_url)

    def create_commit_status(
        self,
        sha: str,
        state: str,
        description: str = "",
        context: str = "Final Results",
    ) -> None:
        self._provider.create_commit_status(sha, state, description, context)
