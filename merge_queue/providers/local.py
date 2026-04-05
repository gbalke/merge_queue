"""Local git provider for integration testing without GitHub API calls.

Implements the same interface as GitHubClientProtocol but operates on a local
bare git repo. Git operations use real subprocess calls; PR metadata, labels,
comments, rulesets, and deployments are stored in-memory.
"""

from __future__ import annotations

import base64
import datetime
import logging
import subprocess
import tempfile
from typing import Any

from merge_queue.github_client import RateLimitInfo

log = logging.getLogger(__name__)


def _git(*args: str, cwd: str, check: bool = True) -> str:
    """Run a git command and return stdout."""
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    if check and result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


class LocalGitProvider:
    """Git provider backed by a local bare repo for integration testing."""

    def __init__(self, repo_path: str, ci_pass: bool = True) -> None:
        self.repo_path = repo_path
        self.work_dir = tempfile.mkdtemp(prefix="mq-local-")
        self._ci_pass = ci_pass
        self._labels: dict[int, set[str]] = {}
        self._label_timestamps: dict[tuple[int, str], datetime.datetime] = {}
        self._prs: dict[int, dict[str, Any]] = {}
        self._comments: dict[int, list[dict[str, Any]]] = {}
        self._comment_bodies: dict[int, str] = {}
        self._next_comment_id = 1
        self._rulesets: dict[int, dict[str, Any]] = {}
        self._next_ruleset_id = 1
        self._deployments: dict[int, dict[str, Any]] = {}
        self._next_deployment_id = 1
        self.rate_limit = RateLimitInfo()

        # Clone the bare repo into work_dir so batch.py git operations work.
        # batch.py calls run_git() which uses the process's cwd, so we configure
        # the clone as origin for push/fetch. We configure user identity to avoid
        # errors on systems without a global git config.
        subprocess.run(
            ["git", "clone", repo_path, self.work_dir],
            capture_output=True,
            check=True,
        )
        _git("config", "user.email", "mq@test.local", cwd=self.work_dir)
        _git("config", "user.name", "MQ Test", cwd=self.work_dir)
        # Also configure identity on the bare repo for commit-tree operations
        _git("config", "user.email", "mq@test.local", cwd=repo_path)
        _git("config", "user.name", "MQ Test", cwd=repo_path)

    def make_git_runner(self):  # type: ignore[return]
        """Return a GitRunner bound to this provider's work_dir.

        Pass the returned callable as the ``git`` parameter to
        ``batch.create_batch`` (or patch ``merge_queue.batch.run_git``) so
        that batch operations run inside the local working clone rather than
        the caller's process cwd.
        """
        work = self.work_dir

        def _runner(*args: str) -> str:
            result = subprocess.run(
                ["git", *args],
                cwd=work,
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                from merge_queue.batch import BatchError

                stderr = result.stderr.strip()
                cmd = " ".join(args)
                raise BatchError(
                    f"git {cmd} failed: {stderr or 'exit code ' + str(result.returncode)}"
                )
            return result.stdout

        return _runner

    # --- PR operations ---

    def create_pr(self, head_ref: str, base_ref: str, title: str) -> int:
        """Register a PR and ensure the head branch exists in the bare repo with a commit."""
        pr_number = len(self._prs) + 1

        # Create the branch with a real commit so git merge operations work.
        _ensure_branch_with_commit(
            self.repo_path,
            self.work_dir,
            head_ref,
            base_ref,
            f"feat: {title}",
        )

        sha = _git("rev-parse", f"origin/{head_ref}", cwd=self.work_dir)

        self._prs[pr_number] = {
            "number": pr_number,
            "title": title,
            "state": "open",
            "head": {"ref": head_ref, "sha": sha},
            "base": {"ref": base_ref},
            "labels": [],
        }
        self._labels[pr_number] = set()
        self._comments[pr_number] = []
        return pr_number

    def list_open_prs(self) -> list[dict[str, Any]]:
        return [pr for pr in self._prs.values() if pr["state"] == "open"]

    def get_pr(self, pr_number: int) -> dict[str, Any]:
        if pr_number not in self._prs:
            raise RuntimeError(f"PR #{pr_number} not found")
        pr = dict(self._prs[pr_number])
        pr["labels"] = [{"name": lbl} for lbl in self._labels.get(pr_number, set())]
        return pr

    def update_pr_base(self, pr_number: int, base: str) -> None:
        if pr_number in self._prs:
            self._prs[pr_number]["base"]["ref"] = base

    # --- Labels ---

    def get_label_timestamp(
        self, pr_number: int, label: str
    ) -> datetime.datetime | None:
        return self._label_timestamps.get((pr_number, label))

    def add_label(self, pr_number: int, label: str) -> None:
        self._labels.setdefault(pr_number, set()).add(label)
        self._label_timestamps[(pr_number, label)] = datetime.datetime.now(
            datetime.timezone.utc
        )
        if pr_number in self._prs:
            current = [
                lbl for lbl in self._prs[pr_number]["labels"] if lbl["name"] != label
            ]
            current.append({"name": label})
            self._prs[pr_number]["labels"] = current

    def remove_label(self, pr_number: int, label: str) -> None:
        self._labels.setdefault(pr_number, set()).discard(label)
        self._label_timestamps.pop((pr_number, label), None)
        if pr_number in self._prs:
            self._prs[pr_number]["labels"] = [
                lbl for lbl in self._prs[pr_number]["labels"] if lbl["name"] != label
            ]

    # --- Comments ---

    def create_comment(self, pr_number: int, body: str) -> int:
        cid = self._next_comment_id
        self._next_comment_id += 1
        self._comment_bodies[cid] = body
        self._comments.setdefault(pr_number, []).append({"id": cid, "body": body})
        return cid

    def update_comment(self, comment_id: int, body: str) -> None:
        self._comment_bodies[comment_id] = body

    # --- Git operations (real git on local repo) ---

    def get_branch_sha(self, branch: str) -> str:
        return _git("rev-parse", f"refs/heads/{branch}", cwd=self.repo_path)

    def list_mq_branches(self) -> list[str]:
        """List mq/* branches (excluding mq/state) from the bare repo."""
        output = _git(
            "for-each-ref",
            "--format=%(refname:short)",
            "refs/heads/mq/",
            cwd=self.repo_path,
        )
        return [line for line in output.splitlines() if line and line != "mq/state"]

    def delete_branch(self, ref: str) -> None:
        _git("branch", "-D", ref, cwd=self.repo_path, check=False)

    def update_ref(self, ref: str, sha: str) -> None:
        """Fast-forward ref to sha (no force — equivalent to GitHub's non-force update)."""
        _git("update-ref", f"refs/heads/{ref}", sha, cwd=self.repo_path)

    def compare_commits(self, base: str, head: str) -> str:
        """Return 'ahead', 'behind', 'identical', or 'diverged'."""
        base_sha = _git("rev-parse", f"refs/heads/{base}", cwd=self.repo_path)
        head_sha = _git("rev-parse", head, cwd=self.repo_path, check=False)
        if not head_sha:
            head_sha = head

        if base_sha == head_sha:
            return "identical"

        # Check if head is a descendant of base (i.e., head is ahead of base).
        result = subprocess.run(
            ["git", "merge-base", "--is-ancestor", base_sha, head_sha],
            cwd=self.repo_path,
            capture_output=True,
        )
        if result.returncode == 0:
            return "ahead"

        result = subprocess.run(
            ["git", "merge-base", "--is-ancestor", head_sha, base_sha],
            cwd=self.repo_path,
            capture_output=True,
        )
        if result.returncode == 0:
            return "behind"

        return "diverged"

    def get_default_branch(self) -> str:
        return "main"

    # --- Rulesets (in-memory, no enforcement) ---

    def create_ruleset(self, name: str, branch_patterns: list[str]) -> int:
        rid = self._next_ruleset_id
        self._next_ruleset_id += 1
        self._rulesets[rid] = {
            "id": rid,
            "name": name,
            "enforcement": "active",
            "conditions": {
                "ref_name": {
                    "include": list(branch_patterns),
                    "exclude": [],
                }
            },
        }
        return rid

    def get_ruleset(self, ruleset_id: int) -> dict[str, Any]:
        if ruleset_id not in self._rulesets:
            raise RuntimeError(f"404 Not Found: ruleset {ruleset_id}")
        return self._rulesets[ruleset_id]

    def delete_ruleset(self, ruleset_id: int) -> None:
        self._rulesets.pop(ruleset_id, None)

    def list_rulesets(self) -> list[dict[str, Any]]:
        return list(self._rulesets.values())

    # --- CI (mock — instant pass/fail) ---

    def dispatch_ci(self, branch: str) -> None:
        pass

    def poll_ci(self, branch: str, timeout_seconds: int = 1800) -> bool:
        return self._ci_pass

    def poll_ci_with_url(
        self, branch: str, timeout_seconds: int = 1800
    ) -> tuple[bool, str]:
        return self._ci_pass, ""

    # --- State branch (local files via git plumbing) ---

    def get_file_content(self, path: str, ref: str) -> dict[str, Any]:
        """Read a file from a branch using git show."""
        try:
            raw = _git("show", f"refs/heads/{ref}:{path}", cwd=self.repo_path)
        except RuntimeError as exc:
            raise RuntimeError(f"404 Not Found: {ref}:{path}") from exc

        content_b64 = base64.b64encode(raw.encode()).decode()
        blob_sha = _git("rev-parse", f"refs/heads/{ref}:{path}", cwd=self.repo_path)
        return {"sha": blob_sha, "content": content_b64}

    def put_file_content(
        self,
        path: str,
        branch: str,
        content_b64: str,
        message: str,
        sha: str | None = None,
    ) -> dict[str, Any]:
        """Write a file to a branch using git plumbing."""
        content_bytes = base64.b64decode(content_b64)

        # Write blob
        blob_sha = _write_blob(self.repo_path, content_bytes)

        # Get current tree for branch
        parent_sha = _git("rev-parse", f"refs/heads/{branch}", cwd=self.repo_path)
        current_tree_sha = _git(
            "rev-parse", f"refs/heads/{branch}^{{tree}}", cwd=self.repo_path
        )

        new_tree_sha = _update_tree(self.repo_path, current_tree_sha, path, blob_sha)

        commit_sha = _git(
            "commit-tree",
            new_tree_sha,
            "-p",
            parent_sha,
            "-m",
            message,
            cwd=self.repo_path,
        )

        _git(
            "update-ref",
            f"refs/heads/{branch}",
            commit_sha,
            cwd=self.repo_path,
        )

        return {"content": {"sha": blob_sha}}

    def commit_files(self, branch: str, files: dict[str, str], message: str) -> str:
        """Commit multiple files to a branch in a single commit."""
        parent_sha = _git("rev-parse", f"refs/heads/{branch}", cwd=self.repo_path)
        current_tree_sha = _git(
            "rev-parse", f"refs/heads/{branch}^{{tree}}", cwd=self.repo_path
        )

        # Apply each file to the tree
        tree_sha = current_tree_sha
        for path, content in files.items():
            blob_sha = _write_blob(self.repo_path, content.encode())
            tree_sha = _update_tree(self.repo_path, tree_sha, path, blob_sha)

        commit_sha = _git(
            "commit-tree",
            tree_sha,
            "-p",
            parent_sha,
            "-m",
            message,
            cwd=self.repo_path,
        )

        _git(
            "update-ref",
            f"refs/heads/{branch}",
            commit_sha,
            cwd=self.repo_path,
        )
        return commit_sha

    def create_orphan_branch(self, branch: str, files: dict[str, str]) -> None:
        """Create an orphan branch with the given files (content is plain text)."""
        # Build tree from scratch
        tree_items = []
        for path, content in files.items():
            blob_sha = _write_blob(self.repo_path, content.encode())
            tree_items.append(f"100644 blob {blob_sha}\t{path}")

        tree_sha = _mktree(self.repo_path, tree_items)

        commit_sha = _git(
            "commit-tree",
            tree_sha,
            "-m",
            f"Initialize {branch}",
            cwd=self.repo_path,
        )

        _git(
            "update-ref",
            f"refs/heads/{branch}",
            commit_sha,
            cwd=self.repo_path,
        )

    # --- Deployments (in-memory) ---

    def create_deployment(self, description: str, ref: str = "main") -> int:
        did = self._next_deployment_id
        self._next_deployment_id += 1
        self._deployments[did] = {
            "id": did,
            "description": description,
            "ref": ref,
            "state": "queued",
        }
        return did

    def update_deployment_status(
        self,
        deployment_id: int,
        state: str,
        description: str = "",
        log_url: str = "",
    ) -> None:
        if deployment_id in self._deployments:
            self._deployments[deployment_id]["state"] = state
            self._deployments[deployment_id]["description"] = description

    # --- Misc ---

    def create_commit_status(
        self,
        sha: str,
        state: str,
        description: str = "",
        context: str = "Final Results",
    ) -> None:
        pass

    def get_pr_ci_status(self, pr_number: int) -> tuple[bool | None, str]:
        return True, ""

    def dispatch_ci_on_ref(self, ref: str) -> None:
        pass

    def get_user_permission(self, username: str) -> str:
        return "admin"

    def get_failed_job_info(self, run_url: str) -> tuple[str, str]:
        return "", ""


# --- Private helpers ---


def _ensure_branch_with_commit(
    bare: str,
    work: str,
    branch: str,
    base: str,
    commit_message: str,
) -> None:
    """Create branch off base in the bare repo via a working clone.

    If the branch already exists in origin, this is a no-op.
    """
    # Fetch to make sure we have the base
    _git("fetch", "origin", cwd=work)

    # Check if branch already exists in origin
    result = subprocess.run(
        ["git", "ls-remote", "--exit-code", "--heads", "origin", branch],
        cwd=work,
        capture_output=True,
    )
    if result.returncode == 0:
        return  # already exists

    # Create and push the branch
    _git("checkout", "-b", branch, f"origin/{base}", cwd=work)
    # Add a file with unique content so this PR has a real commit to merge
    import os

    fname = os.path.join(work, f"{branch.replace('/', '-')}.txt")
    with open(fname, "w") as f:
        f.write(f"{commit_message}\n")

    _git("add", ".", cwd=work)
    _git("commit", "-m", commit_message, cwd=work)
    _git("push", "origin", f"HEAD:refs/heads/{branch}", cwd=work)
    _git("checkout", "main", cwd=work)


def _write_blob(repo_path: str, content: bytes) -> str:
    """Write bytes as a git blob object and return the SHA."""
    result = subprocess.run(
        ["git", "hash-object", "-w", "--stdin"],
        input=content,
        cwd=repo_path,
        capture_output=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"hash-object failed: {result.stderr.decode().strip()}")
    return result.stdout.decode().strip()


def _mktree(repo_path: str, tree_items: list[str]) -> str:
    """Create a tree object from a list of tree item strings."""
    input_text = "\n".join(tree_items) + "\n"
    result = subprocess.run(
        ["git", "mktree"],
        input=input_text.encode(),
        cwd=repo_path,
        capture_output=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"mktree failed: {result.stderr.decode().strip()}")
    return result.stdout.decode().strip()


def _update_tree(repo_path: str, base_tree_sha: str, path: str, blob_sha: str) -> str:
    """Return a new tree SHA that is base_tree with path updated to blob_sha.

    Handles nested paths by building the tree listing from ls-tree and
    replacing or adding the target path entry.
    """
    # For simplicity, handle only flat paths (no subdirectories).
    # Files like "main/STATUS.md" would need recursive tree manipulation;
    # we use a simpler approach: ls-tree the base, swap/add the entry.
    if "/" in path:
        return _update_nested_tree(repo_path, base_tree_sha, path, blob_sha)

    existing = _git("ls-tree", base_tree_sha, cwd=repo_path)
    lines = existing.splitlines()
    new_lines = [line for line in lines if not line.split("\t", 1)[-1] == path]
    new_lines.append(f"100644 blob {blob_sha}\t{path}")
    return _mktree(repo_path, new_lines)


def _update_nested_tree(
    repo_path: str, base_tree_sha: str, path: str, blob_sha: str
) -> str:
    """Update a nested path in a tree, creating intermediate trees as needed."""
    parts = path.split("/", 1)
    dir_name, rest = parts[0], parts[1]

    existing = _git("ls-tree", base_tree_sha, cwd=repo_path)
    lines = existing.splitlines()

    # Find existing subtree for dir_name
    sub_tree_sha: str | None = None
    for line in lines:
        mode, obj_type, sha, name = line.split(None, 3)
        if obj_type == "tree" and name == dir_name:
            sub_tree_sha = sha
            break

    if sub_tree_sha is None:
        # Create an empty tree for the new directory
        sub_tree_sha = _mktree(repo_path, [])

    new_sub_tree_sha = _update_tree(repo_path, sub_tree_sha, rest, blob_sha)

    # Rebuild parent tree with updated subtree
    new_lines = []
    for line in lines:
        parts_line = line.split(None, 3)
        if len(parts_line) == 4 and parts_line[3] == dir_name:
            continue
        new_lines.append(line)
    new_lines.append(f"040000 tree {new_sub_tree_sha}\t{dir_name}")
    return _mktree(repo_path, new_lines)
