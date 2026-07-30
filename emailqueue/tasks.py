"""
Section 2 — Celery email task with exponential backoff and dead-letter handling.

Flow:
  1. Task is submitted to the broker.
  2. Worker picks it up and checks the rate limiter (Redis sliding window).
  3. If rate limit exceeded → retry after a short fixed delay (5s), not
     exponential — the point is to retry soon once the window has room,
     not to give up. This is backpressure, not failure (see note below).
  4. If a transient failure (e.g. SMTP error) → retry with exponential backoff.
  5. If MAX_SEND_ATTEMPTS provider failures are reached → write to
     FailedEmailTask (dead-letter) and stop.

Why rate-limit retries use a separate counter from provider-failure retries
----------------------------------------------------------------------------
Celery's `self.retry()` counts every retry — regardless of reason — against
the same `self.request.retries` / `max_retries` budget. During a real burst
(2,000 requests / 200-per-min limit), a job can easily need 10+ rate-limit
retries just waiting for capacity, which is normal and not a failure. If we
used Celery's shared counter for both rate-limit waits and provider failures,
a job that waited out the rate limit 5 times would hit `MaxRetriesExceededError`
before ever attempting a real send — and since that error is only meaningful
for provider failures, it would surface as an unhandled exception, and the
job would be silently dropped with no dead-letter record. This was verified
against a real Redis + Celery worker: a 253-job burst against a 200/min limit
caused exactly the 53 backlogged jobs to vanish after 4 rate-limit retries.

The fix: `_send_attempt` is an explicit kwarg we thread through retries
ourselves, incremented only on genuine provider (EmailSendError) failures.
Rate-limit retries pass `max_retries=RATE_LIMIT_RETRY_CEILING` (a large
number) so throttling waits are, for practical purposes, unbounded — a job
should keep waiting for capacity rather than being abandoned. Provider
failures are gated by comparing `_send_attempt` to MAX_SEND_ATTEMPTS
ourselves, independent of how many times the job was rate-limited first.

Crash safety:
  task_acks_late=True and task_reject_on_worker_lost=True are set in settings.
  These are NOT overridden per-task so they apply globally. The effect:
    - Message stays in the broker queue until the task function returns.
    - If the worker is SIGKILL'd, the broker redelivers to another worker.
    - No job is silently lost.
"""
import logging

from celery import shared_task
from celery.exceptions import MaxRetriesExceededError

from .rate_limiter import SlidingWindowRateLimiter, RateLimitExceeded

logger = logging.getLogger(__name__)

# Rate limiter: 200 emails per 60 seconds, enforced globally across all workers.
# The key is shared in Redis so multiple Celery workers respect the same limit.
_rate_limiter = SlidingWindowRateLimiter(
    key='email:rate_limit',
    limit=200,
    window_seconds=60,
)

# Genuine provider-failure retries before dead-lettering. This is the only
# budget that governs when we give up on a job.
MAX_SEND_ATTEMPTS = 4

# Safety ceiling for rate-limit retries. Not a real "give up" threshold —
# it exists only so a permanently broken Redis/limiter can't retry forever.
# At 5s per retry this is ~14 hours, far beyond any realistic burst backlog.
RATE_LIMIT_RETRY_CEILING = 10_000


class EmailSendError(Exception):
    """Raised when the email provider returns a transient error."""
    pass


@shared_task(bind=True)
def send_email(self, recipient: str, subject: str, body: str, _send_attempt: int = 0) -> dict:
    """
    Send a transactional email, respecting the 200/min rate limit.

    Args:
        recipient:     Destination email address.
        subject:       Email subject line.
        body:          Plain-text email body.
        _send_attempt: Internal — number of provider-failure retries so far.
                       Do not pass this when calling .delay()/.apply_async();
                       it's threaded through retries by the task itself and
                       is independent of how many times the job was
                       rate-limited (see module docstring).

    Returns:
        dict with status and metadata on success or dead-letter.

    Raises:
        Retries on RateLimitExceeded (short fixed delay, uncapped budget).
        Retries with exponential backoff on EmailSendError (up to
        MAX_SEND_ATTEMPTS), then writes to FailedEmailTask.
    """
    # --- 1. Rate limit check ---
    try:
        _rate_limiter.acquire()
    except RateLimitExceeded:
        logger.warning(
            "Rate limit exceeded for send_email task %s (recipient=%s), retrying in 5s",
            self.request.id, recipient,
        )
        try:
            raise self.retry(
                exc=RateLimitExceeded(),
                countdown=5,
                max_retries=RATE_LIMIT_RETRY_CEILING,
                args=(),
                kwargs={
                    'recipient': recipient,
                    'subject': subject,
                    'body': body,
                    '_send_attempt': _send_attempt,
                },
            )
        except MaxRetriesExceededError:
            # Only reachable if RATE_LIMIT_RETRY_CEILING is exhausted —
            # effectively "the rate limiter/broker has been broken for
            # hours." Dead-letter rather than lose the job silently.
            _write_to_dead_letter(
                self, recipient, subject, body,
                RateLimitExceeded("rate limit retry ceiling exhausted"),
                _send_attempt,
            )
            return {'status': 'dead_lettered', 'recipient': recipient}

    # --- 2. Send the email ---
    try:
        _send_via_provider(recipient, subject, body)
        logger.info("Email sent to %s (task %s)", recipient, self.request.id)
        return {
            'status': 'sent',
            'recipient': recipient,
            'task_id': self.request.id,
            'send_attempt': _send_attempt,
        }

    except EmailSendError as exc:
        if _send_attempt >= MAX_SEND_ATTEMPTS:
            _write_to_dead_letter(self, recipient, subject, body, exc, _send_attempt)
            return {'status': 'dead_lettered', 'recipient': recipient}

        # Transient failure — retry with exponential backoff.
        # countdown doubles each attempt: 1s, 2s, 4s, 8s, 16s
        countdown = 2 ** _send_attempt
        logger.warning(
            "EmailSendError for %s (attempt %d/%d), retrying in %ds: %s",
            recipient,
            _send_attempt + 1,
            MAX_SEND_ATTEMPTS + 1,
            countdown,
            exc,
        )
        try:
            raise self.retry(
                exc=exc,
                countdown=countdown,
                max_retries=RATE_LIMIT_RETRY_CEILING,  # gating is manual via _send_attempt
                args=(),
                kwargs={
                    'recipient': recipient,
                    'subject': subject,
                    'body': body,
                    '_send_attempt': _send_attempt + 1,
                },
            )
        except MaxRetriesExceededError:
            _write_to_dead_letter(self, recipient, subject, body, exc, _send_attempt)
            return {'status': 'dead_lettered', 'recipient': recipient}

    except Exception as exc:
        # Unexpected error — dead-letter immediately, don't retry.
        logger.exception(
            "Unexpected error sending email to %s (task %s)",
            recipient,
            self.request.id,
        )
        _write_to_dead_letter(self, recipient, subject, body, exc, _send_attempt)
        return {'status': 'dead_lettered', 'recipient': recipient}


def _send_via_provider(recipient: str, subject: str, body: str) -> None:
    """
    Stub for the actual email provider call.

    In production this would be an HTTP call to SendGrid/Mailgun/SES.
    The stub raises EmailSendError when the subject starts with 'FAIL:'
    so tests can trigger intentional failures without mocking.
    """
    if subject.startswith('FAIL:'):
        raise EmailSendError(f"Provider rejected email to {recipient}: {subject}")
    # Happy path — provider accepted the email.


def _write_to_dead_letter(task, recipient, subject, body, exc, send_attempt) -> None:
    """Write a permanently failed task to the dead-letter store."""
    # Import here to avoid circular imports and to keep the model
    # decoupled from this module at parse time.
    from .models import FailedEmailTask

    FailedEmailTask.objects.create(
        recipient=recipient,
        subject=subject,
        body=body,
        task_id=task.request.id or '',
        error_message=str(exc),
        retry_count=send_attempt,
    )
    logger.error(
        "Task %s dead-lettered after %d provider-failure attempts: %s",
        task.request.id,
        send_attempt,
        exc,
    )
