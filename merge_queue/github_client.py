"""Thin GitHub API wrapper using requests.

Two tokens are used:
- GITHUB_TOKEN: for most operations (PRs, labels, comments, branches, CI dispatch)
- MQ_ADMIN_TOKEN: for ruleset operations (requires Administration permission)
"""

from __future__ import annotations

import datetime
import logging
import os
import time
from typing import Any, Protocol

import requests

log = logging.getLogger(__name__)

# How long to wait for CI before giving up (seconds)
DEFAULT_CI_TIMEOUT = 30 * 60
DEFAULT_CI_POLL_INTERVAL = 15


class GitHubClientProtocol(Protocol):
    """Protocol for testing — mock this instead of the concrete class."""

    def list_open_prs(self) -> list[dict[str, Any]]: ...
    def get_label_timestamp(self, pr_number: int, label: str) -> datetime.datetime | None: ...
    def add_label(self, pr_number: int, label: str) -> None: ...
    def remove_label(self, pr_number: int, label: str) -> None: ...
    def create_comment(self, pr_number: int, body: str) -> None: ...
    def create_ruleset(self, name: str, branch_patterns: list[str]) -> int: ...
    def delete_ruleset(self, ruleset_id: int) -> None: ...
    def list_rulesets(self) -> list[dict[str, Any]]: ...
    def list_mq_branches(self) -> list[str]: ...
    def delete_branch(self, ref: str) -> None: ...
    def get_branch_sha(self, branch: str) -> str: ...
    def get_default_branch(self) -> str: ...
    def dispatch_ci(self, branch: str) -> None: ...
    def poll_ci(self, branch: str, timeout_seconds: int) -> bool: ...
    def update_ref(self, ref: str, sha: str) -> None: ...
    def update_pr_base(self, pr_number: int, base: str) -> None: ...
    def compare_commits(self, base: str, head: str) -> str: ...
    def get_pr(self, pr_number: int) -> dict[str, Any]: ...


class GitHubClient:
    """Concrete GitHub API client."""

    def __init__(
        self,
        owner: str,
        repo: str,
        token: str | None = None,
        admin_token: str | None = None,
    ):
        self.owner = owner
        self.repo = repo
        self._token = token or os.environ.get("GITHUB_TOKEN", "")
        self._admin_token = admin_token or os.environ.get("MQ_ADMIN_TOKEN", "")
        self._base_url = f"https://api.github.com/repos/{owner}/{repo}"

        self._session = requests.Session()
        self._session.headers.update({
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self._token}",
            "X-GitHub-Api-Version": "2022-11-28",
        })

        self._admin_session = requests.Session()
        admin_tok = self._admin_token or self._token
        self._admin_session.headers.update({
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {admin_tok}",
            "X-GitHub-Api-Version": "2022-11-28",
        })

    def _get(self, path: str, **kwargs: Any) -> Any:
        r = self._session.get(f"{self._base_url}{path}", **kwargs)
        r.raise_for_status()
        return r.json()

    def _post(self, path: str, json: Any = None, **kwargs: Any) -> Any:
        r = self._session.post(f"{self._base_url}{path}", json=json, **kwargs)
        r.raise_for_status()
        return r.json() if r.content else None

    def _put(self, path: str, json: Any = None, **kwargs: Any) -> Any:
        r = self._session.put(f"{self._base_url}{path}", json=json, **kwargs)
        r.raise_for_status()
        return r.json() if r.content else None

    def _patch(self, path: str, json: Any = None, **kwargs: Any) -> Any:
        r = self._session.patch(f"{self._base_url}{path}", json=json, **kwargs)
        r.raise_for_status()
        return r.json() if r.content else None

    def _delete(self, path: str, **kwargs: Any) -> None:
        r = self._session.delete(f"{self._base_url}{path}", **kwargs)
        r.raise_for_status()

    # --- PRs ---

    def list_open_prs(self) -> list[dict[str, Any]]:
        return self._get("/pulls?state=open&per_page=100")

    def get_pr(self, pr_number: int) -> dict[str, Any]:
        return self._get(f"/pulls/{pr_number}")

    def get_label_timestamp(
        self, pr_number: int, label: str
    ) -> datetime.datetime | None:
        """Get when a label was added to a PR using the timeline API."""
        events = self._get(
            f"/issues/{pr_number}/timeline",
            headers={"Accept": "application/vnd.github.mockingbird-preview+json"},
            params={"per_page": 100},
        )
        for event in events:
            if (
                event.get("event") == "labeled"
                and event.get("label", {}).get("name") == label
            ):
                ts = event.get("created_at")
                if ts:
                    return datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return None

    def update_pr_base(self, pr_number: int, base: str) -> None:
        self._patch(f"/pulls/{pr_number}", json={"base": base})

    # --- Labels ---

    def add_label(self, pr_number: int, label: str) -> None:
        self._post(f"/issues/{pr_number}/labels", json={"labels": [label]})

    def remove_label(self, pr_number: int, label: str) -> None:
        try:
            self._delete(f"/issues/{pr_number}/labels/{label}")
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                pass  # Label wasn't there
            else:
                raise

    # --- Comments ---

    def create_comment(self, pr_number: int, body: str) -> None:
        self._post(f"/issues/{pr_number}/comments", json={"body": body})

    # --- Rulesets (uses admin token) ---

    def create_ruleset(self, name: str, branch_patterns: list[str]) -> int:
        r = self._admin_session.post(
            f"{self._base_url}/rulesets",
            json={
                "name": name,
                "target": "branch",
                "enforcement": "active",
                "conditions": {
                    "ref_name": {"include": branch_patterns, "exclude": []}
                },
                "rules": [{"type": "update"}],
            },
        )
        r.raise_for_status()
        return r.json()["id"]

    def delete_ruleset(self, ruleset_id: int) -> None:
        r = self._admin_session.delete(f"{self._base_url}/rulesets/{ruleset_id}")
        r.raise_for_status()

    def list_rulesets(self) -> list[dict[str, Any]]:
        r = self._admin_session.get(
            f"{self._base_url}/rulesets", params={"per_page": 100}
        )
        r.raise_for_status()
        return r.json()

    # --- Branches ---

    def list_mq_branches(self) -> list[str]:
        refs = self._get("/git/matching-refs/heads/mq/")
        return [r["ref"].removeprefix("refs/heads/") for r in refs]

    def get_branch_sha(self, branch: str) -> str:
        data = self._get(f"/git/ref/heads/{branch}")
        return data["object"]["sha"]

    def delete_branch(self, ref: str) -> None:
        try:
            self._delete(f"/git/refs/heads/{ref}")
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 422:
                pass  # Branch already deleted
            else:
                raise

    def update_ref(self, ref: str, sha: str) -> None:
        self._patch(f"/git/refs/heads/{ref}", json={"sha": sha, "force": False})

    # --- CI ---

    def get_default_branch(self) -> str:
        data = self._session.get(f"{self._base_url}").json()
        return data.get("default_branch", "main")

    def dispatch_ci(self, branch: str) -> None:
        self._post(
            "/actions/workflows/ci.yml/dispatches",
            json={"ref": branch, "inputs": {"ref": branch}},
        )
        log.info("Dispatched CI on %s", branch)

    def poll_ci(
        self,
        branch: str,
        timeout_seconds: int = DEFAULT_CI_TIMEOUT,
    ) -> bool:
        """Poll for CI completion on a branch. Returns True if passed."""
        time.sleep(5)  # Wait for run to appear

        run_id = None
        for attempt in range(10):
            data = self._get(
                "/actions/workflows/ci.yml/runs",
                params={
                    "branch": branch,
                    "event": "workflow_dispatch",
                    "per_page": 5,
                },
            )
            runs = data.get("workflow_runs", [])
            if runs:
                run_id = runs[0]["id"]
                break
            log.info("Waiting for CI run to appear (attempt %d)...", attempt + 1)
            time.sleep(5)

        if run_id is None:
            log.error("CI run did not appear after dispatch")
            return False

        log.info("Found CI run %d, polling...", run_id)
        start = time.monotonic()
        while time.monotonic() - start < timeout_seconds:
            data = self._get(f"/actions/runs/{run_id}")
            if data["status"] == "completed":
                conclusion = data["conclusion"]
                log.info("CI completed: %s", conclusion)
                return conclusion == "success"
            log.info("CI status: %s...", data["status"])
            time.sleep(DEFAULT_CI_POLL_INTERVAL)

        log.error("CI timed out after %d seconds", timeout_seconds)
        return False

    # --- Compare ---

    def compare_commits(self, base: str, head: str) -> str:
        """Compare two refs. Returns 'ahead', 'behind', 'identical', or 'diverged'."""
        data = self._get(f"/compare/{base}...{head}")
        return data["status"]
