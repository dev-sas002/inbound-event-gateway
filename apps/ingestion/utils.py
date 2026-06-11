from __future__ import annotations

import hashlib
import json
from typing import Any

KNOWN_EVENT_ID_KEYS = (
    "event_id",
    "eventId",
    "external_event_id",
    "externalEventId",
    "id",
    "message_id",
)


def extract_external_event_id(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    for key in KNOWN_EVENT_ID_KEYS:
        value = payload.get(key)
        if value is not None and str(value).strip():
            # The column is a varchar(255); an over-long vendor id would make
            # PostgreSQL reject the insert outright.
            return str(value)[:255]
    return None


#: RawWebhook.idempotency_key is a varchar(255); a long vendor name plus a long
#: vendor event id overflows it, and PostgreSQL raises rather than truncating.
#: Hashing the overflow keeps the key both unique and in range.
MAX_IDEMPOTENCY_KEY_LENGTH = 255


def _fit(key: str) -> str:
    if len(key) <= MAX_IDEMPOTENCY_KEY_LENGTH:
        return key
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return f"{key[: MAX_IDEMPOTENCY_KEY_LENGTH - len(digest) - 1]}:{digest}"


def build_idempotency_key(
    vendor: str,
    payload: Any,
    external_event_id: str | None,
    supplied_key: str | None = None,
) -> str:
    """
    The key a redelivery must collide with, most authoritative source first.

    A key the vendor sent itself is the strongest statement about which
    deliveries are the same event; the vendor's event id is next; hashing the
    payload is the last resort, and the weakest, because two distinct events
    can serialise identically.
    """
    if supplied_key and supplied_key.strip():
        return _fit(f"header:{vendor}:{supplied_key.strip()}")
    if external_event_id:
        return _fit(f"external:{vendor}:{external_event_id}")
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    return _fit(f"payload:{vendor}:{digest}")
