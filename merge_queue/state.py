"""Queue state snapshot — fetched once, used by rules + queue logic.

Eliminates redundant API calls by fetching all needed data in a single pass.
"""

from __future__ import annotations

import datetime
import logging
from typing import Any

from merge_queue.github_client import GitHubClientProtocol
from merge_queue.types import PullRequest

log = logging.getLogger(__name__)


class QueueState:
    """Immutable snapshot of the merge queue's current state.

    Fetch once via QueueState.fetch(), then pass to rules and queue logic.
    """

    def __init__(
        self,
        default_branch: str,
        mq_branches: list[str],
        rulesets: list[dict[str, Any]],
        prs: list[PullRequest],
        all_pr_data: list[dict[str, Any]],
    ):
        self.default_branch = default_branch
        self.mq_branches = mq_branches
        self.rulesets = rulesets
        self.prs = prs  # Only queue/locked PRs, with timestamps
        self.all_pr_data = all_pr_data  # Raw API data for all open PRs

    @classmethod
    def fetch(cls, client: GitHubClientProtocol) -> QueueState:
        """Fetch all state in minimal API calls.

        API calls made:
          1. get_default_branch()
          2. list_mq_branches()
          3. list_open_prs()
          4. list_rulesets()
          5. get_label_timestamp() x N  (one per queued/locked PR)
        """
        default_branch = client.get_default_branch()  # 1 call (cached)
        mq_branches = client.list_mq_branches()  # 1 call (cached)
        all_pr_data = client.list_open_prs()  # 1 call (cached)
        rulesets = client.list_rulesets()  # 1 call (cached)

        # Build PullRequest objects only for queued/locked PRs
        prs: list[PullRequest] = []
        for pr_data in all_pr_data:
            labels = tuple(lbl["name"] for lbl in pr_data.get("labels", []))
            if "queue" not in labels and "locked" not in labels:
                continue

            queued_at = client.get_label_timestamp(pr_data["number"], "queue")
            if queued_at is None and "locked" in labels:
                queued_at = client.get_label_timestamp(pr_data["number"], "locked")

            prs.append(
                PullRequest(
                    number=pr_data["number"],
                    head_sha=pr_data["head"]["sha"],
                    head_ref=pr_data["head"]["ref"],
                    base_ref=pr_data["base"]["ref"],
                    labels=labels,
                    queued_at=queued_at or datetime.datetime.now(datetime.timezone.utc),
                )
            )

        call_count = 4 + len(prs)  # base calls + 1 timestamp per PR
        log.info(
            "Fetched queue state: %d open PRs, %d queued/locked, %d mq branches, %d rulesets (%d API calls)",
            len(all_pr_data),
            len(prs),
            len(mq_branches),
            len(rulesets),
            call_count,
        )
        return cls(default_branch, mq_branches, rulesets, prs, all_pr_data)

    @property
    def has_active_batch(self) -> bool:
        return len(self.mq_branches) > 0

    @property
    def locked_prs(self) -> list[PullRequest]:
        return [pr for pr in self.prs if "locked" in pr.labels]

    @property
    def queued_prs(self) -> list[PullRequest]:
        return [
            pr for pr in self.prs if "queue" in pr.labels and "locked" not in pr.labels
        ]
