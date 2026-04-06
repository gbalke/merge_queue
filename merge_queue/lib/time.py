"""Shared time utilities."""

from __future__ import annotations

import datetime
import os


def now_iso() -> str:
    """Current UTC time as ISO string."""
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def event_time_or_now() -> str:
    """Use GITHUB_EVENT_TIME if set, else now.

    GITHUB_EVENT_TIME is set from github.event.pull_request.updated_at in the
    workflow, which reflects when the label was added -- a more accurate
    queued_at for PRs that waited in the concurrency queue before do_enqueue ran.
    """
    event_time = os.environ.get("GITHUB_EVENT_TIME", "")
    return event_time if event_time else now_iso()
