"""Persistent state store on the mq/state branch.

Reads/writes state.json via GitHub Contents API with optimistic concurrency.
"""

from __future__ import annotations

import base64
import json
import logging

from merge_queue.github_client import GitHubClientProtocol
from merge_queue.status import render_status_md
from merge_queue.types import empty_state

log = logging.getLogger(__name__)

STATE_BRANCH = "mq/state"
STATE_PATH = "state.json"
STATUS_PATH = "STATUS.md"


class ConflictError(Exception):
    """State was modified by another process."""

    pass


class StateStore:
    """Read/write queue state on the mq/state branch."""

    def __init__(self, client: GitHubClientProtocol):
        self.client = client
        self._state_sha: str | None = None
        self._status_sha: str | None = None

    def read(self) -> dict:
        """Read state.json from mq/state branch.

        Returns empty state if branch or file doesn't exist.
        Caches the file SHA for optimistic concurrency on write.
        """
        try:
            data = self.client.get_file_content(STATE_PATH, STATE_BRANCH)
            self._state_sha = data["sha"]
            content = base64.b64decode(data["content"]).decode()
            return json.loads(content)
        except Exception as e:
            if "404" in str(e) or "Not Found" in str(e):
                log.info("State file not found, returning empty state")
                self._state_sha = None
                return empty_state()
            raise

    def write(self, state: dict) -> None:
        """Write state.json and STATUS.md to the mq/state branch.

        Uses file SHA for optimistic concurrency. Raises ConflictError if
        the file was modified since our last read.
        """

        self._ensure_branch()

        # Write state.json
        content_b64 = base64.b64encode(json.dumps(state, indent=2).encode()).decode()

        try:
            result = self.client.put_file_content(
                STATE_PATH,
                STATE_BRANCH,
                content_b64,
                message="Update merge queue state",
                sha=self._state_sha,
            )
            self._state_sha = result["content"]["sha"]
        except Exception as e:
            if "409" in str(e) or "conflict" in str(e).lower():
                raise ConflictError(f"State file was modified concurrently: {e}") from e
            raise

        # Write STATUS.md (best-effort, don't fail if this fails)
        try:
            status_md = render_status_md(state, self.client)
            status_b64 = base64.b64encode(status_md.encode()).decode()
            # Always fetch current SHA to avoid stale-SHA 422 errors
            if self._status_sha is None:
                try:
                    data = self.client.get_file_content(STATUS_PATH, STATE_BRANCH)
                    self._status_sha = data["sha"]
                except Exception:
                    pass  # File may not exist yet
            result = self.client.put_file_content(
                STATUS_PATH,
                STATE_BRANCH,
                status_b64,
                message="Update merge queue status",
                sha=self._status_sha,
            )
            self._status_sha = result["content"]["sha"]
        except Exception as e:
            if "422" in str(e) or "409" in str(e) or "conflict" in str(e).lower():
                # SHA mismatch — re-read and retry once
                try:
                    data = self.client.get_file_content(STATUS_PATH, STATE_BRANCH)
                    self._status_sha = data["sha"]
                    result = self.client.put_file_content(
                        STATUS_PATH,
                        STATE_BRANCH,
                        status_b64,
                        message="Update merge queue status",
                        sha=self._status_sha,
                    )
                    self._status_sha = result["content"]["sha"]
                except Exception:
                    log.warning("Could not update STATUS.md: %s", e)
            elif "404" not in str(e):
                log.warning("Could not update STATUS.md: %s", e)

    def _ensure_branch(self) -> None:
        """Create the mq/state orphan branch if it doesn't exist."""
        try:
            self.client.get_file_content(STATE_PATH, STATE_BRANCH)
            return  # Branch exists
        except Exception:
            pass

        log.info("Creating mq/state branch with initial state")
        try:
            self.client.create_orphan_branch(
                STATE_BRANCH,
                {
                    STATE_PATH: json.dumps(empty_state(), indent=2),
                    STATUS_PATH: "# Merge Queue Status\n\n_No activity yet._\n",
                },
            )
        except Exception as e:
            if "422" in str(e) or "already exists" in str(e).lower():
                return  # Race condition, branch was created by another run
            raise
