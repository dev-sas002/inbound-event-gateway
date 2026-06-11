from __future__ import annotations

import logging
from time import perf_counter
from typing import Any

from django.conf import settings
from django.db import transaction

from apps.ingestion.models import ProcessingStatus, RawWebhook
from apps.ingestion.repositories import RawWebhookCreateResult, RawWebhookRepository
from apps.ingestion.routing import route_for
from apps.ingestion.utils import build_idempotency_key, extract_external_event_id
from apps.normalization.tasks import process_raw_webhook

logger = logging.getLogger("apps.ingestion")


class IngestionService:
    """Handle raw webhook acceptance and async dispatch."""

    @staticmethod
    def ingest(
        payload: Any, vendor: str, idempotency_key: str | None = None
    ) -> RawWebhookCreateResult:
        start = perf_counter()
        external_event_id = extract_external_event_id(payload)
        idempotency_key = build_idempotency_key(
            vendor=vendor,
            payload=payload,
            external_event_id=external_event_id,
            supplied_key=idempotency_key,
        )

        result = RawWebhookRepository.create_or_get(
            vendor=vendor,
            external_event_id=external_event_id,
            idempotency_key=idempotency_key,
            raw_payload=payload,
        )

        queue = ""
        if result.created:
            # Routed per vendor so one vendor's burst cannot monopolise the
            # worker pool; see apps.ingestion.routing.
            queue = route_for(vendor)
            webhook_id = str(result.webhook.id)
            # on_commit, so a task can never start before the row it reads is
            # visible to the worker's connection.
            transaction.on_commit(
                lambda: process_raw_webhook.apply_async(args=[webhook_id], queue=queue)
            )

        duration_ms = (perf_counter() - start) * 1000
        logger.info(
            "webhook_accepted",
            extra={
                "webhook_id": str(result.webhook.id),
                "vendor": vendor,
                "duration_ms": round(duration_ms, 2),
                "processing_status": result.webhook.processing_status,
                "queue": queue,
                "duplicate": not result.created,
            },
        )
        return result


class ReplayService:
    """
    Re-run normalisation over a payload that was already stored.

    Replays are operator-initiated backfill, never live traffic, so they go to
    the bulk lane: a thousand-row replay must not push a vendor's live webhooks
    behind it.
    """

    @staticmethod
    def replay(webhook: RawWebhook, *, force: bool = False) -> bool:
        in_flight = {ProcessingStatus.PROCESSING, ProcessingStatus.RECEIVED}
        if webhook.processing_status in in_flight and not force:
            return False
        webhook.processing_status = ProcessingStatus.RECEIVED
        webhook.error_message = ""
        webhook.save(update_fields=["processing_status", "error_message"])
        webhook_id = str(webhook.id)
        queue = settings.NORMALIZATION_BULK_QUEUE
        transaction.on_commit(
            lambda: process_raw_webhook.apply_async(args=[webhook_id], queue=queue)
        )
        return True
