"""In-process token-bucket rate limiter, keyed per caller.

Correct for a single replica. With several replicas each enforces its own budget, so the
effective limit is N x rate; a shared limit would live in Redis or, better, in Azure API
Management in front of the service (see docs).
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass


@dataclass
class _Bucket:
    tokens: float
    updated: float


class RateLimiter:
    def __init__(self, rate_per_minute: float, burst: int, *, clock: object = time.monotonic, max_keys: int = 10_000):
        self._rate = rate_per_minute / 60.0
        self._burst = float(burst)
        self._clock = clock
        self._buckets: dict[str, _Bucket] = {}
        self._lock = threading.Lock()
        self._max_keys = max_keys

    def allow(self, key: str) -> tuple[bool, float]:
        """(allowed, seconds until a token is available)."""
        now: float = self._clock()  # type: ignore[operator]
        with self._lock:
            b = self._buckets.get(key)
            if b is None:
                if len(self._buckets) >= self._max_keys:  # bound memory under key-spraying
                    self._buckets.pop(next(iter(self._buckets)))
                b = self._buckets[key] = _Bucket(self._burst, now)
            b.tokens = min(self._burst, b.tokens + (now - b.updated) * self._rate)
            b.updated = now
            if b.tokens >= 1.0:
                b.tokens -= 1.0
                return True, 0.0
            return False, (1.0 - b.tokens) / self._rate
