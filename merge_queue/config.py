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


def get_protected_paths(client) -> list[str]:
    """Read protected_paths from merge-queue.yml. Returns empty list if not found."""
    content = _get_config_content(client)
    if content is None:
        return []
    return _parse_yaml_list_section(content, "protected_paths")


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
    """Ensure all target branches and the mq/state branch have protection rulesets.

    Creates rulesets for any unprotected branches using MQ_ADMIN_TOKEN.
    Deletes rulesets for branches that are no longer in ``target_branches``.
    Ruleset name format: ``mq-protect-{branch_name}`` (``/`` replaced with ``-``).
    The ``mq/state`` branch gets a simpler ruleset (no CI required, just blocks
    non-admin pushes) via the existing ``create_ruleset`` update-block mechanism.

    If creation or deletion fails (e.g. no admin token, private repo without Pro),
    logs a warning but does not block the caller.
    """
    existing = client.list_rulesets()
    protected: set[str] = set()
    state_protected: set[str] = set()
    # Map branch -> ruleset dict for existing mq-protect-* rulesets
    protected_rulesets: dict[str, dict] = {}
    for rs in existing:
        name = rs.get("name", "")
        conditions = rs.get("conditions", {}).get("ref_name", {})
        if name.startswith("mq-protect-"):
            for pattern in conditions.get("include", []):
                # pattern is like "refs/heads/main"
                branch = pattern.removeprefix("refs/heads/")
                protected.add(branch)
                protected_rulesets[branch] = rs
        if name.startswith("mq-state-protect"):
            for pattern in conditions.get("include", []):
                branch = pattern.removeprefix("refs/heads/")
                state_protected.add(branch)

    target_set = set(target_branches)

    # Delete rulesets for branches no longer in config
    for branch in list(protected):
        if branch not in target_set:
            rs = protected_rulesets[branch]
            try:
                client.delete_ruleset(rs["id"])
                log.info("Removed protection for %s (no longer in config)", branch)
            except Exception as e:
                log.warning("Could not remove protection for %s: %s", branch, e)

    for branch in target_branches:
        if branch not in protected:
            log.info("Creating protection ruleset for %s", branch)
            _create_branch_protection(client, branch)

    state_branches = ["mq/state"]
    for sb in state_branches:
        if sb not in state_protected:
            log.info("Creating state branch protection for %s", sb)
            try:
                client.create_ruleset(
                    f"mq-state-protect-{sb.replace('/', '-')}",
                    [f"refs/heads/{sb}"],
                )
            except Exception as e:
                log.warning("Could not protect state branch %s: %s", sb, e)


def _create_branch_protection(client, branch: str) -> None:
    """Create a protection ruleset for a target branch."""
    try:
        log.info("Calling create_protection_ruleset for %s", branch)
        ruleset_id = client.create_protection_ruleset(
            name=f"mq-protect-{branch.replace('/', '-')}",
            branch=branch,
        )
        log.info("Created protection ruleset for %s (id=%s)", branch, ruleset_id)
    except Exception as e:
        detail = ""
        if hasattr(e, "response") and e.response is not None:
            try:
                detail = e.response.text
            except Exception:
                pass
        log.warning("Could not create protection for %s: %s %s", branch, e, detail)
