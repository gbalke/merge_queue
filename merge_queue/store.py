"""Persistent state store on the mq/state branch.

Reads/writes state.json via GitHub Contents API with optimistic concurrency.
"""

from __future__ import annotations

import base64
import json
import logging

from merge_queue.github_client import GitHubClientProtocol
from merge_queue.status import render_branch_status_md, render_root_status_md
from merge_queue.types import empty_state

log = logging.getLogger(__name__)

STATE_BRANCH = "mq/state"
STATE_PATH = "state.json"
ROOT_STATUS_PATH = "STATUS.md"


class ConflictError(Exception):
    """State was modified by another process."""

    pass


def _migrate_v1_to_v2(state: dict, default_branch: str = "main") -> dict:
    """Migrate a v1 state dict to v2 per-branch schema."""
    return {
        "version": 2,
        "updated_at": state.get("updated_at", ""),
        "branches": {
            default_branch: {
                "queue": state.get("queue", []),
                "active_batch": state.get("active_batch"),
            }
        },
        "history": state.get("history", []),
    }


class StateStore:
    """Read/write queue state on the mq/state branch."""

    def __init__(self, client: GitHubClientProtocol):
        self.client = client
        self._state_sha: str | None = None
        self._status_shas: dict[str, str | None] = {}

    def read(self) -> dict:
        """Read state.json from mq/state branch.

        Returns empty state if branch or file doesn't exist.
        Auto-migrates v1 state to v2 per-branch schema.
        Caches the file SHA for optimistic concurrency on write.
        """
        try:
            data = self.client.get_file_content(STATE_PATH, STATE_BRANCH)
            self._state_sha = data["sha"]
            content = base64.b64decode(data["content"]).decode()
            state = json.loads(content)
            if state.get("version", 1) < 2:
                default_branch = "main"
                try:
                    default_branch = self.client.get_default_branch()
                except Exception:
                    pass
                state = _migrate_v1_to_v2(state, default_branch)
            return state
        except Exception as e:
            if "404" in str(e) or "Not Found" in str(e):
                log.info("State file not found, returning empty state")
                self._state_sha = None
                return empty_state()
            raise

    def write(self, state: dict, max_retries: int = 3) -> None:
        """Write state.json and per-branch STATUS.md files to the mq/state branch.

        Auto-retries on conflict (409) by re-reading the current SHA.
        """
        self._ensure_branch()

        content_b64 = base64.b64encode(json.dumps(state, indent=2).encode()).decode()

        for attempt in range(1, max_retries + 1):
            try:
                result = self.client.put_file_content(
                    STATE_PATH,
                    STATE_BRANCH,
                    content_b64,
                    message="Update merge queue state",
                    sha=self._state_sha,
                )
                self._state_sha = result["content"]["sha"]
                break
            except Exception as e:
                if (
                    "409" in str(e) or "conflict" in str(e).lower()
                ) and attempt < max_retries:
                    log.warning(
                        "State write conflict (attempt %d), retrying...", attempt
                    )
                    try:
                        data = self.client.get_file_content(STATE_PATH, STATE_BRANCH)
                        self._state_sha = data["sha"]
                    except Exception:
                        pass
                    content_b64 = base64.b64encode(
                        json.dumps(state, indent=2).encode()
                    ).decode()
                    continue
                if "409" in str(e) or "conflict" in str(e).lower():
                    raise ConflictError(
                        f"State write failed after {max_retries} attempts: {e}"
                    ) from e
                raise

        # Write per-branch STATUS.md files (best-effort)
        for branch_name, branch_state in state.get("branches", {}).items():
            self._write_status_file(
                _branch_status_path(branch_name),
                render_branch_status_md(branch_name, branch_state, self.client),
                f"Update merge queue status for {branch_name}",
            )

        # Write root STATUS.md (best-effort)
        self._write_status_file(
            ROOT_STATUS_PATH,
            render_root_status_md(state, self.client),
            "Update merge queue root status",
        )

    def _write_status_file(self, path: str, content: str, message: str) -> None:
        """Write a status markdown file best-effort, retrying once on SHA conflict."""
        content_b64 = base64.b64encode(content.encode()).decode()
        sha = self._status_shas.get(path)
        if sha is None:
            try:
                data = self.client.get_file_content(path, STATE_BRANCH)
                sha = data["sha"]
                self._status_shas[path] = sha
            except Exception:
                pass
        try:
            result = self.client.put_file_content(
                path, STATE_BRANCH, content_b64, message=message, sha=sha
            )
            self._status_shas[path] = result["content"]["sha"]
        except Exception as e:
            if "422" in str(e) or "409" in str(e) or "conflict" in str(e).lower():
                try:
                    data = self.client.get_file_content(path, STATE_BRANCH)
                    sha = data["sha"]
                    self._status_shas[path] = sha
                    result = self.client.put_file_content(
                        path, STATE_BRANCH, content_b64, message=message, sha=sha
                    )
                    self._status_shas[path] = result["content"]["sha"]
                except Exception:
                    log.warning("Could not update %s: %s", path, e)
            elif "404" not in str(e):
                log.warning("Could not update %s: %s", path, e)

    def _ensure_branch(self) -> None:
        """Create the mq/state orphan branch if it doesn't exist."""
        try:
            self.client.get_file_content(STATE_PATH, STATE_BRANCH)
            return
        except Exception:
            pass

        log.info("Creating mq/state branch with initial state")
        try:
            self.client.create_orphan_branch(
                STATE_BRANCH,
                {
                    STATE_PATH: json.dumps(empty_state(), indent=2),
                    ROOT_STATUS_PATH: "# Merge Queue Status\n\n_No activity yet._\n",
                },
            )
        except Exception as e:
            if "422" in str(e) or "already exists" in str(e).lower():
                return
            raise


def _branch_status_path(branch_name: str) -> str:
    """Return the per-branch status file path within the mq/state tree."""
    return f"{branch_name}/STATUS.md"
