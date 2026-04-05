"""Tests for break-glass authorization in do_enqueue."""

from __future__ import annotations

import base64
from unittest.mock import MagicMock, patch

import pytest

from merge_queue.cli import _is_break_glass_authorized, do_enqueue
from merge_queue.comments import break_glass_denied
from merge_queue.config import get_break_glass_users
from merge_queue.types import empty_state


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_client(permission: str = "none", config_users: list[str] | None = None):
    """Return a mock client with configurable permission and config file."""
    client = MagicMock()
    client.owner = "owner"
    client.repo = "repo"
    client.get_default_branch.return_value = "main"
    client.get_user_permission.return_value = permission

    if config_users is not None:
        lines = "break_glass_users:\n" + "".join(f"  - {u}\n" for u in config_users)
        encoded = base64.b64encode(lines.encode()).decode()
        client.get_file_content.return_value = {"content": encoded}
    else:
        # Simulate file not found
        client.get_file_content.side_effect = Exception("404 not found")

    return client


# ---------------------------------------------------------------------------
# get_break_glass_users
# ---------------------------------------------------------------------------


class TestGetBreakGlassUsers:
    def test_parses_user_list(self):
        client = _make_client(config_users=["alice", "bob"])
        assert get_break_glass_users(client) == ["alice", "bob"]

    def test_returns_empty_when_file_missing(self):
        client = _make_client(config_users=None)
        assert get_break_glass_users(client) == []

    def test_returns_empty_when_no_section(self):
        content = "other_key:\n  - value\n"
        encoded = base64.b64encode(content.encode()).decode()
        client = MagicMock()
        client.get_default_branch.return_value = "main"
        client.get_file_content.return_value = {"content": encoded}
        assert get_break_glass_users(client) == []

    def test_stops_at_next_key(self):
        content = "break_glass_users:\n  - alice\nother_key: true\n"
        encoded = base64.b64encode(content.encode()).decode()
        client = MagicMock()
        client.get_default_branch.return_value = "main"
        client.get_file_content.return_value = {"content": encoded}
        assert get_break_glass_users(client) == ["alice"]

    def test_returns_empty_on_client_error(self):
        client = MagicMock()
        client.get_default_branch.side_effect = Exception("network error")
        assert get_break_glass_users(client) == []

    def test_single_user(self):
        client = _make_client(config_users=["gbalke"])
        assert get_break_glass_users(client) == ["gbalke"]


# ---------------------------------------------------------------------------
# _is_break_glass_authorized
# ---------------------------------------------------------------------------


class TestIsBreakGlassAuthorized:
    def test_empty_sender_rejected(self):
        client = _make_client(permission="admin")
        assert _is_break_glass_authorized(client, "") is False

    def test_admin_permission_authorized(self):
        client = _make_client(permission="admin")
        assert _is_break_glass_authorized(client, "alice") is True

    def test_maintain_permission_authorized(self):
        client = _make_client(permission="maintain")
        assert _is_break_glass_authorized(client, "bob") is True

    def test_write_permission_rejected(self):
        client = _make_client(permission="write")
        assert _is_break_glass_authorized(client, "charlie") is False

    def test_read_permission_rejected(self):
        client = _make_client(permission="read")
        assert _is_break_glass_authorized(client, "dave") is False

    def test_no_permission_rejected(self):
        client = _make_client(permission="none")
        assert _is_break_glass_authorized(client, "eve") is False

    def test_config_allow_list_bypasses_permission_check(self):
        client = _make_client(permission="read", config_users=["trusted-bot"])
        assert _is_break_glass_authorized(client, "trusted-bot") is True
        # Permission API should not have been called (allow list matched first)
        client.get_user_permission.assert_not_called()

    def test_config_allow_list_unknown_user_falls_back_to_permission(self):
        client = _make_client(permission="admin", config_users=["other-user"])
        assert _is_break_glass_authorized(client, "alice") is True
        client.get_user_permission.assert_called_once_with("alice")

    def test_permission_api_exception_returns_false(self):
        client = _make_client(config_users=None)
        client.get_user_permission.side_effect = Exception("API error")
        assert _is_break_glass_authorized(client, "alice") is False

    def test_missing_config_falls_back_to_admin_check(self):
        """No config file → fall back to admin check only."""
        client = _make_client(permission="admin", config_users=None)
        assert _is_break_glass_authorized(client, "alice") is True

    def test_missing_config_non_admin_rejected(self):
        client = _make_client(permission="write", config_users=None)
        assert _is_break_glass_authorized(client, "alice") is False


# ---------------------------------------------------------------------------
# do_enqueue break-glass integration
# ---------------------------------------------------------------------------


def _make_enqueue_client(
    permission: str = "none", config_users: list[str] | None = None
):
    """Return a fully-configured mock client for do_enqueue tests."""
    client = _make_client(permission=permission, config_users=config_users)
    client.get_pr.return_value = {
        "state": "open",
        "head": {"ref": "feat-x", "sha": "abc123"},
        "base": {"ref": "main"},
        "title": "Test PR",
        "labels": [{"name": "queue"}, {"name": "break-glass"}],
    }
    client.list_open_prs.return_value = []
    client.list_mq_branches.return_value = []
    client.list_rulesets.return_value = []
    client.get_pr_ci_status.return_value = (
        False,
        "",
    )  # CI would fail without break-glass
    client.create_comment.return_value = 42
    client.create_deployment.return_value = 99
    client.update_deployment_status.return_value = None
    return client


@pytest.fixture
def mock_store_empty():
    with patch("merge_queue.cli.StateStore") as cls:
        store = MagicMock()
        store.read.return_value = empty_state()
        cls.return_value = store
        yield store


class TestDoEnqueueBreakGlass:
    def test_authorized_admin_bypasses_ci(self, monkeypatch, mock_store_empty):
        monkeypatch.setenv("MQ_SENDER", "alice")
        client = _make_enqueue_client(permission="admin")

        with patch("merge_queue.cli.QueueState") as qs:
            from tests.conftest import make_api_state

            qs.fetch.return_value = make_api_state()
            result = do_enqueue(client, 42)

        # Should not post a denial comment or remove the break-glass label
        denial_calls = [
            c
            for c in client.create_comment.call_args_list
            if "break-glass denied" in str(c)
        ]
        assert denial_calls == []
        # break-glass label must NOT have been removed (only queue label cleanup is ok)
        bg_remove_calls = [
            c for c in client.remove_label.call_args_list if "break-glass" in str(c)
        ]
        assert bg_remove_calls == []
        # break-glass was authorized and CI gate skipped; further results depend on batch mock
        assert result in (
            "queued_waiting",
            "queued",
            "batch_active",
            "no_stacks",
            "batch_error",
        )

    def test_unauthorized_user_gets_denied(self, monkeypatch, mock_store_empty):
        monkeypatch.setenv("MQ_SENDER", "random-user")
        client = _make_enqueue_client(permission="write")

        with patch("merge_queue.cli.QueueState") as qs:
            from tests.conftest import make_api_state

            qs.fetch.return_value = make_api_state()
            result = do_enqueue(client, 42)

        # Denial comment must have been posted
        assert client.create_comment.call_count >= 1
        comment_bodies = [str(c) for c in client.create_comment.call_args_list]
        assert any("break-glass denied" in b for b in comment_bodies)
        # break-glass label must be removed
        client.remove_label.assert_any_call(42, "break-glass")
        # CI gate kicks in and rejects (CI failed)
        assert result == "ci_not_ready"

    def test_config_allow_list_user_bypasses_ci(self, monkeypatch, mock_store_empty):
        monkeypatch.setenv("MQ_SENDER", "trusted-bot")
        client = _make_enqueue_client(permission="read", config_users=["trusted-bot"])

        with patch("merge_queue.cli.QueueState") as qs:
            from tests.conftest import make_api_state

            qs.fetch.return_value = make_api_state()
            result = do_enqueue(client, 42)

        denial_calls = [
            c
            for c in client.create_comment.call_args_list
            if "break-glass denied" in str(c)
        ]
        assert denial_calls == []
        # break-glass authorized and CI gate skipped; batch mock may cause batch_error
        assert result in (
            "queued_waiting",
            "queued",
            "batch_active",
            "no_stacks",
            "batch_error",
        )

    def test_empty_sender_gets_denied(self, monkeypatch, mock_store_empty):
        monkeypatch.delenv("MQ_SENDER", raising=False)
        client = _make_enqueue_client(permission="admin")

        with patch("merge_queue.cli.QueueState") as qs:
            from tests.conftest import make_api_state

            qs.fetch.return_value = make_api_state()
            result = do_enqueue(client, 42)

        comment_bodies = [str(c) for c in client.create_comment.call_args_list]
        assert any("break-glass denied" in b for b in comment_bodies)
        assert result == "ci_not_ready"


# ---------------------------------------------------------------------------
# break_glass_denied comment template
# ---------------------------------------------------------------------------


class TestBreakGlassDeniedComment:
    def test_contains_sender(self):
        msg = break_glass_denied("alice")
        assert "alice" in msg

    def test_contains_explanation(self):
        msg = break_glass_denied("alice")
        assert "break-glass denied" in msg
        assert "not authorized" in msg

    def test_includes_queue_link_when_owner_repo_provided(self):
        msg = break_glass_denied("alice", owner="myorg", repo="myrepo")
        assert "myorg/myrepo" in msg
        assert "Queue" in msg

    def test_no_queue_link_without_owner_repo(self):
        msg = break_glass_denied("alice")
        assert "Queue" not in msg

    @pytest.mark.parametrize("sender", ["gbalke", "admin-bot", "dependabot[bot]"])
    def test_various_senders(self, sender):
        msg = break_glass_denied(sender)
        assert sender in msg
