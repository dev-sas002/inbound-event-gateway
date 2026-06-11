"""
Rule-based normalisation. The default when no OpenAI key is configured.

The service exists to turn each vendor's idiosyncratic webhook shape into one
canonical event. A model does that well because the space of vendor shapes is
open-ended. But the *pipeline* around it — idempotent ingestion, the state
machine, retries, the confidence gate, entity state — is the part worth being
able to run, test and demonstrate, and none of it should require a billable
key.

So this reads the same payloads with explicit rules, in two tiers:

1. If the vendor has a profile (`apps.normalization.vendors`), use it. A
   profile says where the identifier and status live and what the vendor's
   status words mean, so the reading is a lookup rather than a guess and the
   confidence says so.
2. Otherwise fall back to heuristics: look for an identifier under any of the
   names vendors actually use, map their status vocabulary onto the canonical
   one, and report a genuine confidence based on how much it had to guess.

Where a model would infer, this abstains — an unrecognised status yields low
confidence and is routed for review rather than being asserted.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from apps.normalization.backends.base import NormalizationResponse
from apps.normalization.exceptions import NormalizationError
from apps.normalization.models import EntityType, InvoiceStatus, ShipmentStatus
from apps.normalization.schemas import NormalizationResult
from apps.normalization.vendors import VendorProfileSpec, profile_for

logger = logging.getLogger("apps.normalization")

PROMPT_VERSION = "rules-v1"
MODEL_NAME = "rules-based-normalizer"

# fmt: off
# The vendor vocabularies below are laid out by hand and the formatter is
# told to leave them alone. Grouping is meaning here: the status tables are
# grouped by the canonical value each row collapses onto, and the key lists
# by which kind of key they are. Exploded one-string-per-line, a reviewer
# checking whether "dispatched" belongs with IN_TRANSIT would be reading a
# 120-line column instead of a table.
#: Keys vendors use for the thing being tracked, most specific first.
ID_KEYS = (
    "shipment_id", "shipmentId", "tracking_number", "trackingNumber", "tracking_id",
    "shipment_reference", "shipmentReference", "container_no", "containerNo",
    "container_number", "containerNumber",
    "invoice_id", "invoiceId", "invoice_number", "invoiceNumber",
    "reference", "ref", "order_id", "orderId", "id", "uuid",
)

TIME_KEYS = (
    "event_time", "eventTime", "occurred_at", "occurredAt", "timestamp", "ts",
    "created_at", "createdAt", "updated_at", "updatedAt", "date",
)

# Ordered by how directly the key names a status. An explicit "status" field
# always wins; "type" sits near the end because it often carries a dotted event
# name ("invoice.payment_succeeded") rather than a bare status; and the free
# text fields are last resorts. Reading a status out of prose is safe here
# precisely because unrecognised vocabulary scores low and is held rather than
# asserted — the cost of looking is a review, not a wrong fact.
STATUS_KEYS = (
    "status", "state", "status_text", "statusText",
    "event", "event_type", "eventType", "phase", "lifecycle",
    "event_description", "eventDescription", "code", "type",
    "message", "description",
)

#: Vendor vocabulary → canonical status. Deliberately explicit: a fuzzy match
#: here would produce confident wrong answers, which is worse than abstaining.
# Maps only onto statuses the model actually defines. The canonical vocabulary
# is deliberately narrow, so several vendor words collapse onto one value —
# which is the point of normalising in the first place.
SHIPMENT_STATUS = {
    "picked_up": "PICKED_UP", "pickup": "PICKED_UP", "collected": "PICKED_UP",
    "in_transit": "IN_TRANSIT", "intransit": "IN_TRANSIT", "transit": "IN_TRANSIT",
    "shipped": "IN_TRANSIT", "dispatched": "IN_TRANSIT", "departed": "IN_TRANSIT",
    "out_for_delivery": "OUT_FOR_DELIVERY", "outfordelivery": "OUT_FOR_DELIVERY",
    "delivered": "DELIVERED", "completed": "DELIVERED", "arrived": "DELIVERED",
}

INVOICE_STATUS = {
    "issued": "ISSUED", "open": "ISSUED", "sent": "ISSUED", "pending": "ISSUED",
    "created": "ISSUED", "draft": "ISSUED",
    "paid": "PAID", "settled": "PAID", "payment_succeeded": "PAID",
    "voided": "VOIDED", "void": "VOIDED", "cancelled": "VOIDED", "canceled": "VOIDED",
    "refunded": "REFUNDED", "refund": "REFUNDED", "chargeback": "REFUNDED",
}

#: Identifier keys that name what is being identified. Finding the id under
#: one of these is far stronger evidence than any word in the payload: a
#: `container_no` is a container, whatever else the body happens to say.
SHIPMENT_ID_KEYS = frozenset(
    {
        "shipment_id", "shipmentid", "tracking_number", "trackingnumber", "tracking_id",
        "shipment_reference", "shipmentreference", "container_no", "containerno",
        "container_number", "containernumber",
    }
)
INVOICE_ID_KEYS = frozenset(
    {"invoice_id", "invoiceid", "invoice_number", "invoicenumber"}
)

#: Awarded when the identifier's own key names the entity type.
ID_KEY_CONFIDENCE = 0.9

#: Words that signal which kind of thing the payload is about. Used only when
#: the identifier key was a generic one ("id", "reference"), where there is
#: nothing better to go on.
SHIPMENT_HINTS = (
    "shipment", "tracking", "carrier", "parcel", "delivery", "freight",
    "container", "vessel", "voyage", "cargo", "consignment", "waybill", "courier",
)
INVOICE_HINTS = (
    "invoice", "billing", "payment", "amount_due", "charge", "remittance", "payable",
)

#: Keys that name the *sender* rather than describe the event. Excluded from
#: the hint scan: a company called "GlobalFreightPay" is not evidence that its
#: payload is about freight, and `carrier` — a hint word in its own right —
#: appears as a key on payloads of every kind.
VENDOR_NAMING_KEYS = frozenset({"vendor", "source", "provider", "carrier"})

# fmt: on

#: Confidence awarded when a vendor profile answered outright. Below 1.0 on
#: purpose: a profile is still an inference about the vendor, just a recorded
#: and reviewable one.
PROFILE_CONFIDENCE = 0.97


def flatten_payload(payload: Any, prefix: str = "") -> dict[str, Any]:
    """
    Flatten nested objects so a key can be found at any depth.

    Both the dotted path and the bare leaf name are indexed, so a profile can
    pin `data.shipment.tracking_number` exactly while the heuristics can still
    find `tracking_number` wherever it happens to sit.
    """
    out: dict[str, Any] = {}
    if isinstance(payload, dict):
        for key, value in payload.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if isinstance(value, (dict, list)):
                out.update(flatten_payload(value, path))
            else:
                out[path] = value
                out.setdefault(str(key), value)
    elif isinstance(payload, list):
        for index, item in enumerate(payload):
            out.update(flatten_payload(item, f"{prefix}[{index}]"))
    return out


def first_entry(flat: dict[str, Any], keys: Iterable[str]) -> tuple[str | None, Any]:
    """The first of `keys` present in the payload, as (key, value)."""
    keys = tuple(keys)
    for key in keys:
        if key in flat and flat[key] not in (None, ""):
            return key, flat[key]
    # Fall back to a suffix match, so `data.shipment.tracking_number` is found.
    for key in keys:
        for flat_key, value in flat.items():
            if flat_key.split(".")[-1] == key and value not in (None, ""):
                return key, value
    return None, None


def first_value(flat: dict[str, Any], keys: Iterable[str]) -> Any:
    return first_entry(flat, keys)[1]


def parse_timestamp(raw: Any) -> datetime | None:
    """A vendor timestamp as an aware datetime, or None if it is not one."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        # Heuristic: values past this magnitude are milliseconds.
        seconds = float(raw) / 1000 if float(raw) > 1e11 else float(raw)
        try:
            return datetime.fromtimestamp(seconds, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(raw).strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def status_token(raw: Any) -> str:
    """The vendor's status word, reduced to a comparable token."""
    return re.sub(r"[^a-z0-9]+", "_", str(raw).strip().lower()).strip("_")


class RuleBasedNormalizer:
    """
    Same interface as the OpenAI backend, no network.

    Returning the identical response shape is what lets the Celery task stay
    unchanged: it asks for a normaliser and calls it.
    """

    def normalize(self, payload: Any, *, vendor: str = "") -> NormalizationResponse:
        profile = profile_for(vendor)
        flat = flatten_payload(payload)

        id_key, entity_id = self._entity_id(flat, profile)
        entity_type, type_confidence = self._classify(flat, id_key, profile)
        raw_status = first_value(flat, self._status_keys(profile))
        canonical_status, status_confidence = self._status(entity_type, raw_status, profile)
        event_time, time_confidence, time_found = self._event_time(flat, profile)

        if not entity_id:
            # Without an identifier there is nothing to key state on, so this
            # is a hard failure rather than a low-confidence guess.
            raise NormalizationError("No identifier found in payload; cannot normalise without one")

        # The weakest signal governs. Averaging would let a confident guess at
        # one field hide a blind guess at another.
        confidence = round(min(type_confidence, status_confidence, time_confidence), 3)

        result = NormalizationResult.from_dict(
            {
                "entity_type": entity_type,
                "entity_id": entity_id,
                "canonical_status": canonical_status,
                "event_time": event_time.isoformat(),
                "confidence_score": confidence,
                "normalized_payload": {
                    "source_status": raw_status,
                    # What was actually read out of the payload, not a fixed
                    # list: this is the only record of how much the normaliser
                    # had to fall back on, and a constant here claimed every
                    # field had been found even when none had.
                    "fields_found": sorted(
                        name
                        for name, found in (
                            ("entity_id", bool(entity_id)),
                            ("status", raw_status is not None),
                            ("event_time", time_found),
                        )
                        if found
                    ),
                    "normalizer": "rules",
                    # Which profile, if any, did the reading — so a reviewer
                    # can tell a looked-up answer from a guessed one.
                    "vendor_profile": profile.vendor if profile else None,
                },
            }
        )
        logger.info(
            "normalized_locally",
            extra={
                "entity_type": entity_type,
                "vendor": vendor,
                "confidence": confidence,
            },
        )
        return NormalizationResponse(
            normalized=result,
            llm_model=MODEL_NAME,
            prompt_version=PROMPT_VERSION,
        )

    # -- profile-aware key selection --------------------------------------

    @staticmethod
    def _id_keys(profile: VendorProfileSpec | None) -> tuple[str, ...]:
        return (*profile.id_paths, *ID_KEYS) if profile else ID_KEYS

    @staticmethod
    def _status_keys(profile: VendorProfileSpec | None) -> tuple[str, ...]:
        return (*profile.status_paths, *STATUS_KEYS) if profile else STATUS_KEYS

    @staticmethod
    def _time_keys(profile: VendorProfileSpec | None) -> tuple[str, ...]:
        return (*profile.time_paths, *TIME_KEYS) if profile else TIME_KEYS

    # -- field extraction -------------------------------------------------

    def _classify(
        self, flat: dict[str, Any], id_key: str | None, profile: VendorProfileSpec | None
    ) -> tuple[str, float]:
        if profile and profile.entity_type:
            return profile.entity_type, PROFILE_CONFIDENCE

        # The key the identifier was found under, first. Counting domain words
        # in the whole payload is a much weaker signal, and it used to be the
        # only one: a carrier's shipment event that never said "shipment" came
        # out UNCLASSIFIED and was held, every single time.
        leaf = (id_key or "").split(".")[-1].lower()
        if leaf in SHIPMENT_ID_KEYS:
            return EntityType.SHIPMENT, ID_KEY_CONFIDENCE
        if leaf in INVOICE_ID_KEYS:
            return EntityType.INVOICE, ID_KEY_CONFIDENCE

        blob = self._hint_blob(flat)
        shipment = sum(1 for hint in SHIPMENT_HINTS if hint in blob)
        invoice = sum(1 for hint in INVOICE_HINTS if hint in blob)

        if shipment > invoice and shipment > 0:
            return EntityType.SHIPMENT, min(0.95, 0.6 + 0.1 * shipment)
        if invoice > shipment and invoice > 0:
            return EntityType.INVOICE, min(0.95, 0.6 + 0.1 * invoice)
        # Ambiguous or unrecognised: say so rather than picking one.
        return EntityType.UNCLASSIFIED, 0.2

    @staticmethod
    def _hint_blob(flat: dict[str, Any]) -> str:
        return " ".join(
            f"{key} {value}"
            for key, value in flat.items()
            if key.split(".")[-1].lower() not in VENDOR_NAMING_KEYS
        ).lower()

    def _entity_id(
        self, flat: dict[str, Any], profile: VendorProfileSpec | None
    ) -> tuple[str | None, str]:
        key, value = first_entry(flat, self._id_keys(profile))
        return key, (str(value).strip() if value is not None else "")

    def _status(
        self, entity_type: str, raw: Any, profile: VendorProfileSpec | None
    ) -> tuple[str, float]:
        if entity_type == EntityType.UNCLASSIFIED:
            return "UNKNOWN", 0.2
        table = SHIPMENT_STATUS if entity_type == EntityType.SHIPMENT else INVOICE_STATUS
        # The least-committal value of each vocabulary, used only alongside a
        # low confidence score so it is routed for review rather than trusted.
        default = (
            ShipmentStatus.IN_TRANSIT
            if entity_type == EntityType.SHIPMENT
            else InvoiceStatus.ISSUED
        )
        if raw is None:
            return default, 0.25

        key = status_token(raw)
        # The vendor's own vocabulary first: a profile is a recorded statement
        # about what this sender's words mean, and it outranks the generic
        # table, which only knows what words usually mean.
        if profile and key in profile.status_map:
            return profile.status_map[key], PROFILE_CONFIDENCE
        if key in table:
            return table[key], 0.9
        # Vendors namespace their event names: "invoice.payment_succeeded",
        # "parcel.out_for_delivery". Try every suffix, longest first, so
        # "out_for_delivery" is preferred over the bare "delivery" — matching
        # the shortest suffix first would map a compound status onto whichever
        # single word happened to appear in the table.
        parts = key.split("_")
        for start in range(1, len(parts)):
            suffix = "_".join(parts[start:])
            if profile and suffix in profile.status_map:
                return profile.status_map[suffix], PROFILE_CONFIDENCE
            if suffix in table:
                # Slightly lower than an exact match: the prefix was discarded
                # unread, so there is marginally more inference involved.
                return table[suffix], 0.75
        # Unrecognised vocabulary. Low confidence routes this for review
        # instead of asserting a status nobody verified.
        return default, 0.3

    def _event_time(
        self, flat: dict[str, Any], profile: VendorProfileSpec | None
    ) -> tuple[datetime, float, bool]:
        """Returns (time, confidence, whether the payload supplied it)."""
        # A path the profile named outranks a lucky hit on a generic key name:
        # one is a recorded fact about the vendor, the other is a convention.
        if profile and profile.time_paths:
            parsed = parse_timestamp(first_value(flat, profile.time_paths))
            if parsed is not None:
                return parsed, PROFILE_CONFIDENCE, True

        raw = first_value(flat, self._time_keys(profile))
        if raw is not None:
            parsed = parse_timestamp(raw)
            if parsed is not None:
                return parsed, 0.95, True
        # No usable timestamp: record arrival time, and say the confidence is
        # low because the event's real time is unknown.
        return datetime.now(UTC), 0.4, False
