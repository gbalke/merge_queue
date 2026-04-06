"""GitHub Actions CI provider — thin wrapper around GitHubClient CI methods."""

from __future__ import annotations


class GitHubActionsCIProvider:
    """Delegates CI operations to the underlying GitHubClient.

    Accepts optional ``workflow`` and ``status_context`` params for future use
    (Phase 2); currently all calls pass straight through to the client.
    """

    def __init__(
        self,
        client,
        *,
        workflow: str | None = None,
        status_context: str | None = None,
    ) -> None:
        self._client = client
        self._workflow = workflow
        self._status_context = status_context

    def dispatch_ci(self, branch: str) -> None:
        self._client.dispatch_ci(branch)

    def poll_ci_with_url(self, branch: str, timeout_seconds: int) -> tuple[bool, str]:
        return self._client.poll_ci_with_url(branch, timeout_seconds)

    def poll_ci(self, branch: str, timeout_seconds: int) -> bool:
        return self._client.poll_ci(branch, timeout_seconds)

    def get_pr_ci_status(self, pr_number: int) -> tuple[bool | None, str]:
        return self._client.get_pr_ci_status(pr_number)

    def dispatch_ci_on_ref(self, ref: str) -> None:
        self._client.dispatch_ci_on_ref(ref)

    def get_failed_job_info(self, run_url: str) -> tuple[str, str]:
        return self._client.get_failed_job_info(run_url)

    def create_commit_status(
        self,
        sha: str,
        state: str,
        description: str = "",
        context: str = "Final Results",
    ) -> None:
        self._client.create_commit_status(sha, state, description, context)
