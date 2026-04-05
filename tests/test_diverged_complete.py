"""Tests for complete_batch handling of 422 HTTPError from update_ref.

When main diverges (emergency commits pushed directly), update_ref with
force=False returns 422. This should be wrapped as BatchError("diverged")
so the existing retry logic in cli.py can handle it.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
import requests

from merge_queue.batch import BatchError, complete_batch

from tests.conftest import make_batch, make_pr, make_stack


def _make_422_error() -> requests.HTTPError:
    """Create a requests.HTTPError with a 422 response."""
    response = MagicMock(spec=requests.Response)
    response.status_code = 422
    response.text = "Update is not a fast forward"
    error = requests.HTTPError(
        "422 Client Error: Unprocessable Entity", response=response
    )
    error.response = response
    return error


def _make_500_error() -> requests.HTTPError:
    """Create a requests.HTTPError with a 500 response."""
    response = MagicMock(spec=requests.Response)
    response.status_code = 500
    response.text = "Internal Server Error"
    error = requests.HTTPError("500 Server Error", response=response)
    error.response = response
    return error


class TestDivergedComplete:
    """update_ref 422 should become BatchError with 'diverged' in message."""

    def test_422_from_update_ref_raises_batch_error(self, mock_client):
        """When update_ref raises HTTPError 422, complete_batch should raise
        BatchError with 'diverged' so the CLI retry logic can handle it."""
        pr = make_pr(1, "feat-a", head_sha="sha-1")
        batch = make_batch(make_stack(pr))
        mock_client.update_ref.side_effect = _make_422_error()

        with pytest.raises(BatchError, match="diverged"):
            complete_batch(mock_client, batch)

    def test_non_422_http_error_propagates(self, mock_client):
        """Non-422 HTTPErrors from update_ref should NOT be caught —
        they should propagate as-is so real errors aren't swallowed."""
        pr = make_pr(1, "feat-a", head_sha="sha-1")
        batch = make_batch(make_stack(pr))
        mock_client.update_ref.side_effect = _make_500_error()

        with pytest.raises(requests.HTTPError, match="500"):
            complete_batch(mock_client, batch)
