from django.db import models


class FailedEmailTask(models.Model):
    """
    Dead-letter store for email tasks that have permanently failed.

    A task writes here after exhausting all retries (max_retries reached).
    This is intentionally a simple Django model rather than a Redis list or
    separate queue — it gives us:
      - Persistence across Redis restarts
      - Easy querying/inspection via Django admin or shell
      - A clear audit trail of what failed and why

    In production you'd build a management command or admin action to
    replay these. For this assessment, the model + admin registration
    is the dead-letter mechanism.
    """

    # what we were trying to send
    recipient = models.EmailField()
    subject = models.CharField(max_length=255)
    body = models.TextField()

    # failure bookkeeping
    task_id = models.CharField(
        max_length=255,
        blank=True,
        help_text="Celery task ID of the final failed attempt",
    )
    error_message = models.TextField(
        blank=True,
        help_text="Exception message from the last retry",
    )
    retry_count = models.PositiveIntegerField(
        default=0,
        help_text="Number of times the task was attempted before giving up",
    )

    failed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-failed_at']
        verbose_name = 'Failed Email Task'
        verbose_name_plural = 'Failed Email Tasks'

    def __str__(self):
        return f"[FAILED] {self.subject} → {self.recipient} (attempts: {self.retry_count})"
