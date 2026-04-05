"""Tests for markdown sanitization in comments.py."""

from __future__ import annotations

import pytest

import merge_queue.comments as comments


@pytest.mark.parametrize(
    "char",
    ["|", "`", "*", "_", "~", "[", "]", "<", ">", "#"],
)
def test_sanitize_escapes_special_chars(char: str) -> None:
    result = comments._sanitize(f"before{char}after")
    assert f"\\{char}" in result
    assert char not in result.replace(f"\\{char}", "")


def test_sanitize_plain_text_unchanged() -> None:
    assert comments._sanitize("hello world 123") == "hello world 123"


def test_pr_table_escapes_title() -> None:
    stack = [{"number": 1, "title": "Fix `bug` | pipe"}]
    result = comments._pr_table(stack)
    assert "\\`bug\\`" in result
    assert "\\|" in result


def test_progress_escapes_branch_and_target() -> None:
    result = comments.progress(
        "running_ci",
        [],
        branch="mq/`evil`",
        target_branch="main`",
    )
    assert "\\`evil\\`" in result
    assert "main\\`" in result


def test_merge_conflict_escapes_target_branch() -> None:
    result = comments.merge_conflict("main`injected")
    assert "main\\`injected" in result


def test_merged_escapes_default_branch() -> None:
    result = comments.merged("main`injected")
    assert "main\\`injected" in result
