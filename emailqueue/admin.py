from django.contrib import admin
from .models import FailedEmailTask


@admin.register(FailedEmailTask)
class FailedEmailTaskAdmin(admin.ModelAdmin):
    list_display = ['recipient', 'subject', 'retry_count', 'failed_at', 'task_id']
    list_filter = ['failed_at']
    search_fields = ['recipient', 'subject', 'task_id']
    readonly_fields = ['task_id', 'error_message', 'retry_count', 'failed_at']
