from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from apps.normalization.models import EntityType, InvoiceStatus, ShipmentStatus

_ALLOWED_STATUSES = {
    EntityType.SHIPMENT: set(ShipmentStatus.values),
    EntityType.INVOICE: set(InvoiceStatus.values),
    EntityType.UNCLASSIFIED: {"UNKNOWN"},
}


def allowed_statuses(entity_type: str) -> set[str]:
    """The canonical statuses one entity type may take. Empty for an unknown type."""
    return set(_ALLOWED_STATUSES.get(str(entity_type).upper(), set()))


@dataclass(frozen=True)
class NormalizationResult:
    entity_type: str
    entity_id: str
    canonical_status: str
    event_time: datetime
    confidence_score: float
    normalized_payload: dict[str, Any]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> NormalizationResult:
        entity_type = str(data["entity_type"]).upper()
        if entity_type not in set(EntityType.values):
            raise ValueError("Invalid entity_type")

        canonical_status = str(data["canonical_status"]).upper()
        if canonical_status not in _ALLOWED_STATUSES[entity_type]:
            raise ValueError("Invalid canonical_status for entity_type")

        entity_id = str(data["entity_id"]).strip()
        if not entity_id:
            raise ValueError("entity_id is required")

        event_time_raw = str(data["event_time"]).strip()
        event_time = datetime.fromisoformat(event_time_raw.replace("Z", "+00:00"))
        if event_time.tzinfo is None:
            raise ValueError("event_time must include timezone")

        confidence_score = float(data["confidence_score"])
        if confidence_score < 0.0 or confidence_score > 1.0:
            raise ValueError("confidence_score must be between 0 and 1")

        normalized_payload_raw = data.get("normalized_payload") or {}
        if not isinstance(normalized_payload_raw, dict):
            raise ValueError("normalized_payload must be an object")

        return cls(
            entity_type=entity_type,
            entity_id=entity_id,
            canonical_status=canonical_status,
            event_time=event_time,
            confidence_score=confidence_score,
            normalized_payload=normalized_payload_raw,
        )
