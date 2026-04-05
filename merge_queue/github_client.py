"""Thin GitHub API wrapper using requests.

Two tokens are used:
- GITHUB_TOKEN: for most operations (PRs, labels, comments, branches, CI dispatch)
- MQ_ADMIN_TOKEN: for ruleset operations (requires Administration permission)

Rate limit tracking: every response updates the rate limit counters.
Call counting: every request increments a counter for auditing.
Caching: read-only endpoints are cached within a single process run.
"""

from __future__ import annotations

import datetime
import logging
import os
import time
from typing import Any, Protocol

import requests

log = logging.getLogger(__name__)

DEFAULT_CI_TIMEOUT = 30 * 60
DEFAULT_CI_POLL_INTERVAL = 15


class RateLimitInfo:
    """Tracks GitHub API rate limit state from response headers."""

    def __init__(self) -> None:
        self.limit: int = 0
        self.remaining: int = 0
        self.used: int = 0
        self.reset_at: datetime.datetime | None = None
        self.request_count: int = 0

    def update(self, response: requests.Response) -> None:
        self.request_count += 1
        headers = response.headers
        if "X-RateLimit-Limit" in headers:
            self.limit = int(headers["X-RateLimit-Limit"])
        if "X-RateLimit-Remaining" in headers:
            self.remaining = int(headers["X-RateLimit-Remaining"])
        if "X-RateLimit-Used" in headers:
            self.used = int(headers["X-RateLimit-Used"])
        if "X-RateLimit-Reset" in headers:
            self.reset_at = datetime.datetime.fromtimestamp(
                int(headers["X-RateLimit-Reset"]), tz=datetime.timezone.utc
            )

        if self.remaining > 0 and self.remaining <= 100:
            log.warning(
                "GitHub API rate limit low: %d/%d remaining (resets %s)",
                self.remaining,
                self.limit,
                self.reset_at,
            )

    def summary(self) -> str:
        return (
            f"requests={self.request_count}, "
            f"remaining={self.remaining}/{self.limit}, "
            f"resets={self.reset_at}"
        )


class GitHubClientProtocol(Protocol):
    """Protocol for testing — mock this instead of the concrete class."""

    def list_open_prs(self) -> list[dict[str, Any]]: ...
    def get_label_timestamp(
        self, pr_number: int, label: str
    ) -> datetime.datetime | None: ...
    def add_label(self, pr_number: int, label: str) -> None: ...
    def remove_label(self, pr_number: int, label: str) -> None: ...
    def create_comment(self, pr_number: int, body: str) -> int: ...
    def update_comment(self, comment_id: int, body: str) -> None: ...
    def get_failed_job_info(self, run_url: str) -> tuple[str, str]: ...
    def create_ruleset(self, name: str, branch_patterns: list[str]) -> int: ...
    def get_ruleset(self, ruleset_id: int) -> dict[str, Any]: ...
    def delete_ruleset(self, ruleset_id: int) -> None: ...
    def list_rulesets(self) -> list[dict[str, Any]]: ...
    def create_protection_ruleset(self, name: str, branch: str) -> int: ...
    def list_mq_branches(self) -> list[str]: ...
    def delete_branch(self, ref: str) -> None: ...
    def get_branch_sha(self, branch: str) -> str: ...
    def get_default_branch(self) -> str: ...
    def dispatch_ci(self, branch: str) -> None: ...
    def poll_ci(self, branch: str, timeout_seconds: int) -> bool: ...
    def poll_ci_with_url(
        self, branch: str, timeout_seconds: int
    ) -> tuple[bool, str]: ...
    def update_ref(self, ref: str, sha: str) -> None: ...
    def update_pr_base(self, pr_number: int, base: str) -> None: ...
    def compare_commits(self, base: str, head: str) -> str: ...
    def get_pr(self, pr_number: int) -> dict[str, Any]: ...
    def get_file_content(self, path: str, ref: str) -> dict[str, Any]: ...
    def put_file_content(
        self,
        path: str,
        branch: str,
        content_b64: str,
        message: str,
        sha: str | None = None,
    ) -> dict[str, Any]: ...
    def create_orphan_branch(self, branch: str, files: dict[str, str]) -> None: ...
    def get_pr_ci_status(self, pr_number: int) -> tuple[bool | None, str]: ...
    def dispatch_ci_on_ref(self, ref: str) -> None: ...
    def create_commit_status(
        self,
        sha: str,
        state: str,
        description: str = "",
        context: str = "Final Results",
    ) -> None: ...
    def get_user_permission(self, username: str) -> str: ...
    def create_deployment(self, description: str, ref: str = "main") -> int: ...
    def update_deployment_status(
        self, deployment_id: int, state: str, description: str = ""
    ) -> None: ...
    @property
    def rate_limit(self) -> RateLimitInfo: ...


class GitHubClient:
    """Concrete GitHub API client with rate limit tracking and caching."""

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
        self._session.headers.update(
            {
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self._token}",
                "X-GitHub-Api-Version": "2022-11-28",
            }
        )

        self._admin_session = requests.Session()
        admin_tok = self._admin_token or self._token
        self._admin_session.headers.update(
            {
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {admin_tok}",
                "X-GitHub-Api-Version": "2022-11-28",
            }
        )

        self._ci_workflow = os.environ.get("MQ_CI_WORKFLOW", "ci.yml")

        self.rate_limit = RateLimitInfo()

        # Per-run caches for read-only data
        self._cache_open_prs: list[dict[str, Any]] | None = None
        self._cache_default_branch: str | None = None
        self._cache_mq_branches: list[str] | None = None
        self._cache_rulesets: list[dict[str, Any]] | None = None
        self._cache_label_timestamps: dict[
            tuple[int, str], datetime.datetime | None
        ] = {}

    def invalidate_cache(self) -> None:
        """Clear all caches. Call after write operations that change state."""
        self._cache_open_prs = None
        self._cache_mq_branches = None
        self._cache_rulesets = None
        # label timestamps and default branch don't change within a run

    def _track(self, response: requests.Response) -> None:
        self.rate_limit.update(response)

    def _get(self, path: str, **kwargs: Any) -> Any:
        r = self._session.get(f"{self._base_url}{path}", **kwargs)
        self._track(r)
        r.raise_for_status()
        return r.json()

    def _post(self, path: str, json: Any = None, **kwargs: Any) -> Any:
        r = self._session.post(f"{self._base_url}{path}", json=json, **kwargs)
        self._track(r)
        r.raise_for_status()
        return r.json() if r.content else None

    def _put(self, path: str, json: Any = None, **kwargs: Any) -> Any:
        r = self._session.put(f"{self._base_url}{path}", json=json, **kwargs)
        self._track(r)
        r.raise_for_status()
        return r.json() if r.content else None

    def _patch(self, path: str, json: Any = None, **kwargs: Any) -> Any:
        r = self._session.patch(f"{self._base_url}{path}", json=json, **kwargs)
        self._track(r)
        r.raise_for_status()
        return r.json() if r.content else None

    def _delete(self, path: str, **kwargs: Any) -> None:
        r = self._session.delete(f"{self._base_url}{path}", **kwargs)
        self._track(r)
        r.raise_for_status()

    # --- PRs ---

    def list_open_prs(self) -> list[dict[str, Any]]:
        if self._cache_open_prs is not None:
            return self._cache_open_prs
        self._cache_open_prs = self._get("/pulls?state=open&per_page=100")
        return self._cache_open_prs

    def get_pr(self, pr_number: int) -> dict[str, Any]:
        return self._get(f"/pulls/{pr_number}")

    def get_label_timestamp(
        self, pr_number: int, label: str
    ) -> datetime.datetime | None:
        cache_key = (pr_number, label)
        if cache_key in self._cache_label_timestamps:
            return self._cache_label_timestamps[cache_key]

        events = self._get(
            f"/issues/{pr_number}/timeline",
            headers={"Accept": "application/vnd.github.mockingbird-preview+json"},
            params={"per_page": 100},
        )
        result = None
        for event in events:
            if (
                event.get("event") == "labeled"
                and event.get("label", {}).get("name") == label
            ):
                ts = event.get("created_at")
                if ts:
                    result = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    break
        self._cache_label_timestamps[cache_key] = result
        return result

    def update_pr_base(self, pr_number: int, base: str) -> None:
        self._patch(f"/pulls/{pr_number}", json={"base": base})

    # --- Labels ---

    def add_label(self, pr_number: int, label: str) -> None:
        self._post(f"/issues/{pr_number}/labels", json={"labels": [label]})
        self.invalidate_cache()  # labels changed

    def remove_label(self, pr_number: int, label: str) -> None:
        try:
            self._delete(f"/issues/{pr_number}/labels/{label}")
            self.invalidate_cache()
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                pass
            else:
                raise

    # --- Comments ---

    def create_comment(self, pr_number: int, body: str) -> int:
        """Create a comment on a PR. Returns the comment ID."""
        data = self._post(f"/issues/{pr_number}/comments", json={"body": body})
        return data["id"]

    def update_comment(self, comment_id: int, body: str) -> None:
        """Update an existing comment by ID."""
        self._patch(f"/issues/comments/{comment_id}", json={"body": body})

    def get_failed_job_info(self, run_url: str) -> tuple[str, str]:
        """Extract the failed job name and error snippet from a CI run.

        Returns (job_name, error_snippet). Best effort — returns ("", "") on failure.
        """
        try:
            # Extract run_id from URL like .../actions/runs/12345
            run_id = run_url.rstrip("/").split("/")[-1]
            jobs_data = self._get(f"/actions/runs/{run_id}/jobs")
            for job in jobs_data.get("jobs", []):
                if job.get("conclusion") == "failure":
                    job_name = job.get("name", "unknown")
                    # Get the failed step
                    for step in job.get("steps", []):
                        if step.get("conclusion") == "failure":
                            step_name = step.get("name", "")
                            return job_name, f"Failed at step: {step_name}"
                    return job_name, ""
        except Exception as e:
            log.warning("Could not fetch failed job info: %s", e)
        return "", ""

    # --- Rulesets (uses admin token) ---

    def create_ruleset(self, name: str, branch_patterns: list[str]) -> int:
        r = self._admin_session.post(
            f"{self._base_url}/rulesets",
            json={
                "name": name,
                "target": "branch",
                "enforcement": "active",
                "conditions": {"ref_name": {"include": branch_patterns, "exclude": []}},
                "rules": [{"type": "update"}],
            },
        )
        self._track(r)
        r.raise_for_status()
        self._cache_rulesets = None
        return r.json()["id"]

    def create_protection_ruleset(self, name: str, branch: str) -> int:
        """Create a branch protection ruleset requiring PRs + CI status check.

        The ruleset:
        - Requires pull requests (no direct push)
        - Requires "Final Results" status check
        - Admin role can bypass (for MQ fast-forward)
        """
        r = self._admin_session.post(
            f"{self._base_url}/rulesets",
            json={
                "name": name,
                "target": "branch",
                "enforcement": "active",
                "conditions": {
                    "ref_name": {
                        "include": [f"refs/heads/{branch}"],
                        "exclude": [],
                    }
                },
                "rules": [
                    {
                        "type": "pull_request",
                        "parameters": {
                            "required_approving_review_count": 0,
                            "dismiss_stale_reviews_on_push": False,
                            "require_last_push_approval": False,
                        },
                    },
                    {
                        "type": "required_status_checks",
                        "parameters": {
                            "strict_status_check_policy": False,
                            "required_status_checks": [{"context": "Final Results"}],
                        },
                    },
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
        self._track(r)
        r.raise_for_status()
        self._cache_rulesets = None
        return r.json()["id"]

    def get_ruleset(self, ruleset_id: int) -> dict[str, Any]:
        r = self._admin_session.get(f"{self._base_url}/rulesets/{ruleset_id}")
        self._track(r)
        r.raise_for_status()
        return r.json()

    def delete_ruleset(self, ruleset_id: int) -> None:
        r = self._admin_session.delete(f"{self._base_url}/rulesets/{ruleset_id}")
        self._track(r)
        r.raise_for_status()
        self._cache_rulesets = None

    def list_rulesets(self) -> list[dict[str, Any]]:
        if self._cache_rulesets is not None:
            return self._cache_rulesets
        r = self._admin_session.get(
            f"{self._base_url}/rulesets", params={"per_page": 100}
        )
        self._track(r)
        r.raise_for_status()
        self._cache_rulesets = r.json()
        return self._cache_rulesets

    # --- Branches ---

    def list_mq_branches(self) -> list[str]:
        """List mq/* batch branches (excludes mq/state)."""
        if self._cache_mq_branches is not None:
            return self._cache_mq_branches
        refs = self._get("/git/matching-refs/heads/mq/")
        self._cache_mq_branches = [
            r["ref"].removeprefix("refs/heads/")
            for r in refs
            if r["ref"] != "refs/heads/mq/state"
        ]
        return self._cache_mq_branches

    def get_branch_sha(self, branch: str) -> str:
        data = self._get(f"/git/ref/heads/{branch}")
        return data["object"]["sha"]

    def delete_branch(self, ref: str) -> None:
        try:
            self._delete(f"/git/refs/heads/{ref}")
            self._cache_mq_branches = None
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 422:
                pass
            else:
                raise

    def update_ref(self, ref: str, sha: str) -> None:
        self._patch(f"/git/refs/heads/{ref}", json={"sha": sha, "force": False})

    # --- CI ---

    def get_default_branch(self) -> str:
        if self._cache_default_branch is not None:
            return self._cache_default_branch
        r = self._session.get(f"{self._base_url}")
        self._track(r)
        self._cache_default_branch = r.json().get("default_branch", "main")
        return self._cache_default_branch

    def dispatch_ci(self, branch: str) -> None:
        self._post(
            f"/actions/workflows/{self._ci_workflow}/dispatches",
            json={"ref": branch, "inputs": {"ref": branch}},
        )
        log.info("Dispatched CI on %s", branch)

    def poll_ci(
        self,
        branch: str,
        timeout_seconds: int = DEFAULT_CI_TIMEOUT,
    ) -> bool:
        passed, _ = self.poll_ci_with_url(branch, timeout_seconds)
        return passed

    def poll_ci_with_url(
        self,
        branch: str,
        timeout_seconds: int = DEFAULT_CI_TIMEOUT,
    ) -> tuple[bool, str]:
        """Poll for CI completion. Returns (passed, run_html_url)."""
        time.sleep(5)

        run_id = None
        run_url = ""
        for attempt in range(10):
            data = self._get(
                f"/actions/workflows/{self._ci_workflow}/runs",
                params={
                    "branch": branch,
                    "event": "workflow_dispatch",
                    "per_page": 5,
                },
            )
            runs = data.get("workflow_runs", [])
            if runs:
                run_id = runs[0]["id"]
                run_url = runs[0].get("html_url", "")
                break
            log.info("Waiting for CI run to appear (attempt %d)...", attempt + 1)
            time.sleep(5)

        if run_id is None:
            log.error("CI run did not appear after dispatch")
            return False, ""

        log.info("Found CI run %d, polling...", run_id)
        start = time.monotonic()
        while time.monotonic() - start < timeout_seconds:
            data = self._get(f"/actions/runs/{run_id}")
            run_url = data.get("html_url", run_url)
            if data["status"] == "completed":
                conclusion = data["conclusion"]
                log.info("CI completed: %s", conclusion)
                return conclusion == "success", run_url
            log.info("CI status: %s...", data["status"])
            time.sleep(DEFAULT_CI_POLL_INTERVAL)

        log.error("CI timed out after %d seconds", timeout_seconds)
        return False, run_url

    # --- Compare ---

    def compare_commits(self, base: str, head: str) -> str:
        data = self._get(f"/compare/{base}...{head}")
        return data["status"]

    # --- Contents API (for state branch) ---

    def get_file_content(self, path: str, ref: str) -> dict[str, Any]:
        """Get file content from a specific branch. Returns {sha, content(base64)}."""
        return self._get(f"/contents/{path}", params={"ref": ref})

    def put_file_content(
        self,
        path: str,
        branch: str,
        content_b64: str,
        message: str,
        sha: str | None = None,
    ) -> dict[str, Any]:
        """Create or update a file. Returns response with content.sha."""
        body: dict[str, Any] = {
            "message": message,
            "content": content_b64,
            "branch": branch,
        }
        if sha is not None:
            body["sha"] = sha
        return self._put(f"/contents/{path}", json=body)

    def create_orphan_branch(self, branch: str, files: dict[str, str]) -> None:
        """Create an orphan branch with the given files.

        Uses the Git Data API: create blobs, tree, commit, then ref.
        """
        # Create blobs for each file
        tree_items = []
        for path, content in files.items():
            blob = self._post(
                "/git/blobs",
                json={
                    "content": content,
                    "encoding": "utf-8",
                },
            )
            tree_items.append(
                {
                    "path": path,
                    "mode": "100644",
                    "type": "blob",
                    "sha": blob["sha"],
                }
            )

        # Create tree (no base_tree = orphan)
        tree = self._post("/git/trees", json={"tree": tree_items})

        # Create commit (no parents = orphan)
        commit = self._post(
            "/git/commits",
            json={
                "message": f"Initialize {branch}",
                "tree": tree["sha"],
                "parents": [],
            },
        )

        # Create ref
        self._post(
            "/git/refs",
            json={
                "ref": f"refs/heads/{branch}",
                "sha": commit["sha"],
            },
        )
        log.info("Created orphan branch %s", branch)

    # --- PR CI Status ---

    def get_pr_ci_status(self, pr_number: int) -> tuple[bool | None, str]:
        """Check if a PR's CI has passed.

        Returns (passed, details_url) where:
          True  = CI passed
          False = CI failed
          None  = CI pending/not started (no completed check run found)
        """
        pr = self.get_pr(pr_number)
        sha = pr["head"]["sha"]
        data = self._get(
            f"/commits/{sha}/check-runs",
            params={"check_name": "Final Results"},
        )
        runs = data.get("check_runs", [])
        if not runs:
            return None, ""  # No check runs yet — CI pending
        run = runs[0]
        conclusion = run.get("conclusion")
        if conclusion is None:
            return None, run.get("html_url", "")  # Still running
        passed = conclusion == "success"
        url = run.get("html_url", "")
        return passed, url

    def dispatch_ci_on_ref(self, ref: str) -> None:
        """Dispatch CI on an arbitrary git ref (branch name)."""
        self._post(
            f"/actions/workflows/{self._ci_workflow}/dispatches",
            json={"ref": ref, "inputs": {"ref": ref}},
        )

    # --- Commit Status API ---

    def create_commit_status(
        self,
        sha: str,
        state: str,
        description: str = "",
        context: str = "Final Results",
    ) -> None:
        """Set a commit status on a SHA. state: success, failure, pending, error."""
        self._post(
            f"/statuses/{sha}",
            json={
                "state": state,
                "description": description[:140],
                "context": context,
            },
        )

    # --- Collaborator permissions ---

    def get_user_permission(self, username: str) -> str:
        """Get a user's permission level.

        Returns 'admin', 'maintain', 'write', 'read', or 'none'.
        """
        data = self._get(f"/collaborators/{username}/permission")
        return data.get("permission", "none")

    # --- Deployments API (for live UI) ---

    def create_deployment(self, description: str, ref: str = "main") -> int:
        """Create a deployment in the merge-queue environment. Returns deployment ID."""
        data = self._post(
            "/deployments",
            json={
                "ref": ref,
                "environment": "merge-queue",
                "description": description,
                "auto_merge": False,
                "required_contexts": [],
            },
        )
        return data["id"]

    def update_deployment_status(
        self,
        deployment_id: int,
        state: str,
        description: str = "",
        log_url: str = "",
    ) -> None:
        """Update deployment status. state: queued, in_progress, success, failure, inactive."""
        body: dict[str, Any] = {
            "state": state,
            "description": description[:140],
        }
        if log_url:
            body["log_url"] = log_url
        self._post(f"/deployments/{deployment_id}/statuses", json=body)
