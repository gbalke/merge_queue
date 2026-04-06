"""Tests for the pluggable CI provider abstraction (Phase 1)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from merge_queue.ci import get_provider
from merge_queue.ci.github_actions import GitHubActionsCIProvider
from merge_queue.ci.local import LocalCIProvider


# --- get_provider factory ---


class TestGetProviderDefaultReturnsGitHubActions:
    def test_no_config(self):
        client = MagicMock()
        provider = get_provider(None, client)
        assert isinstance(provider, GitHubActionsCIProvider)

    def test_no_config_no_kwargs(self):
        client = MagicMock()
        provider = get_provider(github_client=client)
        assert isinstance(provider, GitHubActionsCIProvider)


class TestGetProviderExplicitGitHubActions:
    def test_explicit_provider(self):
        client = MagicMock()
        provider = get_provider({"provider": "github_actions"}, client)
        assert isinstance(provider, GitHubActionsCIProvider)

    def test_with_workflow_param(self):
        client = MagicMock()
        provider = get_provider(
            {"provider": "github_actions", "workflow": "build.yml"}, client
        )
        assert isinstance(provider, GitHubActionsCIProvider)
        assert provider._workflow == "build.yml"


class TestGetProviderUnknownRaises:
    def test_unknown_raises_value_error(self):
        with pytest.raises(ValueError, match="Unknown CI provider"):
            get_provider({"provider": "jenkins"}, MagicMock())

    def test_buildkite_raises_not_implemented(self):
        with pytest.raises(NotImplementedError):
            get_provider({"provider": "buildkite"}, MagicMock())


# --- GitHubActionsCIProvider delegation ---


class TestGitHubActionsDelegatesDispatch:
    def test_dispatch_ci(self):
        client = MagicMock()
        provider = GitHubActionsCIProvider(client)
        provider.dispatch_ci("my-branch")
        client.dispatch_ci.assert_called_once_with("my-branch")

    def test_dispatch_ci_on_ref(self):
        client = MagicMock()
        provider = GitHubActionsCIProvider(client)
        provider.dispatch_ci_on_ref("refs/heads/main")
        client.dispatch_ci_on_ref.assert_called_once_with("refs/heads/main")


class TestGitHubActionsDelegatesPoll:
    def test_poll_ci_with_url(self):
        client = MagicMock()
        client.poll_ci_with_url.return_value = (True, "https://example.com/run/1")
        provider = GitHubActionsCIProvider(client)
        result = provider.poll_ci_with_url("my-branch", 600)
        client.poll_ci_with_url.assert_called_once_with("my-branch", 600)
        assert result == (True, "https://example.com/run/1")

    def test_poll_ci(self):
        client = MagicMock()
        client.poll_ci.return_value = True
        provider = GitHubActionsCIProvider(client)
        result = provider.poll_ci("my-branch", 300)
        client.poll_ci.assert_called_once_with("my-branch", 300)
        assert result is True

    def test_get_pr_ci_status(self):
        client = MagicMock()
        client.get_pr_ci_status.return_value = (True, "https://example.com/run/1")
        provider = GitHubActionsCIProvider(client)
        result = provider.get_pr_ci_status(42)
        client.get_pr_ci_status.assert_called_once_with(42)
        assert result == (True, "https://example.com/run/1")

    def test_get_failed_job_info(self):
        client = MagicMock()
        client.get_failed_job_info.return_value = ("build", "compile")
        provider = GitHubActionsCIProvider(client)
        result = provider.get_failed_job_info("https://example.com/run/1")
        client.get_failed_job_info.assert_called_once_with("https://example.com/run/1")
        assert result == ("build", "compile")

    def test_create_commit_status(self):
        client = MagicMock()
        provider = GitHubActionsCIProvider(client)
        provider.create_commit_status("abc123", "success", "All good", "CI")
        client.create_commit_status.assert_called_once_with(
            "abc123", "success", "All good", "CI"
        )


# --- LocalCIProvider ---


class TestLocalCIProvider:
    def test_dispatch_ci(self):
        mock_provider = MagicMock()
        provider = LocalCIProvider(mock_provider)
        provider.dispatch_ci("feature")
        mock_provider.dispatch_ci.assert_called_once_with("feature")

    def test_poll_ci_with_url(self):
        mock_provider = MagicMock()
        mock_provider.poll_ci_with_url.return_value = (True, "")
        provider = LocalCIProvider(mock_provider)
        result = provider.poll_ci_with_url("feature", 60)
        assert result == (True, "")

    def test_get_pr_ci_status(self):
        mock_provider = MagicMock()
        mock_provider.get_pr_ci_status.return_value = (True, "")
        provider = LocalCIProvider(mock_provider)
        result = provider.get_pr_ci_status(1)
        assert result == (True, "")

    def test_create_commit_status(self):
        mock_provider = MagicMock()
        provider = LocalCIProvider(mock_provider)
        provider.create_commit_status("sha1", "success")
        mock_provider.create_commit_status.assert_called_once_with(
            "sha1", "success", "", "Final Results"
        )
