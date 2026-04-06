"""Tests for rate limit tracking and caching."""

from __future__ import annotations

from unittest.mock import MagicMock

from merge_queue.providers import RateLimitInfo


class TestRateLimitInfo:
    def test_initial_state(self):
        rl = RateLimitInfo()
        assert rl.limit == 0
        assert rl.remaining == 0
        assert rl.used == 0
        assert rl.reset_at is None
        assert rl.request_count == 0

    def test_update_from_response(self):
        rl = RateLimitInfo()
        resp = MagicMock()
        resp.headers = {
            "X-RateLimit-Limit": "5000",
            "X-RateLimit-Remaining": "4990",
            "X-RateLimit-Used": "10",
            "X-RateLimit-Reset": "1735689600",
        }

        rl.update(resp)

        assert rl.limit == 5000
        assert rl.remaining == 4990
        assert rl.used == 10
        assert rl.reset_at is not None
        assert rl.request_count == 1

    def test_increments_request_count(self):
        rl = RateLimitInfo()
        resp = MagicMock()
        resp.headers = {}

        rl.update(resp)
        rl.update(resp)
        rl.update(resp)

        assert rl.request_count == 3

    def test_missing_headers_ok(self):
        rl = RateLimitInfo()
        resp = MagicMock()
        resp.headers = {}

        rl.update(resp)

        assert rl.limit == 0
        assert rl.request_count == 1

    def test_low_remaining_logs_warning(self, caplog):
        rl = RateLimitInfo()
        resp = MagicMock()
        resp.headers = {
            "X-RateLimit-Limit": "5000",
            "X-RateLimit-Remaining": "50",
            "X-RateLimit-Reset": "1735689600",
        }

        import logging

        with caplog.at_level(logging.WARNING):
            rl.update(resp)

        assert "rate limit low" in caplog.text.lower()

    def test_summary(self):
        rl = RateLimitInfo()
        resp = MagicMock()
        resp.headers = {
            "X-RateLimit-Limit": "5000",
            "X-RateLimit-Remaining": "4990",
            "X-RateLimit-Reset": "1735689600",
        }
        rl.update(resp)

        summary = rl.summary()
        assert "requests=1" in summary
        assert "remaining=4990/5000" in summary
