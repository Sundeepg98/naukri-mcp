"""Resilience utilities — retry, rate limiting for autonomous operations."""

import asyncio
import logging
import time
from typing import Callable, Awaitable, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


async def retry_with_backoff(
    fn: Callable[[], Awaitable[T]],
    max_attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 8.0,
    operation: str = "operation",
) -> T:
    """Retry an async operation with exponential backoff + jitter.

    Args:
        fn: Async callable to retry
        max_attempts: Maximum retry attempts (default 3)
        base_delay: Initial delay in seconds (default 1.0)
        max_delay: Maximum delay cap in seconds (default 8.0)
        operation: Description for logging

    Returns:
        Result of fn()

    Raises:
        Last exception if all attempts fail
    """
    last_exc = None
    for attempt in range(max_attempts):
        try:
            return await fn()
        except Exception as e:
            last_exc = e
            if attempt < max_attempts - 1:
                import random
                delay = min(base_delay * (2 ** attempt) + random.uniform(0, 0.5), max_delay)
                logger.warning("%s failed (attempt %d/%d), retrying in %.1fs: %s",
                              operation, attempt + 1, max_attempts, delay, e)
                await asyncio.sleep(delay)
            else:
                logger.error("%s failed after %d attempts: %s", operation, max_attempts, e)
    raise last_exc


class RateLimiter:
    """Simple token bucket rate limiter for API calls."""

    def __init__(self, max_calls: int = 10, period_seconds: float = 60.0):
        self._max_calls = max_calls
        self._period = period_seconds
        self._calls: list[float] = []

    async def acquire(self, operation: str = ""):
        """Wait until a call is allowed. Blocks if rate limit exceeded."""
        now = time.monotonic()
        # Remove expired entries
        self._calls = [t for t in self._calls if now - t < self._period]
        if len(self._calls) >= self._max_calls:
            wait = self._period - (now - self._calls[0])
            if wait > 0:
                logger.info("Rate limit reached for %s, waiting %.1fs", operation, wait)
                await asyncio.sleep(wait)
        self._calls.append(time.monotonic())
