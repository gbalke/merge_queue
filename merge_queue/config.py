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


def parse_metrics_config(content: str) -> dict | None:
    """Parse the ``metrics`` section from config file content.

    Returns a dict with keys like ``backend`` and ``endpoint``, or ``None``
    if no ``metrics:`` section is present.  An empty ``metrics:`` section
    (immediately followed by another top-level key or EOF) returns ``{}``.
    """
    result: dict[str, str] = {}
    in_section = False
    for line in content.split("\n"):
        stripped = line.strip()
        if stripped == "metrics:":
            in_section = True
            continue
        if in_section:
            # A non-indented, non-empty line ends the section
            if stripped and not line[0].isspace():
                break
            if ":" in stripped:
                key, _, value = stripped.partition(":")
                result[key.strip()] = value.strip()
    if not in_section:
        return None
    return result


def get_metrics_config(client) -> dict | None:
    """Read the ``metrics`` section from ``merge-queue.yml``.

    Returns ``None`` if the file cannot be fetched or has no metrics section.
    """
    content = _get_config_content(client)
    if content is None:
        return None
    return parse_metrics_config(content)


def parse_ci_config(content: str) -> dict | None:
    """Parse the ``ci`` section from config file content.

    Returns a dict with keys like ``provider``, ``workflow``, and
    ``status_context``, or ``None`` if no ``ci:`` section is present.
    An empty ``ci:`` section (immediately followed by another top-level key
    or EOF) returns ``{}``.
    """
    result: dict[str, str] = {}
    in_section = False
    for line in content.split("\n"):
        stripped = line.strip()
        if stripped == "ci:":
            in_section = True
            continue
        if in_section:
            # A non-indented, non-empty line ends the section
            if stripped and not line[0].isspace():
                break
            if ":" in stripped:
                key, _, value = stripped.partition(":")
                result[key.strip()] = value.strip()
    if not in_section:
        return None
    return result


def get_ci_config(client) -> dict | None:
    """Read the ``ci`` section from ``merge-queue.yml``.

    Returns ``None`` if the file cannot be fetched or has no ci section.
    """
    content = _get_config_content(client)
    if content is None:
        return None
    return parse_ci_config(content)


def _leading_spaces(line: str) -> int:
    """Return the number of leading spaces in a line."""
    return len(line) - len(line.lstrip(" "))


def _parse_protected_paths_section(content: str) -> list[dict]:
    """Parse the protected_paths section from config content.

    Handles two entry formats:

    Simple string::

        protected_paths:
          - merge-queue.yml

    Path+approvers block::

        protected_paths:
          - path: merge-queue.yml
            approvers:
              - alice
              - bob

    Simple string entries get ``approvers=[]`` (falls back to break_glass_users).
    Path+approvers blocks without an ``approvers`` key also get ``approvers=[]``.

    Indentation levels are used to distinguish top-level path list items from
    nested approver list items, avoiding a PyYAML dependency.
    """
    items: list[dict] = []
    in_section = False
    current_entry: dict | None = None
    in_approvers = False
    # Indentation of top-level protected_paths entries (e.g. 2 for "  - path:")
    entry_indent: int = -1
    # Indentation of approver list items (e.g. 6 for "      - alice")
    approver_indent: int = -1

    for line in content.split("\n"):
        stripped = line.strip()

        if not stripped:
            continue

        if stripped == "protected_paths:":
            in_section = True
            entry_indent = -1
            approver_indent = -1
            continue

        if not in_section:
            continue

        # A line starting at column 0 ends the section
        if line[0] != " ":
            if current_entry is not None:
                items.append(current_entry)
                current_entry = None
            in_section = False
            in_approvers = False
            continue

        indent = _leading_spaces(line)

        # Learn the entry indentation level from the first list item seen
        if entry_indent == -1 and stripped.startswith("- "):
            entry_indent = indent

        # A list item at the entry-level indent = a new protected_paths entry
        if indent == entry_indent and stripped.startswith("- "):
            if current_entry is not None:
                items.append(current_entry)
                current_entry = None
            in_approvers = False
            approver_indent = -1

            value = stripped[2:].strip()
            if value.startswith("path:"):
                path_val = value[len("path:") :].strip()
                current_entry = {"path": path_val, "approvers": []}
            else:
                # Simple string entry — no sub-keys expected
                items.append({"path": value, "approvers": []})
            continue

        # Deeper-indented lines are sub-keys / sub-items of the current entry
        if current_entry is not None:
            if stripped == "approvers:":
                in_approvers = True
                approver_indent = -1
                continue
            if in_approvers and stripped.startswith("- "):
                # Learn the approver-item indentation from the first one
                if approver_indent == -1:
                    approver_indent = indent
                if indent == approver_indent:
                    current_entry["approvers"].append(stripped[2:].strip())
                    continue
            # Any unrecognised sub-key turns off approver-collection mode
            if not (in_approvers and stripped.startswith("- ")):
                in_approvers = False

    if current_entry is not None:
        items.append(current_entry)

    return items


def get_protected_paths(client) -> list[dict]:
    """Read protected_paths from merge-queue.yml.

    Returns a list of dicts of the form::

        [{"path": "merge-queue.yml", "approvers": ["gbalke"]}, ...]

    Simple string entries get ``approvers=[]``, which falls back to
    ``break_glass_users`` + admins at approval-check time.

    Returns an empty list if the file does not exist, cannot be fetched,
    or contains no ``protected_paths`` section.
    """
    content = _get_config_content(client)
    if content is None:
        return []
    return _parse_protected_paths_section(content)


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


def _ensure_mq_branches_protected(client) -> None:
    """Ensure mq/* batch branches are protected — only admin can write.

    Creates a single ``mq-branches-protect`` ruleset covering ``refs/heads/mq/*``
    that blocks both updates (pushes) and deletions for everyone except the admin
    role (actor_id=5, RepositoryRole).  The MQ admin token is the only identity
    that bypasses this ruleset so it can create/delete batch branches.

    ``mq/state`` is explicitly excluded because protecting it blocks the atomic
    ``commit_files`` writes via Git Data API.  The MQ concurrency group ensures
    single-writer access to mq/state instead.

    Migrates legacy ``mq-state-protect-*`` rulesets by deleting them once the new
    unified ruleset is in place.

    If creation or deletion fails (e.g. no admin token), logs a warning and
    returns without raising.
    """
    existing = client.list_rulesets()

    # IDs of legacy mq-state-protect-* rulesets to clean up after migration.
    legacy_ids: list[int] = []
    for rs in existing:
        name = rs.get("name", "")
        if name == "mq-branches-protect":
            # Already present — nothing to do. Clean up legacy ones anyway.
            for rs2 in existing:
                if rs2.get("name", "").startswith("mq-state-protect"):
                    _try_delete_ruleset(client, rs2)
            return
        if name.startswith("mq-state-protect"):
            if "id" in rs:
                legacy_ids.append(rs["id"])

    try:
        r = client._admin_session.post(
            f"{client._base_url}/rulesets",
            json={
                "name": "mq-branches-protect",
                "target": "branch",
                "enforcement": "active",
                "conditions": {
                    "ref_name": {
                        "include": ["refs/heads/mq/*"],
                        "exclude": ["refs/heads/mq/state"],
                    }
                },
                "rules": [
                    {"type": "update"},
                    {"type": "deletion"},
                ],
                "bypass_actors": [
                    {
                        "actor_id": 5,
                        "actor_type": "RepositoryRole",
                        "bypass_mode": "always",
                    }
                ],
            },
        )
        r.raise_for_status()
        log.info("Created mq-branches-protect ruleset (id=%s)", r.json().get("id"))
    except Exception as e:
        log.warning("Could not protect mq/* branches: %s", e)
        return

    # New ruleset is live — delete the now-superseded legacy rulesets.
    for ruleset_id in legacy_ids:
        try:
            client.delete_ruleset(ruleset_id)
            log.info("Deleted legacy mq-state-protect ruleset id=%s", ruleset_id)
        except Exception as e:
            log.warning("Could not delete legacy ruleset id=%s: %s", ruleset_id, e)


def _try_delete_ruleset(client, rs: dict) -> None:
    """Best-effort deletion of a ruleset; logs but does not raise on failure."""
    ruleset_id = rs.get("id")
    if ruleset_id is None:
        return
    try:
        client.delete_ruleset(ruleset_id)
        log.info("Deleted legacy ruleset '%s' id=%s", rs.get("name"), ruleset_id)
    except Exception as e:
        log.warning(
            "Could not delete legacy ruleset '%s' id=%s: %s",
            rs.get("name"),
            ruleset_id,
            e,
        )


def ensure_branch_protection(client, target_branches: list[str]) -> None:
    """Ensure all target branches and mq/* branches have protection rulesets.

    Creates rulesets for any unprotected target branches using MQ_ADMIN_TOKEN.
    Deletes rulesets for branches that are no longer in ``target_branches``.
    Ruleset name format: ``mq-protect-{branch_name}`` (``/`` replaced with ``-``).

    A single ``mq-branches-protect`` ruleset covers all ``mq/*`` branches
    (excluding ``mq/state``) so that only the admin token can push to
    lock/batch branches.  ``mq/state`` is excluded because protecting it
    blocks atomic writes.  Any legacy ``mq-state-protect-*`` rulesets are
    cleaned up automatically.

    If creation or deletion fails (e.g. no admin token, private repo without Pro),
    logs a warning but does not block the caller.
    """
    existing = client.list_rulesets()
    protected: set[str] = set()
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

    _ensure_mq_branches_protected(client)


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
