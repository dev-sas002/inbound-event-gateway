from __future__ import annotations

from django.contrib import admin, messages
from django.http import HttpRequest

from apps.ingestion.models import RawWebhook
from apps.ingestion.services import ReplayService


@admin.register(RawWebhook)
class RawWebhookAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "vendor",
        "external_event_id",
        "processing_status",
        "retry_count",
        "received_at",
    )
    list_filter = ("vendor", "processing_status", "received_at")
    search_fields = ("id", "external_event_id", "idempotency_key", "vendor", "error_message")
    readonly_fields = ("id", "received_at")
    actions = ["replay_selected"]

    @admin.action(description="Replay selected webhooks")
    def replay_selected(self, request: HttpRequest, queryset):
        # force=True because this is a deliberate human action on rows someone
        # picked by hand. Without it a webhook stuck in PROCESSING — a worker
        # that died mid-task leaves one behind — was silently skipped, and the
        # admin was the only place left to recover it from.
        replayed = 0
        for webhook in queryset:
            if ReplayService.replay(webhook, force=True):
                replayed += 1
        self.message_user(request, f"Replayed {replayed} webhook(s).", level=messages.SUCCESS)
