"""Retry with backoff (B9, lightweight half) — wraps transient agent/tool failures.

The idempotency-across-replay half of B9 is deferred with durability.
"""

from __future__ import annotations

from datetime import timedelta


class ExponentialBackoffRetry:
    def __init__(self, max_attempts: int = 3, base_seconds: float = 0.0, factor: float = 2.0):
        self.max_attempts = max_attempts
        self.base_seconds = base_seconds
        self.factor = factor

    def next_backoff(self, attempt: int, error: Exception) -> timedelta | None:
        """`attempt` = number of failures so far (0-based). None = give up."""
        if attempt >= self.max_attempts - 1:
            return None
        return timedelta(seconds=self.base_seconds * (self.factor**attempt))
