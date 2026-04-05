"""Read merge-queue.yml config from the repo root."""

from __future__ import annotations

import base64


def get_break_glass_users(client) -> list[str]:
    """Read break_glass_users from merge-queue.yml in the repo root.

    Parses a simple YAML list without requiring PyYAML:

        break_glass_users:
          - alice
          - bob

    Returns an empty list if the file does not exist, cannot be fetched,
    or contains no ``break_glass_users`` section.
    """
    try:
        default_branch = client.get_default_branch()
        data = client.get_file_content("merge-queue.yml", default_branch)
        content = base64.b64decode(data["content"]).decode()
        users: list[str] = []
        in_section = False
        for line in content.split("\n"):
            if line.strip() == "break_glass_users:":
                in_section = True
                continue
            if in_section:
                if line.strip().startswith("- "):
                    users.append(line.strip()[2:].strip())
                else:
                    # Any non-list line ends the section
                    break
        return users
    except Exception:
        return []
