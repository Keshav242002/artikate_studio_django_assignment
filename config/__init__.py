from .celery import app as celery_app  # noqa: F401 — ensures celery app is loaded when Django starts

__all__ = ('celery_app',)
