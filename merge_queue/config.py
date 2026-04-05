"""Read merge-queue.yml config from the repo root."""

from __future__ import annotations

import base64
import logging

log = logging.getLogger(__name__)


def _parse_yaml_list_section(content: str, section: str) -> list[str]:
    """Extract a simple YAML list section from file content without PyYAML.

    Parses blocks of the form::

        section_name:
          - value1
          - value2

    Returns an empty list if the section is not found or has no entries.
    """
    items: list[str] = []
    in_section = False
    for line in content.split("\n"):
        if line.strip() == f"{section}:":
            in_section = True
            continue
        if in_section:
            if line.strip().startswith("- "):
                items.append(line.strip()[2:].strip())
            else:
                # Any non-list line ends the section
                break
    return items


def _get_config_content(client) -> str | None:
    """Fetch and decode merge-queue.yml from the repo's default branch.

    Returns None if the file cannot be fetched.
    """
    try:
        default_branch = client.get_default_branch()
        data = client.get_file_content("merge-queue.yml", default_branch)
        return base64.b64decode(data["content"]).decode()
    except Exception:
        return None


def get_break_glass_users(client) -> list[str]:
    """Read break_glass_users from merge-queue.yml in the repo root.

    Parses a simple YAML list without requiring PyYAML:

        break_glass_users:
          - alice
          - bob

    Returns an empty list if the file does not exist, cannot be fetched,
    or contains no ``break_glass_users`` section.
    """
    content = _get_config_content(client)
    if content is None:
        return []
    return _parse_yaml_list_section(content, "break_glass_users")


def get_target_branches(client) -> list[str]:
    """Read target_branches from merge-queue.yml in the repo root.

    Parses a simple YAML list without requiring PyYAML:

        target_branches:
          - main
          - release/1.0

    The default branch is always included (prepended if not already listed).
    Returns ``[client.get_default_branch()]`` if the file does not exist,
    cannot be fetched, or contains no ``target_branches`` section.
    """
    default = client.get_default_branch()
    content = _get_config_content(client)
    if content is not None:
        branches = _parse_yaml_list_section(content, "target_branches")
        if branches:
            if default not in branches:
                branches.insert(0, default)
            return branches
    return [default]


def ensure_branch_protection(client, target_branches: list[str]) -> None:
    """Ensure all target branches have protection rulesets.

    Creates rulesets for any unprotected branches using MQ_ADMIN_TOKEN.
    Ruleset name format: ``mq-protect-{branch_name}`` (``/`` replaced with ``-``).

    If creation fails (e.g. no admin token, private repo without Pro), logs a
    warning but does not block the caller.
    """
    existing = client.list_rulesets()
    protected: set[str] = set()
    for rs in existing:
        if rs.get("name", "").startswith("mq-protect-"):
            conditions = rs.get("conditions", {}).get("ref_name", {})
            for pattern in conditions.get("include", []):
                # pattern is like "refs/heads/main"
                branch = pattern.removeprefix("refs/heads/")
                protected.add(branch)

    for branch in target_branches:
        if branch not in protected:
            log.info("Creating protection ruleset for %s", branch)
            _create_branch_protection(client, branch)


def _create_branch_protection(client, branch: str) -> None:
    """Create a protection ruleset for a target branch."""
    try:
        client.create_protection_ruleset(
            name=f"mq-protect-{branch.replace('/', '-')}",
            branch=branch,
        )
    except Exception as e:
        log.warning("Could not create protection for %s: %s", branch, e)
