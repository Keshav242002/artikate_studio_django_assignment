"""
Section 2 — Sliding window rate limiter using Redis sorted sets.

Why sliding window over the alternatives
-----------------------------------------
- Fixed window (INCR + EXPIRE): simple, but allows 2× the limit at window
  boundaries. If limit=200/min and a window resets at :00, a client can send
  200 at :59 and 200 more at :01 — 400 requests in a 2-second span.
- Token bucket (DECR + TTL): smooths bursts but the "remaining tokens" state
  drifts if Redis restarts, and fractional replenishment rates add complexity.
- Sliding window (sorted set + ZREMRANGEBYSCORE): precise, no boundary burst,
  the window truly slides second by second.

How atomicity is guaranteed
----------------------------
We use Redis WATCH + MULTI/EXEC (optimistic locking):

  1. WATCH the sorted set key — Redis will abort our EXEC if anything
     modifies the key between our read and our write.
  2. Read: ZREMRANGEBYSCORE (evict stale entries) + ZCARD (count current).
     These run immediately in "immediate mode" before MULTI.
  3. If count < limit: MULTI → ZADD + EXPIRE → EXEC.
     If the key was touched between WATCH and EXEC, Redis raises WatchError
     and we retry the whole loop.
  4. If count >= limit: skip MULTI, return blocked.

This is equivalent in safety to a Lua script — no two workers can both read
"count=199" and both proceed. The WATCH makes concurrent writes visible.

Why MULTI/EXEC instead of Lua here
------------------------------------
Both are valid (the spec explicitly lists "MULTI/EXEC" as an acceptable
atomicity mechanism). MULTI/EXEC is chosen because:
  - Works with plain fakeredis in tests (no Lua runtime required)
  - More debuggable — each command is a native Redis command visible in logs
  - The retry-on-WatchError loop is a standard Redis pattern

Redis failure behaviour
------------------------
Fail closed — if Redis raises ConnectionError, we raise RateLimitExceeded
which causes the Celery task to retry. We do NOT silently allow the email
through. For a transactional email system, accidentally bursting past the
provider limit is worse than a short delay while Redis recovers.
"""
import time
import random
import logging
from typing import Optional

import redis
from redis import WatchError

logger = logging.getLogger(__name__)


class RateLimitExceeded(Exception):
    """Raised when the sliding window limit has been reached."""
    pass


class SlidingWindowRateLimiter:
    """
    Redis sliding window rate limiter backed by a sorted set.

    Thread-safe and process-safe — all state lives in Redis.
    Multiple Celery workers share the same Redis key, so the limit
    is enforced globally, not per-worker.

    Atomicity: WATCH + MULTI/EXEC (optimistic locking).

    Usage::

        limiter = SlidingWindowRateLimiter('email:rate_limit', limit=200, window_seconds=60)
        try:
            limiter.acquire()
            # ... do the thing
        except RateLimitExceeded:
            # ... retry later
    """

    def __init__(
        self,
        key: str,
        limit: int,
        window_seconds: int,
        redis_client: Optional[redis.Redis] = None,
    ):
        """
        Args:
            key:            Redis key for the sorted set.
            limit:          Maximum number of requests per window.
            window_seconds: Size of the sliding window in seconds.
            redis_client:   Optional Redis client (injected for testing with
                            fakeredis). If None, created lazily from Django
                            settings on first use.
        """
        self.key = key
        self.limit = limit
        self.window_seconds = window_seconds
        self._redis = redis_client

    @property
    def redis(self) -> redis.Redis:
        """Lazy Redis client — created on first use so Django settings are loaded."""
        if self._redis is None:
            from django.conf import settings
            self._redis = redis.from_url(
                getattr(settings, 'RATE_LIMITER_REDIS_URL', 'redis://localhost:6379/1')
            )
        return self._redis

    def acquire(self) -> None:
        """
        Attempt to acquire a rate limit token.

        Uses WATCH + MULTI/EXEC for atomicity — if another worker modifies
        the sorted set between our read and write, we retry automatically.

        Raises:
            RateLimitExceeded: if the limit is reached for the current window,
                               or if Redis is unreachable (fail closed).
        """
        try:
            allowed = self._try_acquire()
        except redis.ConnectionError as exc:
            logger.error("Redis unreachable in rate limiter, failing closed: %s", exc)
            raise RateLimitExceeded("Redis unavailable — failing closed") from exc

        if not allowed:
            raise RateLimitExceeded(
                f"Rate limit of {self.limit} per {self.window_seconds}s exceeded"
            )

    def _try_acquire(self) -> bool:
        """
        Core WATCH + MULTI/EXEC logic. Returns True if allowed, False if blocked.
        Retries automatically on WatchError (concurrent modification).
        """
        max_retries = 10  # safety cap — in practice 1-2 retries is typical

        for attempt in range(max_retries):
            try:
                return self._execute_transaction()
            except WatchError:
                # Another worker modified the key between our WATCH and EXEC.
                # Retry — our next read will see the updated count.
                logger.debug(
                    "WatchError on rate limiter key '%s', retrying (attempt %d)",
                    self.key, attempt + 1,
                )
                continue

        # Extremely unlikely — treat as blocked to be safe
        logger.warning("Rate limiter exhausted retries on WatchError, treating as blocked")
        return False

    def _execute_transaction(self) -> bool:
        """Single WATCH + MULTI/EXEC attempt. Raises WatchError on conflict."""
        now_us = int(time.time() * 1_000_000)          # microseconds
        window_start_us = now_us - (self.window_seconds * 1_000_000)
        ttl = self.window_seconds + 1

        with self.redis.pipeline() as pipe:
            # --- WATCH phase ---
            # If the key changes between here and EXEC, Redis aborts and
            # raises WatchError, which we catch in _try_acquire().
            pipe.watch(self.key)

            # --- Immediate mode (before MULTI) ---
            # These commands execute instantly, not queued.
            pipe.zremrangebyscore(self.key, '-inf', window_start_us)
            count = pipe.zcard(self.key)

            if count >= self.limit:
                # Limit reached — unwatch and return blocked without writing.
                pipe.reset()
                return False

            # --- MULTI/EXEC phase ---
            # Queue the write. If the key was touched since WATCH, EXEC raises
            # WatchError before any of these commands run.
            pipe.multi()
            # Use timestamp + random suffix as member to avoid collisions when
            # two workers call ZADD at the exact same microsecond.
            member = f"{now_us}-{random.randint(0, 999_999)}"
            pipe.zadd(self.key, {member: now_us})
            pipe.expire(self.key, ttl)
            pipe.execute()  # raises WatchError if key was modified since WATCH

        return True

    def current_count(self) -> int:
        """
        Return the number of requests in the current window.
        Used in tests and monitoring — not in the hot path.
        """
        now_us = int(time.time() * 1_000_000)
        window_start_us = now_us - (self.window_seconds * 1_000_000)
        try:
            self.redis.zremrangebyscore(self.key, '-inf', window_start_us)
            return self.redis.zcard(self.key)
        except redis.ConnectionError:
            return 0
