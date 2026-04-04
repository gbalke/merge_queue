"""Tests for configurable CI workflow name via MQ_CI_WORKFLOW env var."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from merge_queue.github_client import GitHubClient


def _make_client(monkeypatch, env_value: str | None = None) -> GitHubClient:
    """Instantiate GitHubClient with network calls stubbed out."""
    if env_value is None:
        monkeypatch.delenv("MQ_CI_WORKFLOW", raising=False)
    else:
        monkeypatch.setenv("MQ_CI_WORKFLOW", env_value)

    with patch("merge_queue.github_client.requests.Session"):
        return GitHubClient(owner="owner", repo="repo", token="tok")


class TestCiWorkflowDefault:
    def test_default_is_ci_yml(self, monkeypatch):
        client = _make_client(monkeypatch)
        assert client._ci_workflow == "ci.yml"


class TestCiWorkflowEnvVar:
    def test_custom_workflow_name(self, monkeypatch):
        client = _make_client(monkeypatch, env_value="test.yml")
        assert client._ci_workflow == "test.yml"

    @pytest.mark.parametrize(
        "workflow_name",
        ["test.yml", "build.yaml", "custom-ci.yml", "my-workflow.yml"],
    )
    def test_various_workflow_names(self, monkeypatch, workflow_name):
        client = _make_client(monkeypatch, env_value=workflow_name)
        assert client._ci_workflow == workflow_name


class TestDispatchCiUsesWorkflow:
    def test_dispatch_uses_configured_workflow(self, monkeypatch):
        client = _make_client(monkeypatch, env_value="test.yml")

        with patch.object(client, "_post") as mock_post:
            client.dispatch_ci("my-branch")

        mock_post.assert_called_once()
        url_arg = mock_post.call_args[0][0]
        assert "/actions/workflows/test.yml/dispatches" == url_arg

    def test_dispatch_default_uses_ci_yml(self, monkeypatch):
        client = _make_client(monkeypatch)

        with patch.object(client, "_post") as mock_post:
            client.dispatch_ci("my-branch")

        url_arg = mock_post.call_args[0][0]
        assert "/actions/workflows/ci.yml/dispatches" == url_arg

    def test_dispatch_passes_branch_ref(self, monkeypatch):
        client = _make_client(monkeypatch, env_value="test.yml")

        with patch.object(client, "_post") as mock_post:
            client.dispatch_ci("feature-branch")

        _, kwargs = mock_post.call_args
        assert kwargs["json"]["ref"] == "feature-branch"


class TestPollCiWithUrlUsesWorkflow:
    _LIST_RESPONSE = {
        "workflow_runs": [
            {
                "id": 42,
                "html_url": "https://github.com/owner/repo/actions/runs/42",
            }
        ]
    }
    _RUN_RESPONSE = {
        "id": 42,
        "html_url": "https://github.com/owner/repo/actions/runs/42",
        "status": "completed",
        "conclusion": "success",
    }

    def _get_side_effect(self, path: str, **_kwargs) -> dict:
        """Return list response for workflow runs, run detail otherwise."""
        if "workflow_runs" in path or "/runs" in path and "workflows" in path:
            return self._LIST_RESPONSE
        return self._RUN_RESPONSE

    def test_poll_uses_configured_workflow(self, monkeypatch):
        client = _make_client(monkeypatch, env_value="test.yml")

        with (
            patch("time.sleep"),
            patch.object(client, "_get", side_effect=self._get_side_effect) as mock_get,
        ):
            client.poll_ci_with_url("my-branch", timeout_seconds=60)

        # The first _get call should list runs for the configured workflow
        first_url = mock_get.call_args_list[0][0][0]
        assert first_url == "/actions/workflows/test.yml/runs"

    def test_poll_default_uses_ci_yml(self, monkeypatch):
        client = _make_client(monkeypatch)

        with (
            patch("time.sleep"),
            patch.object(client, "_get", side_effect=self._get_side_effect) as mock_get,
        ):
            client.poll_ci_with_url("my-branch", timeout_seconds=60)

        first_url = mock_get.call_args_list[0][0][0]
        assert first_url == "/actions/workflows/ci.yml/runs"
