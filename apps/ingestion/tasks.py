from __future__ import annotations

import logging

from celery import shared_task

from apps.ingestion.models import ProcessingStatus, RawWebhook
from apps.ingestion.services import ReplayService

logger = logging.getLogger("apps.ingestion")


@shared_task
def replay_failed_webhooks(limit: int = 100) -> int:
    """Replay a bounded batch of failed/dead-letter webhooks."""
    queryset = RawWebhook.objects.filter(
        processing_status__in=[ProcessingStatus.FAILED, ProcessingStatus.DEAD_LETTER]
    ).order_by("received_at")[:limit]

    replayed = 0
    for webhook in queryset:
        if ReplayService.replay(webhook, force=True):
            replayed += 1

    logger.info("bulk_replay_executed", extra={"replayed": replayed})
    return replayed
