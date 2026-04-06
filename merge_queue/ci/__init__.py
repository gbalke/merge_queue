"""Pluggable CI provider abstraction.

Defines the ``CIProvider`` protocol and a ``get_provider()`` factory that
returns the appropriate implementation based on config.
"""

from __future__ import annotations

from typing import Protocol


class CIProvider(Protocol):
    """Protocol for CI systems (GitHub Actions, Buildkite, etc.)."""

    def dispatch_ci(self, branch: str) -> None: ...

    def poll_ci_with_url(
        self, branch: str, timeout_seconds: int
    ) -> tuple[bool, str]: ...

    def poll_ci(self, branch: str, timeout_seconds: int) -> bool: ...

    def get_pr_ci_status(self, pr_number: int) -> tuple[bool | None, str]: ...

    def dispatch_ci_on_ref(self, ref: str) -> None: ...

    def get_failed_job_info(self, run_url: str) -> tuple[str, str]: ...

    def create_commit_status(
        self,
        sha: str,
        state: str,
        description: str = "",
        context: str = "Final Results",
    ) -> None: ...


def get_provider(config: dict | None = None, github_client=None) -> CIProvider:
    """Return a CIProvider based on config.

    Args:
        config: Parsed ``ci:`` section from merge-queue.yml, or ``None``.
        github_client: A ``GitHubClient`` (or compatible) instance used by
            the default GitHub Actions provider.

    Returns:
        A ``CIProvider`` implementation.

    Raises:
        ValueError: If the configured provider is unknown.
    """
    if config is None:
        from merge_queue.ci.github_actions import GitHubActionsCIProvider

        return GitHubActionsCIProvider(github_client)

    provider_name = config.get("provider", "github_actions")

    if provider_name == "github_actions":
        from merge_queue.ci.github_actions import GitHubActionsCIProvider

        return GitHubActionsCIProvider(
            github_client,
            workflow=config.get("workflow"),
            status_context=config.get("status_context"),
        )

    if provider_name == "buildkite":
        raise NotImplementedError("Buildkite CI provider is not yet implemented")

    raise ValueError(f"Unknown CI provider: {provider_name!r}")
