"""Tests for the protected paths feature."""

from __future__ import annotations

import base64
from unittest.mock import MagicMock, patch

import pytest

from merge_queue.cli import _has_authorized_approval, _matches_protected, do_enqueue
from merge_queue.comments import protected_path_approval_required
from merge_queue.config import get_protected_paths
from merge_queue.state import QueueState
from merge_queue.types import empty_state


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config_content(
    protected_paths: list[str | dict] | None = None,
    break_glass_users: list[str] | None = None,
) -> str:
    """Build a merge-queue.yml config string.

    ``protected_paths`` entries may be plain strings (simple format) or dicts
    with ``path`` and optional ``approvers`` keys (extended format).
    """
    lines = []
    if break_glass_users is not None:
        lines.append("break_glass_users:")
        for u in break_glass_users:
            lines.append(f"  - {u}")
    if protected_paths is not None:
        lines.append("protected_paths:")
        for p in protected_paths:
            if isinstance(p, str):
                lines.append(f"  - {p}")
            else:
                lines.append(f"  - path: {p['path']}")
                if p.get("approvers"):
                    lines.append("    approvers:")
                    for a in p["approvers"]:
                        lines.append(f"      - {a}")
    return "\n".join(lines) + "\n"


def _make_client(
    protected_paths: list[str | dict] | None = None,
    break_glass_users: list[str] | None = None,
    pr_files: list[str] | None = None,
    reviews: list[dict] | None = None,
    permission: str = "none",
) -> MagicMock:
    client = MagicMock()
    client.owner = "owner"
    client.repo = "repo"
    client.get_default_branch.return_value = "main"
    client.list_mq_branches.return_value = []
    client.list_rulesets.return_value = []
    client.list_open_prs.return_value = []
    client.get_branch_sha.return_value = "abc123"
    client.compare_commits.return_value = "ahead"
    client.create_ruleset.return_value = 42
    client.poll_ci.return_value = True
    client.get_user_permission.return_value = permission
    client.get_pr.return_value = {
        "number": 1,
        "state": "open",
        "title": "Test PR",
        "head": {"ref": "feature/test", "sha": "sha-1"},
        "base": {"ref": "main"},
        "labels": [{"name": "queue"}],
    }
    client.get_pr_ci_status.return_value = (True, "")
    client.get_pr_files.return_value = pr_files or []
    client.get_pr_reviews.return_value = reviews or []

    content = _make_config_content(protected_paths, break_glass_users)
    encoded = base64.b64encode(content.encode()).decode()
    client.get_file_content.return_value = {"content": encoded}

    return client


@pytest.fixture
def mock_store_empty():
    with patch("merge_queue.cli.StateStore") as cls:
        store = MagicMock()
        store.read.return_value = empty_state()
        cls.return_value = store
        yield store


@pytest.fixture
def mock_queue_state():
    api_state = QueueState(
        default_branch="main",
        mq_branches=[],
        rulesets=[],
        prs=[],
        all_pr_data=[],
    )
    with patch("merge_queue.cli.QueueState") as cls:
        cls.fetch.return_value = api_state
        yield api_state


# ---------------------------------------------------------------------------
# get_protected_paths — simple string format (backward compat)
# ---------------------------------------------------------------------------


class TestGetProtectedPathsSimpleFormat:
    def test_parses_path_list(self):
        client = _make_client(protected_paths=["merge-queue.yml", "merge_queue/"])
        result = get_protected_paths(client)
        assert result == [
            {"path": "merge-queue.yml", "approvers": []},
            {"path": "merge_queue/", "approvers": []},
        ]

    def test_returns_empty_when_file_missing(self):
        client = MagicMock()
        client.get_default_branch.return_value = "main"
        client.get_file_content.side_effect = Exception("404 not found")
        assert get_protected_paths(client) == []

    def test_returns_empty_when_no_section(self):
        content = "break_glass_users:\n  - alice\n"
        encoded = base64.b64encode(content.encode()).decode()
        client = MagicMock()
        client.get_default_branch.return_value = "main"
        client.get_file_content.return_value = {"content": encoded}
        assert get_protected_paths(client) == []

    def test_single_exact_path(self):
        client = _make_client(protected_paths=["merge-queue.yml"])
        result = get_protected_paths(client)
        assert result == [{"path": "merge-queue.yml", "approvers": []}]

    def test_directory_and_file_paths(self):
        client = _make_client(
            protected_paths=["merge-queue.yml", ".github/workflows/", "merge_queue/"]
        )
        result = get_protected_paths(client)
        assert result == [
            {"path": "merge-queue.yml", "approvers": []},
            {"path": ".github/workflows/", "approvers": []},
            {"path": "merge_queue/", "approvers": []},
        ]


# ---------------------------------------------------------------------------
# get_protected_paths — path+approvers format
# ---------------------------------------------------------------------------


class TestGetProtectedPathsExtendedFormat:
    def test_single_path_with_approvers(self):
        client = _make_client(
            protected_paths=[
                {"path": "merge-queue.yml", "approvers": ["gbalke", "infra-lead"]}
            ]
        )
        result = get_protected_paths(client)
        assert result == [
            {"path": "merge-queue.yml", "approvers": ["gbalke", "infra-lead"]}
        ]

    def test_path_with_no_approvers_key(self):
        # A path: block with no approvers: sub-key falls back to []
        content = "protected_paths:\n  - path: merge-queue.yml\n"
        encoded = base64.b64encode(content.encode()).decode()
        client = MagicMock()
        client.get_default_branch.return_value = "main"
        client.get_file_content.return_value = {"content": encoded}
        result = get_protected_paths(client)
        assert result == [{"path": "merge-queue.yml", "approvers": []}]

    def test_multiple_paths_with_approvers(self):
        client = _make_client(
            protected_paths=[
                {"path": "merge-queue.yml", "approvers": ["gbalke", "infra-lead"]},
                {"path": ".github/workflows/", "approvers": ["gbalke", "devops-bot"]},
            ]
        )
        result = get_protected_paths(client)
        assert result == [
            {"path": "merge-queue.yml", "approvers": ["gbalke", "infra-lead"]},
            {"path": ".github/workflows/", "approvers": ["gbalke", "devops-bot"]},
        ]

    def test_mixed_simple_and_extended_format(self):
        """Simple string and path+approvers entries can coexist."""
        content = (
            "protected_paths:\n"
            "  - merge-queue.yml\n"
            "  - path: .github/workflows/\n"
            "    approvers:\n"
            "      - gbalke\n"
            "      - devops-bot\n"
            "  - merge_queue/\n"
        )
        encoded = base64.b64encode(content.encode()).decode()
        client = MagicMock()
        client.get_default_branch.return_value = "main"
        client.get_file_content.return_value = {"content": encoded}
        result = get_protected_paths(client)
        assert result == [
            {"path": "merge-queue.yml", "approvers": []},
            {"path": ".github/workflows/", "approvers": ["gbalke", "devops-bot"]},
            {"path": "merge_queue/", "approvers": []},
        ]


# ---------------------------------------------------------------------------
# _matches_protected
# ---------------------------------------------------------------------------


class TestMatchesProtected:
    def test_exact_file_match(self):
        result = _matches_protected(
            ["merge-queue.yml", "src/main.py"],
            [{"path": "merge-queue.yml", "approvers": []}],
        )
        assert result == [{"path": "merge-queue.yml", "approvers": []}]

    def test_directory_match(self):
        result = _matches_protected(
            ["merge_queue/cli.py", "tests/test_cli.py"],
            [{"path": "merge_queue/", "approvers": []}],
        )
        assert result == [{"path": "merge_queue/", "approvers": []}]

    def test_no_match(self):
        result = _matches_protected(
            ["src/main.py", "README.md"],
            [{"path": "merge-queue.yml", "approvers": []}],
        )
        assert result == []

    def test_multiple_patterns_matched(self):
        result = _matches_protected(
            ["merge-queue.yml", "merge_queue/cli.py"],
            [
                {"path": "merge-queue.yml", "approvers": []},
                {"path": "merge_queue/", "approvers": []},
            ],
        )
        paths = {e["path"] for e in result}
        assert paths == {"merge-queue.yml", "merge_queue/"}

    def test_directory_pattern_no_partial_file_match(self):
        # "merge_queue/" should NOT match "merge_queue_extra/foo.py"
        result = _matches_protected(
            ["merge_queue_extra/foo.py"],
            [{"path": "merge_queue/", "approvers": []}],
        )
        assert result == []

    def test_each_pattern_appears_once_for_multiple_files(self):
        result = _matches_protected(
            ["merge_queue/cli.py", "merge_queue/config.py"],
            [{"path": "merge_queue/", "approvers": []}],
        )
        assert result == [{"path": "merge_queue/", "approvers": []}]

    def test_empty_files(self):
        assert (
            _matches_protected([], [{"path": "merge-queue.yml", "approvers": []}]) == []
        )

    def test_empty_patterns(self):
        assert _matches_protected(["merge-queue.yml"], []) == []

    def test_approvers_preserved_in_match(self):
        result = _matches_protected(
            ["merge-queue.yml"],
            [{"path": "merge-queue.yml", "approvers": ["gbalke", "infra-lead"]}],
        )
        assert result == [
            {"path": "merge-queue.yml", "approvers": ["gbalke", "infra-lead"]}
        ]

    def test_legacy_string_patterns(self):
        """Plain string entries should still work for backward compat."""
        result = _matches_protected(["merge-queue.yml"], ["merge-queue.yml"])
        assert result == [{"path": "merge-queue.yml", "approvers": []}]

    def test_legacy_string_directory_match(self):
        result = _matches_protected(["dir/foo.py"], ["dir/"])
        assert result == [{"path": "dir/", "approvers": []}]

    @pytest.mark.parametrize(
        "files,patterns,expected_paths",
        [
            (["a.txt"], [{"path": "a.txt", "approvers": []}], ["a.txt"]),
            (["a.txt"], [{"path": "b.txt", "approvers": []}], []),
            (["dir/foo.py"], [{"path": "dir/", "approvers": []}], ["dir/"]),
            (["other/foo.py"], [{"path": "dir/", "approvers": []}], []),
            (
                ["a.txt", "dir/foo.py"],
                [{"path": "a.txt", "approvers": []}, {"path": "dir/", "approvers": []}],
                ["a.txt", "dir/"],
            ),
        ],
    )
    def test_parametrized(self, files, patterns, expected_paths):
        result = _matches_protected(files, patterns)
        assert [e["path"] for e in result] == expected_paths


# ---------------------------------------------------------------------------
# _has_authorized_approval
# ---------------------------------------------------------------------------


class TestHasAuthorizedApproval:
    def _client_with_reviews(
        self,
        reviews: list[dict],
        break_glass_users: list[str] | None = None,
        permission: str = "none",
    ) -> MagicMock:
        return _make_client(
            break_glass_users=break_glass_users or [],
            reviews=reviews,
            permission=permission,
        )

    def test_approved_by_break_glass_user(self):
        client = self._client_with_reviews(
            reviews=[{"user": "gbalke", "state": "APPROVED"}],
            break_glass_users=["gbalke"],
        )
        assert _has_authorized_approval(client, 1) is True

    def test_approved_by_admin(self):
        client = self._client_with_reviews(
            reviews=[{"user": "admin-user", "state": "APPROVED"}],
            break_glass_users=[],
            permission="admin",
        )
        assert _has_authorized_approval(client, 1) is True

    def test_approved_by_maintain(self):
        client = self._client_with_reviews(
            reviews=[{"user": "maintainer", "state": "APPROVED"}],
            break_glass_users=[],
            permission="maintain",
        )
        assert _has_authorized_approval(client, 1) is True

    def test_not_approved(self):
        client = self._client_with_reviews(
            reviews=[{"user": "random", "state": "CHANGES_REQUESTED"}],
            break_glass_users=[],
            permission="none",
        )
        assert _has_authorized_approval(client, 1) is False

    def test_approved_by_non_admin_non_break_glass(self):
        client = self._client_with_reviews(
            reviews=[{"user": "contributor", "state": "APPROVED"}],
            break_glass_users=["gbalke"],
            permission="write",
        )
        assert _has_authorized_approval(client, 1) is False

    def test_no_reviews(self):
        client = self._client_with_reviews(
            reviews=[],
            break_glass_users=["gbalke"],
        )
        assert _has_authorized_approval(client, 1) is False

    def test_latest_review_counts(self):
        # User first approved then requested changes — net result is not approved
        client = self._client_with_reviews(
            reviews=[
                {"user": "gbalke", "state": "APPROVED"},
                {"user": "gbalke", "state": "CHANGES_REQUESTED"},
            ],
            break_glass_users=["gbalke"],
        )
        assert _has_authorized_approval(client, 1) is False

    def test_get_user_permission_exception_ignored(self):
        client = self._client_with_reviews(
            reviews=[{"user": "stranger", "state": "APPROVED"}],
            break_glass_users=[],
        )
        client.get_user_permission.side_effect = Exception("network error")
        assert _has_authorized_approval(client, 1) is False

    # Per-path approvers tests

    def test_per_path_approver_approved(self):
        """path_approvers provided — approval by a listed user is sufficient."""
        client = self._client_with_reviews(
            reviews=[{"user": "infra-lead", "state": "APPROVED"}],
            break_glass_users=[],  # not in global list
            permission="none",
        )
        assert (
            _has_authorized_approval(client, 1, path_approvers=["infra-lead"]) is True
        )

    def test_per_path_approver_not_in_list(self):
        """Reviewer is not in path_approvers and not admin — rejected."""
        client = self._client_with_reviews(
            reviews=[{"user": "random-dev", "state": "APPROVED"}],
            break_glass_users=["random-dev"],  # global list, but per-path overrides
            permission="none",
        )
        # When path_approvers is non-empty, global break_glass_users is NOT consulted
        assert (
            _has_authorized_approval(client, 1, path_approvers=["infra-lead"]) is False
        )

    def test_per_path_approver_admin_always_allowed(self):
        """Admins can approve even when path_approvers is provided."""
        client = self._client_with_reviews(
            reviews=[{"user": "admin-user", "state": "APPROVED"}],
            break_glass_users=[],
            permission="admin",
        )
        assert (
            _has_authorized_approval(client, 1, path_approvers=["infra-lead"]) is True
        )

    def test_empty_path_approvers_falls_back_to_break_glass(self):
        """Empty path_approvers list falls back to global break_glass_users."""
        client = self._client_with_reviews(
            reviews=[{"user": "gbalke", "state": "APPROVED"}],
            break_glass_users=["gbalke"],
        )
        assert _has_authorized_approval(client, 1, path_approvers=[]) is True

    def test_none_path_approvers_falls_back_to_break_glass(self):
        """None path_approvers falls back to global break_glass_users."""
        client = self._client_with_reviews(
            reviews=[{"user": "gbalke", "state": "APPROVED"}],
            break_glass_users=["gbalke"],
        )
        assert _has_authorized_approval(client, 1, path_approvers=None) is True


# ---------------------------------------------------------------------------
# Comment template
# ---------------------------------------------------------------------------


class TestProtectedPathApprovalRequired:
    def test_contains_paths_string_format(self):
        msg = protected_path_approval_required(["merge-queue.yml", "merge_queue/"])
        assert "`merge-queue.yml`" in msg
        assert "`merge_queue/`" in msg

    def test_contains_paths_dict_format(self):
        msg = protected_path_approval_required(
            [
                {"path": "merge-queue.yml", "approvers": []},
                {"path": "merge_queue/", "approvers": []},
            ]
        )
        assert "`merge-queue.yml`" in msg
        assert "`merge_queue/`" in msg

    def test_contains_approval_language(self):
        msg = protected_path_approval_required(["merge-queue.yml"])
        assert "Approval required" in msg
        assert "protected paths" in msg

    def test_contains_footer_with_owner_repo(self, monkeypatch):
        monkeypatch.setenv("GITHUB_RUN_URL", "")
        msg = protected_path_approval_required(["a.txt"], owner="myorg", repo="myrepo")
        assert "myorg" in msg
        assert "myrepo" in msg

    def test_single_path(self):
        msg = protected_path_approval_required(["merge-queue.yml"])
        assert "- `merge-queue.yml`" in msg

    def test_per_path_approvers_shown(self):
        """When a path has approvers, they should appear in the comment."""
        msg = protected_path_approval_required(
            [{"path": "merge-queue.yml", "approvers": ["gbalke", "infra-lead"]}]
        )
        assert "`merge-queue.yml`" in msg
        assert "@gbalke" in msg
        assert "@infra-lead" in msg

    def test_no_approvers_no_at_mention(self):
        """Paths without approvers should not produce @-mentions."""
        msg = protected_path_approval_required(
            [{"path": "merge-queue.yml", "approvers": []}]
        )
        assert "@" not in msg


# ---------------------------------------------------------------------------
# do_enqueue integration
# ---------------------------------------------------------------------------


class TestDoEnqueueProtectedPaths:
    def test_rejects_pr_touching_protected_path_without_approval(
        self, mock_store_empty, mock_queue_state
    ):
        client = _make_client(
            protected_paths=["merge-queue.yml"],
            break_glass_users=["gbalke"],
            pr_files=["merge-queue.yml", "src/main.py"],
            reviews=[],  # No approvals
        )
        result = do_enqueue(client, 1)
        assert result == "approval_required"
        client.remove_label.assert_called_with(1, "queue")
        client.create_comment.assert_called_once()
        comment_body = client.create_comment.call_args[0][1]
        assert "merge-queue.yml" in comment_body

    def test_accepts_pr_touching_protected_path_with_break_glass_approval(
        self, mock_store_empty, mock_queue_state
    ):
        client = _make_client(
            protected_paths=["merge-queue.yml"],
            break_glass_users=["gbalke"],
            pr_files=["merge-queue.yml"],
            reviews=[{"user": "gbalke", "state": "APPROVED"}],
        )
        with patch("merge_queue.cli.do_process", return_value="processing"):
            result = do_enqueue(client, 1)
        assert result != "approval_required"
        # label should NOT have been removed for the queue
        remove_calls = [
            c for c in client.remove_label.call_args_list if c[0] == (1, "queue")
        ]
        assert not remove_calls

    def test_accepts_pr_touching_protected_path_with_admin_approval(
        self, mock_store_empty, mock_queue_state
    ):
        client = _make_client(
            protected_paths=["merge-queue.yml"],
            break_glass_users=[],
            pr_files=["merge-queue.yml"],
            reviews=[{"user": "admin-user", "state": "APPROVED"}],
            permission="admin",
        )
        with patch("merge_queue.cli.do_process", return_value="processing"):
            result = do_enqueue(client, 1)
        assert result != "approval_required"

    def test_no_protected_paths_skips_check(self, mock_store_empty, mock_queue_state):
        client = _make_client(
            protected_paths=[],
            pr_files=["merge-queue.yml", "src/main.py"],
            reviews=[],
        )
        with patch("merge_queue.cli.do_process", return_value="processing"):
            result = do_enqueue(client, 1)
        assert result != "approval_required"
        # get_pr_files should NOT be called when no protected paths are configured
        client.get_pr_files.assert_not_called()

    def test_pr_not_touching_protected_path_skips_check(
        self, mock_store_empty, mock_queue_state
    ):
        client = _make_client(
            protected_paths=["merge-queue.yml"],
            break_glass_users=["gbalke"],
            pr_files=["src/main.py", "tests/test_foo.py"],
            reviews=[],
        )
        with patch("merge_queue.cli.do_process", return_value="processing"):
            result = do_enqueue(client, 1)
        assert result != "approval_required"

    def test_directory_match_requires_approval(
        self, mock_store_empty, mock_queue_state
    ):
        client = _make_client(
            protected_paths=["merge_queue/"],
            break_glass_users=["gbalke"],
            pr_files=["merge_queue/cli.py"],
            reviews=[],
        )
        result = do_enqueue(client, 1)
        assert result == "approval_required"

    def test_per_path_approver_grants_access(self, mock_store_empty, mock_queue_state):
        """A user in path-specific approvers can approve even if not in break_glass_users."""
        client = _make_client(
            protected_paths=[{"path": "merge-queue.yml", "approvers": ["infra-lead"]}],
            break_glass_users=["gbalke"],
            pr_files=["merge-queue.yml"],
            reviews=[{"user": "infra-lead", "state": "APPROVED"}],
            permission="none",
        )
        with patch("merge_queue.cli.do_process", return_value="processing"):
            result = do_enqueue(client, 1)
        assert result != "approval_required"

    def test_per_path_approver_wrong_user_rejected(
        self, mock_store_empty, mock_queue_state
    ):
        """A break_glass user cannot approve if path has its own non-empty approvers list."""
        client = _make_client(
            protected_paths=[{"path": "merge-queue.yml", "approvers": ["infra-lead"]}],
            break_glass_users=["gbalke"],
            pr_files=["merge-queue.yml"],
            reviews=[{"user": "gbalke", "state": "APPROVED"}],
            permission="none",
        )
        result = do_enqueue(client, 1)
        assert result == "approval_required"

    def test_fallback_to_break_glass_when_no_path_approvers(
        self, mock_store_empty, mock_queue_state
    ):
        """Path with empty approvers list falls back to global break_glass_users."""
        client = _make_client(
            protected_paths=[{"path": "merge-queue.yml", "approvers": []}],
            break_glass_users=["gbalke"],
            pr_files=["merge-queue.yml"],
            reviews=[{"user": "gbalke", "state": "APPROVED"}],
        )
        with patch("merge_queue.cli.do_process", return_value="processing"):
            result = do_enqueue(client, 1)
        assert result != "approval_required"

    def test_multiple_protected_paths_all_must_be_approved(
        self, mock_store_empty, mock_queue_state
    ):
        """Both touched paths must be approved — one approval is not enough."""
        client = _make_client(
            protected_paths=[
                {"path": "merge-queue.yml", "approvers": ["infra-lead"]},
                {"path": ".github/workflows/", "approvers": ["devops-bot"]},
            ],
            break_glass_users=[],
            pr_files=["merge-queue.yml", ".github/workflows/ci.yml"],
            reviews=[{"user": "infra-lead", "state": "APPROVED"}],
            permission="none",
        )
        # infra-lead approved merge-queue.yml but not .github/workflows/
        result = do_enqueue(client, 1)
        assert result == "approval_required"

    def test_multiple_protected_paths_both_approved(
        self, mock_store_empty, mock_queue_state
    ):
        """All touched paths approved — PR should enter the queue."""
        client = _make_client(
            protected_paths=[
                {"path": "merge-queue.yml", "approvers": ["infra-lead"]},
                {"path": ".github/workflows/", "approvers": ["infra-lead"]},
            ],
            break_glass_users=[],
            pr_files=["merge-queue.yml", ".github/workflows/ci.yml"],
            reviews=[{"user": "infra-lead", "state": "APPROVED"}],
            permission="none",
        )
        with patch("merge_queue.cli.do_process", return_value="processing"):
            result = do_enqueue(client, 1)
        assert result != "approval_required"
