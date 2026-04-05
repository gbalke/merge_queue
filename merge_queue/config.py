"""Read merge-queue.yml config from the repo root."""

from __future__ import annotations

import base64


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

    Returns ``[client.get_default_branch()]`` if the file does not exist,
    cannot be fetched, or contains no ``target_branches`` section.  This
    preserves backward compatibility: repos with no config get the same
    behaviour as before.
    """
    content = _get_config_content(client)
    if content is not None:
        branches = _parse_yaml_list_section(content, "target_branches")
        if branches:
            return branches
    return [client.get_default_branch()]
