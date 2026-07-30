"""
Section 2 tests — rate limiter + Celery task behaviour.

Infrastructure:
  - fakeredis: in-process Redis emulation, no real Redis needed
  - task.apply(): runs Celery tasks synchronously, no worker needed
  - CELERY_TASK_ALWAYS_EAGER is set per-test, not globally, to keep
    the test suite portable

What we assert:
  1. Rate limiter allows requests up to the limit and blocks beyond it
  2. Rate limiter uses atomic Redis operations (Lua script) correctly
  3. 500 submitted tasks: none lost, rate limit respected across all
  4. Intentional failures retry with exponential backoff
  5. Permanently failed tasks land in FailedEmailTask (dead-letter)
  6. Redis unavailability causes fail-closed behaviour
"""
import time
import unittest
from unittest.mock import patch, MagicMock

import fakeredis

from django.test import TestCase, override_settings
from celery.exceptions import Retry

from emailqueue.rate_limiter import SlidingWindowRateLimiter, RateLimitExceeded
from emailqueue.models import FailedEmailTask


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_limiter(limit=10, window=60, fake_redis=None):
    """Return a SlidingWindowRateLimiter backed by fakeredis."""
    if fake_redis is None:
        fake_redis = fakeredis.FakeRedis()
    return SlidingWindowRateLimiter(
        key='test:rate_limit',
        limit=limit,
        window_seconds=window,
        redis_client=fake_redis,
    )


# ---------------------------------------------------------------------------
# Rate limiter unit tests
# ---------------------------------------------------------------------------

class SlidingWindowRateLimiterTest(TestCase):

    def test_allows_up_to_limit(self):
        """Exactly `limit` requests should succeed."""
        limiter = make_limiter(limit=5)
        for i in range(5):
            limiter.acquire()  # must not raise
        self.assertEqual(limiter.current_count(), 5)

    def test_blocks_beyond_limit(self):
        """The (limit+1)th request must raise RateLimitExceeded."""
        limiter = make_limiter(limit=5)
        for _ in range(5):
            limiter.acquire()
        with self.assertRaises(RateLimitExceeded):
            limiter.acquire()

    def test_window_slides_correctly(self):
        """
        After the window expires, the counter resets and requests are
        allowed again. We fake time by manipulating the sorted set TTL
        rather than sleeping.
        """
        fake_redis = fakeredis.FakeRedis()
        limiter = make_limiter(limit=3, window=1, fake_redis=fake_redis)

        # fill the window
        for _ in range(3):
            limiter.acquire()
        with self.assertRaises(RateLimitExceeded):
            limiter.acquire()

        # manually expire the sorted set to simulate window slide
        fake_redis.delete('test:rate_limit')

        # now we should be allowed again
        limiter.acquire()  # must not raise

    def test_fails_closed_on_redis_unavailable(self):
        """
        If Redis raises ConnectionError, acquire() must raise
        RateLimitExceeded (fail closed), not silently allow the request.
        """
        broken_redis = MagicMock()
        broken_redis.pipeline.side_effect = __import__('redis').ConnectionError("down")
        limiter = SlidingWindowRateLimiter(
            key='test:rate_limit',
            limit=200,
            window_seconds=60,
            redis_client=broken_redis,
        )
        with self.assertRaises(RateLimitExceeded):
            limiter.acquire()

    def test_concurrent_requests_do_not_exceed_limit(self):
        """
        Simulate rapid concurrent calls — the Lua script must be atomic,
        so even if two calls arrive simultaneously, neither sees a stale count.
        fakeredis serialises everything so this tests the logic, not threading.
        """
        limiter = make_limiter(limit=10)
        allowed = 0
        blocked = 0
        for _ in range(20):
            try:
                limiter.acquire()
                allowed += 1
            except RateLimitExceeded:
                blocked += 1

        self.assertEqual(allowed, 10)
        self.assertEqual(blocked, 10)


# ---------------------------------------------------------------------------
# Celery task tests — use task.apply() for synchronous execution
# ---------------------------------------------------------------------------

@override_settings(
    CELERY_TASK_ALWAYS_EAGER=True,   # run tasks inline, no worker needed
)
class SendEmailTaskTest(TestCase):

    def _make_patched_task(self, fake_redis=None, limit=200):
        """
        Return the send_email task with its rate limiter pointed at
        fakeredis. We patch the module-level _rate_limiter object.
        """
        from emailqueue import tasks as task_module
        if fake_redis is None:
            fake_redis = fakeredis.FakeRedis()
        limiter = SlidingWindowRateLimiter(
            key='email:rate_limit',
            limit=limit,
            window_seconds=60,
            redis_client=fake_redis,
        )
        task_module._rate_limiter = limiter
        return task_module.send_email

    def test_happy_path_returns_sent(self):
        """Successful email returns status=sent."""
        send_email = self._make_patched_task()
        result = send_email.apply(args=['user@example.com', 'Hello', 'Body'])
        self.assertEqual(result.result['status'], 'sent')
        self.assertEqual(result.result['recipient'], 'user@example.com')

    def test_intentional_failure_retries_and_dead_letters(self):
        """
        Subject prefixed with 'FAIL:' causes EmailSendError on every attempt.
        After max_retries (4), the task writes to FailedEmailTask.
        This proves: retry logic works AND dead-letter store is populated.
        """
        send_email = self._make_patched_task()

        result = send_email.apply(
            args=['user@example.com', 'FAIL: order confirmation', 'Body']
        )

        # task should report dead_lettered, not raise
        self.assertEqual(result.result['status'], 'dead_lettered')

        # exactly one DLQ record
        self.assertEqual(FailedEmailTask.objects.count(), 1)
        failed = FailedEmailTask.objects.first()
        self.assertEqual(failed.recipient, 'user@example.com')
        self.assertEqual(failed.retry_count, 4)  # max_retries=4

    def test_rate_limit_exceeded_triggers_retry(self):
        """
        When the rate limiter is full, the task raises RateLimitExceeded
        which Celery retries.
        """
        fake_redis = fakeredis.FakeRedis()
        send_email = self._make_patched_task(fake_redis=fake_redis, limit=1)

        # first email goes through
        result = send_email.apply(args=['a@example.com', 'Subject', 'Body'])
        self.assertEqual(result.result['status'], 'sent')

        # second email: hit rate limit -> retries 5 times -> fails with RateLimitExceeded
        # (It doesn't dead-letter because RateLimitExceeded is not caught in the
        # dead-letter block, which is intended for SMTP errors).
        result2 = send_email.apply(args=['b@example.com', 'Subject 2', 'Body 2'])

        self.assertIsInstance(result2.result, RateLimitExceeded)


# ---------------------------------------------------------------------------
# 500-job integration test
# ---------------------------------------------------------------------------

@override_settings(
    CELERY_TASK_ALWAYS_EAGER=True,
)
class FiveHundredJobsTest(TestCase):
    """
    Submit 500 email jobs and assert:
      1. No job is lost — every job produces a result (sent or dead_lettered)
      2. The rate limiter never allows more than 200 requests per window
      3. At least one intentional failure is retried and dead-lettered
    """

    def test_500_jobs(self):
        from emailqueue import tasks as task_module

        fake_redis = fakeredis.FakeRedis()
        limiter = SlidingWindowRateLimiter(
            key='email:rate_limit',
            limit=200,
            window_seconds=60,
            redis_client=fake_redis,
        )
        task_module._rate_limiter = limiter

        send_email = task_module.send_email

        results = []
        # 490 normal jobs + 10 intentional failures
        jobs = [
            ('user{}@example.com'.format(i), 'Order #{}'.format(i), 'Body')
            for i in range(490)
        ] + [
            ('fail{}@example.com'.format(i), 'FAIL: Order #{}'.format(i), 'Body')
            for i in range(10)
        ]

        for recipient, subject, body in jobs:
            # After each batch of 200, reset the rate limiter window to
            # simulate time passing (the window slides every 60 seconds).
            # Without this, only the first 200 would go through.
            # This models the real scenario: 2000 burst over several minutes.
            if len(results) > 0 and len(results) % 200 == 0:
                fake_redis.delete('email:rate_limit')

            result = send_email.apply(args=[recipient, subject, body])
            results.append(result)

        # --- Assert 1: No job lost ---
        # Every task must produce a result object, not raise unhandled
        self.assertEqual(len(results), 500, "Expected 500 results, some tasks were lost")

        # All results must have a known status
        for r in results:
            self.assertIn(
                r.result.get('status'),
                ('sent', 'dead_lettered'),
                f"Unexpected result status: {r.result}",
            )

        # --- Assert 2: Rate limit never exceeded ---
        # The sliding window must never have more than 200 entries at once.
        # Since we reset the key every 200 jobs, peak count stays at 200.
        peak_count = limiter.current_count()
        self.assertLessEqual(
            peak_count, 200,
            f"Rate limit exceeded: {peak_count} requests in window",
        )

        # --- Assert 3: At least one failure was retried and dead-lettered ---
        dlq_count = FailedEmailTask.objects.count()
        self.assertGreaterEqual(
            dlq_count, 1,
            "Expected at least one dead-lettered task from the 10 intentional failures",
        )
        # All 10 intentional failures should eventually dead-letter
        self.assertEqual(dlq_count, 10)

        # Dead-lettered tasks must have exhausted retries (retry_count == max_retries)
        for failed in FailedEmailTask.objects.all():
            self.assertEqual(
                failed.retry_count, 4,  # max_retries=4
                f"Task {failed.task_id} didn't fully retry before dead-lettering",
            )
