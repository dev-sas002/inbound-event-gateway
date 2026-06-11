from __future__ import annotations

import uuid

from django.db import models
from django.db.models import Q


class ProcessingStatus(models.TextChoices):
    RECEIVED = "RECEIVED", "Received"
    PROCESSING = "PROCESSING", "Processing"
    NORMALIZED = "NORMALIZED", "Normalized"
    FAILED = "FAILED", "Failed"
    DEAD_LETTER = "DEAD_LETTER", "Dead Letter"


class RawWebhook(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    vendor = models.CharField(max_length=128, db_index=True)
    # null=True, not blank="": the partial unique constraint below is
    # conditioned on external_event_id IS NOT NULL, so that vendors who send no
    # event id are not all collapsed onto one "" row. DJ001 objects to nullable
    # char fields in general; here the null is load-bearing.
    external_event_id = models.CharField(max_length=255, blank=True, null=True)  # noqa: DJ001
    idempotency_key = models.CharField(max_length=255)
    raw_payload = models.JSONField()
    processing_status = models.CharField(
        max_length=32,
        choices=ProcessingStatus.choices,
        default=ProcessingStatus.RECEIVED,
        db_index=True,
    )
    retry_count = models.PositiveIntegerField(default=0)
    error_message = models.TextField(blank=True)
    received_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-received_at"]
        indexes = [
            models.Index(fields=["vendor", "received_at"], name="raw_vendor_received_idx"),
            models.Index(fields=["external_event_id"], name="raw_ext_event_idx"),
            models.Index(
                fields=["processing_status", "received_at"], name="raw_status_received_idx"
            ),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["vendor", "idempotency_key"], name="uq_vendor_idempotency_key"
            ),
            models.UniqueConstraint(
                fields=["vendor", "external_event_id"],
                name="uq_vendor_external_event",
                condition=Q(external_event_id__isnull=False),
            ),
        ]

    def __str__(self) -> str:
        return f"{self.vendor}:{self.id}"
