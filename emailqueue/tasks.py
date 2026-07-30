"""
Section 2 — Celery email task with exponential backoff and dead-letter handling.

Flow:
  1. Task is submitted to the broker.
  2. Worker picks it up and checks the rate limiter (Redis sliding window).
  3. If rate limit exceeded → raise RateLimitExceeded, Celery retries with
     a short delay (not exponential — we want to retry soon, not give up).
  4. If a transient failure (e.g. SMTP error) → retry with exponential backoff.
  5. If max_retries reached → write to FailedEmailTask (dead-letter) and stop.

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


class EmailSendError(Exception):
    """Raised when the email provider returns a transient error."""
    pass


@shared_task(
    bind=True,
    # Retry up to 4 times (5 total attempts).
    max_retries=4,
    # acks_late and reject_on_worker_lost are set globally in settings.
    # Documented here for clarity:
    #   acks_late=True               — message acked only after task returns
    #   task_reject_on_worker_lost=True — SIGKILL causes broker to requeue
)
def send_email(self, recipient: str, subject: str, body: str) -> dict:
    """
    Send a transactional email, respecting the 200/min rate limit.

    Args:
        recipient:  Destination email address.
        subject:    Email subject line.
        body:       Plain-text email body.

    Returns:
        dict with status and metadata on success.

    Raises:
        Retry on RateLimitExceeded (short delay, not exponential).
        Retry with exponential backoff on EmailSendError.
        Writes to FailedEmailTask on MaxRetriesExceededError.
    """
    # --- 1. Rate limit check ---
    try:
        _rate_limiter.acquire()
    except RateLimitExceeded:
        # Rate limit hit. Retry after a short fixed delay (5s).
        # We don't use exponential backoff here — the point is to retry
        # quickly once the window slides, not to give up.
        logger.warning(
            "Rate limit exceeded for send_email task %s, retrying in 5s",
            self.request.id,
        )
        raise self.retry(exc=RateLimitExceeded(), countdown=5)

    # --- 2. Send the email ---
    try:
        _send_via_provider(recipient, subject, body)
        logger.info("Email sent to %s (task %s)", recipient, self.request.id)
        return {
            'status': 'sent',
            'recipient': recipient,
            'task_id': self.request.id,
            'retries': self.request.retries,
        }

    except EmailSendError as exc:
        # Transient failure — retry with exponential backoff.
        # countdown doubles each attempt: 1s, 2s, 4s, 8s, 16s
        countdown = 2 ** self.request.retries
        logger.warning(
            "EmailSendError for %s (attempt %d/%d), retrying in %ds: %s",
            recipient,
            self.request.retries + 1,
            self.max_retries + 1,
            countdown,
            exc,
        )
        try:
            raise self.retry(exc=exc, countdown=countdown)
        except MaxRetriesExceededError:
            _write_to_dead_letter(self, recipient, subject, body, exc)
            return {'status': 'dead_lettered', 'recipient': recipient}
        except Exception as retry_exc:
            # Catch Retry (which self.retry raises even on final attempt in eager mode)
            if self.request.retries >= self.max_retries:
                _write_to_dead_letter(self, recipient, subject, body, exc)
                return {'status': 'dead_lettered', 'recipient': recipient}
            raise retry_exc

    except Exception as exc:
        # Unexpected error — dead-letter immediately, don't retry.
        logger.exception(
            "Unexpected error sending email to %s (task %s)",
            recipient,
            self.request.id,
        )
        _write_to_dead_letter(self, recipient, subject, body, exc)
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


def _write_to_dead_letter(task, recipient, subject, body, exc) -> None:
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
        retry_count=task.request.retries,
    )
    logger.error(
        "Task %s dead-lettered after %d retries: %s",
        task.request.id,
        task.request.retries,
        exc,
    )
