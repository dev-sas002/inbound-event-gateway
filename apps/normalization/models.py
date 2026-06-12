from __future__ import annotations

from django.db import models
from django.db.models import Q


class EntityType(models.TextChoices):
    SHIPMENT = "SHIPMENT", "Shipment"
    INVOICE = "INVOICE", "Invoice"
    UNCLASSIFIED = "UNCLASSIFIED", "Unclassified"


class ShipmentStatus(models.TextChoices):
    PICKED_UP = "PICKED_UP", "Picked Up"
    IN_TRANSIT = "IN_TRANSIT", "In Transit"
    OUT_FOR_DELIVERY = "OUT_FOR_DELIVERY", "Out For Delivery"
    DELIVERED = "DELIVERED", "Delivered"


class InvoiceStatus(models.TextChoices):
    ISSUED = "ISSUED", "Issued"
    PAID = "PAID", "Paid"
    VOIDED = "VOIDED", "Voided"
    REFUNDED = "REFUNDED", "Refunded"


class ReviewDecision(models.TextChoices):
    APPROVE = "approve", "Approved"
    REJECT = "reject", "Rejected"


class NormalizedEvent(models.Model):
    webhook = models.OneToOneField(
        "ingestion.RawWebhook",
        on_delete=models.CASCADE,
        related_name="normalized_event",
    )
    entity_type = models.CharField(max_length=32, choices=EntityType.choices, db_index=True)
    entity_id = models.CharField(max_length=255, db_index=True)
    canonical_status = models.CharField(max_length=64, db_index=True)
    event_time = models.DateTimeField(db_index=True)
    normalized_payload = models.JSONField()
    confidence_score = models.FloatField()
    llm_model = models.CharField(max_length=128)
    prompt_version = models.CharField(max_length=32)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    #: Set when the normaliser's confidence fell below the configured
    #: threshold. Such an event is still recorded in full, but is deliberately
    #: not allowed to update entity state — an unverified guess must not
    #: silently become the system's belief about a shipment or an invoice.
    requires_review = models.BooleanField(default=False, db_index=True)
    review_reason = models.CharField(max_length=255, blank=True, default="")
    reviewed_at = models.DateTimeField(null=True, blank=True)
    #: What the reviewer actually decided. Recorded as a column rather than
    #: parsed back out of review_reason, because calibration compares approval
    #: rates against confidence and a prose audit note is not a data source.
    review_decision = models.CharField(
        max_length=16, choices=ReviewDecision.choices, blank=True, default=""
    )

    class Meta:
        ordering = ["-event_time"]
        indexes = [
            models.Index(
                fields=["entity_type", "entity_id", "event_time"], name="norm_entity_event_idx"
            ),
            models.Index(fields=["canonical_status", "created_at"], name="norm_status_created_idx"),
            # The review queue is the one query a human waits on, and it is
            # always the same shape: held, undecided, oldest first. A partial
            # index over exactly that predicate stays small no matter how many
            # events have been auto-applied.
            models.Index(
                fields=["created_at"],
                name="norm_review_pending_idx",
                condition=Q(requires_review=True, reviewed_at__isnull=True),
            ),
            # Supports the threshold sweep used by the low-confidence view and
            # by calibration, which scans a confidence band rather than a point.
            models.Index(fields=["confidence_score", "created_at"], name="norm_confidence_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.entity_type}:{self.entity_id}:{self.canonical_status}"


class ProfileSource(models.TextChoices):
    """Where a vendor profile came from, which is also how much to trust it."""

    MANUAL = "MANUAL", "Hand written"
    DISCOVERED = "DISCOVERED", "Discovered from samples"


class VendorProfile(models.Model):
    """
    What this service has learned about one vendor's payload shape.

    Without a profile the normaliser has to guess which key holds the
    identifier and what the vendor's status vocabulary means, and it scores
    itself down accordingly. A profile turns those guesses into lookups, which
    is why a profiled vendor clears the confidence gate and an unprofiled one
    often does not.

    Profiles are data rather than code so that onboarding a vendor is a
    configuration change — see `apps.normalization.discovery`, which proposes
    one from sample payloads.
    """

    vendor = models.CharField(max_length=128, unique=True)
    entity_type = models.CharField(
        max_length=32,
        choices=EntityType.choices,
        blank=True,
        default="",
        help_text="Forced entity type, or blank to let the normaliser classify.",
    )
    #: Flattened payload paths, most authoritative first. A path is matched
    #: whole ("data.shipment.id") or by its last segment ("id").
    id_paths = models.JSONField(default=list, blank=True)
    status_paths = models.JSONField(default=list, blank=True)
    time_paths = models.JSONField(default=list, blank=True)
    #: Vendor status token (normalised to lowercase_underscore) -> canonical
    #: status. A token mapped to null is one discovery saw but could not
    #: interpret; it stays unmapped so the gate holds those events.
    status_map = models.JSONField(default=dict, blank=True)
    source = models.CharField(
        max_length=16, choices=ProfileSource.choices, default=ProfileSource.MANUAL
    )
    sample_count = models.PositiveIntegerField(default=0)
    notes = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["vendor"]

    def __str__(self) -> str:
        return f"{self.vendor} ({self.source})"
