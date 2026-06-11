from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from django.db import IntegrityError, transaction

from apps.ingestion.models import ProcessingStatus, RawWebhook


@dataclass(frozen=True)
class RawWebhookCreateResult:
    webhook: RawWebhook
    created: bool


class RawWebhookRepository:
    """Repository for raw webhook persistence and retrieval."""

    @staticmethod
    def create_or_get(
        *,
        vendor: str,
        external_event_id: str | None,
        idempotency_key: str,
        raw_payload: Any,
    ) -> RawWebhookCreateResult:
        defaults = {
            "external_event_id": external_event_id,
            "raw_payload": raw_payload,
            "processing_status": ProcessingStatus.RECEIVED,
        }
        try:
            with transaction.atomic():
                webhook, created = RawWebhook.objects.get_or_create(
                    vendor=vendor,
                    idempotency_key=idempotency_key,
                    defaults=defaults,
                )
                return RawWebhookCreateResult(webhook=webhook, created=created)
        except IntegrityError:
            # Two constraints can fire here, and only one of them is the
            # idempotency key. A redelivery that carries the same vendor event
            # id under a *different* key trips uq_vendor_external_event, and
            # looking the row up by key alone would then raise DoesNotExist —
            # turning a duplicate delivery, the case this table exists to make
            # safe, into a 500.
            webhook = RawWebhook.objects.filter(
                vendor=vendor, idempotency_key=idempotency_key
            ).first()
            if webhook is None and external_event_id:
                webhook = RawWebhook.objects.filter(
                    vendor=vendor, external_event_id=external_event_id
                ).first()
            if webhook is None:
                raise
            return RawWebhookCreateResult(webhook=webhook, created=False)

    @staticmethod
    def get_by_id(webhook_id: str) -> RawWebhook:
        return RawWebhook.objects.get(id=webhook_id)
