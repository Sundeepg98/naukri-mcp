"""Resilience utilities — retry, rate limiting for autonomous operations."""

import asyncio
import logging
import random
import time
from typing import Awaitable, Callable, Optional, TypeVar

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
    """Sliding-window rate limiter for outbound calls (anti-detection throttle).

    Caps throughput to ``max_calls`` per ``period_seconds``. When the window is
    full, ``acquire()`` sleeps until the oldest call ages out. An asyncio.Lock
    serializes the read-modify-write of the window so concurrent callers (e.g.
    batch apply with ``max_concurrent`` > 1) can't all slip past the cap at once.

    The clock and sleep are injectable (``time_func`` / ``sleep_func``) so tests
    can drive it with a fake clock — no real waiting.
    """

    def __init__(
        self,
        max_calls: int = 10,
        period_seconds: float = 60.0,
        time_func: Callable[[], float] = time.monotonic,
        sleep_func: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self._max_calls = max_calls
        self._period = period_seconds
        self._calls: list[float] = []
        self._time = time_func
        self._sleep = sleep_func
        self._lock = asyncio.Lock()

    async def acquire(self, operation: str = ""):
        """Wait until a call is allowed. Blocks (sleeps) if the window is full."""
        # max_calls <= 0 disables limiting (defensive — avoids divide-by-window edge).
        if self._max_calls <= 0:
            return
        async with self._lock:
            now = self._time()
            # Drop entries that have aged out of the window.
            self._calls = [t for t in self._calls if now - t < self._period]
            if len(self._calls) >= self._max_calls:
                wait = self._period - (now - self._calls[0])
                if wait > 0:
                    logger.info("Rate limit reached for %s, waiting %.1fs", operation, wait)
                    await self._sleep(wait)
                    # Re-evaluate the window after sleeping.
                    now = self._time()
                    self._calls = [t for t in self._calls if now - t < self._period]
            self._calls.append(self._time())


async def jittered_delay(
    base_seconds: float,
    jitter_min: float,
    jitter_max: float,
    sleep_func: Callable[[float], Awaitable[None]] = asyncio.sleep,
    rand_func: Callable[[float, float], float] = random.uniform,
) -> float:
    """Sleep for ``base_seconds`` + a random jitter in ``[jitter_min, jitter_max]``.

    A constant cadence between actions is a bot tell; this randomizes the gap.
    Returns the total delay actually slept (handy for assertions/logging). The
    sleep and RNG are injectable so tests are deterministic and don't wait.
    """
    if jitter_max < jitter_min:
        jitter_min, jitter_max = jitter_max, jitter_min
    jitter = rand_func(jitter_min, jitter_max) if jitter_max > 0 else 0.0
    total = max(0.0, base_seconds) + max(0.0, jitter)
    if total > 0:
        await sleep_func(total)
    return total


# Shared apply-path rate limiter (anti-detection). Lazily built from config so
# env overrides are honored and tests can substitute their own instance.
_apply_rate_limiter: Optional[RateLimiter] = None


def get_apply_rate_limiter() -> RateLimiter:
    """Return the process-wide apply-path RateLimiter, building it on first use."""
    global _apply_rate_limiter
    if _apply_rate_limiter is None:
        from naukri_server.config import APPLY_RATE_MAX_CALLS, APPLY_RATE_PERIOD_SECONDS
        _apply_rate_limiter = RateLimiter(
            max_calls=APPLY_RATE_MAX_CALLS,
            period_seconds=APPLY_RATE_PERIOD_SECONDS,
        )
    return _apply_rate_limiter
