"""
Turning an HTTP request into something the ingestion service can act on.

Which header names a vendor, which one carries the retry key, and what counts
as a payload at all are rules about *webhooks*, not about Django. They lived in
the view, where they could only be exercised through a request factory and
could not be reused by the replay path or by a future transport. They live here
instead, and the view is left doing what a view should: read, delegate, answer.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from django.http import HttpRequest

#: Vendors send the retry-safety key under this header; the column it lands in
#: is unique per (vendor, key), which is what makes a redelivery a no-op.
IDEMPOTENCY_HEADER = "Idempotency-Key"
VENDOR_HEADER = "X-Webhook-Vendor"

#: Payload keys that name the sender, in descending order of how explicitly
#: they do so.
VENDOR_PAYLOAD_KEYS = ("vendor", "source", "provider", "carrier")

UNKNOWN_VENDOR = "unknown"

MAX_VENDOR_LENGTH = 128
MAX_IDEMPOTENCY_HEADER_LENGTH = 255


class InvalidPayload(ValueError):
    """The request body is not something that can be normalised."""


@dataclass(frozen=True)
class InboundWebhook:
    """One delivery, read out of the request and ready to store."""

    vendor: str
    payload: dict[str, Any]
    raw_body: bytes
    idempotency_key: str | None
    signature: str | None


def resolve_vendor(headers, payload: Any) -> str:
    """
    Who sent this.

    The header wins because it is a statement by the transport rather than an
    inference from the body, and a vendor that bothers to set it is telling us
    something the payload may not.
    """
    header_value = (headers.get(VENDOR_HEADER) or "").strip()
    if header_value:
        return header_value[:MAX_VENDOR_LENGTH]
    if isinstance(payload, dict):
        for key in VENDOR_PAYLOAD_KEYS:
            value = payload.get(key)
            if value is not None and str(value).strip():
                return str(value).strip()[:MAX_VENDOR_LENGTH]
    return UNKNOWN_VENDOR


def resolve_idempotency_key(headers) -> str | None:
    """
    The vendor's own retry key, when it sends one.

    A vendor that retries a delivery repeats this header, so honouring it is
    what makes the retry a no-op even if the body differs by a field the vendor
    added on retry. Without it the key is derived from the payload, which
    collapses two genuinely distinct events that happen to serialise
    identically.
    """
    value = headers.get(IDEMPOTENCY_HEADER)
    if value is None:
        return None
    value = value.strip()
    return value[:MAX_IDEMPOTENCY_HEADER_LENGTH] if value else None


def read_request(
    request: HttpRequest, payload: Any, raw_body: bytes, signature_header: str
) -> InboundWebhook:
    """
    Build an InboundWebhook, or refuse the request.

    `raw_body` is passed in rather than read from `request` here, and that is
    not a style choice. The signature covers the bytes the vendor actually
    sent, and once the request stream has been parsed — which is exactly what
    producing `payload` does — Django refuses to hand the body back
    (`RawPostDataException`) on any real WSGI server. Taking the bytes as an
    argument makes the caller read them first, and makes it impossible to get
    the order wrong in here.
    """
    # An empty body or a bare JSON scalar carries nothing to normalise, and
    # storing it only defers the failure to a worker. Refuse it here, where
    # the vendor can still see why.
    if not isinstance(payload, dict) or not payload:
        raise InvalidPayload("A non-empty JSON object payload is required.")

    return InboundWebhook(
        vendor=resolve_vendor(request.headers, payload),
        payload=payload,
        raw_body=raw_body,
        idempotency_key=resolve_idempotency_key(request.headers),
        signature=request.headers.get(signature_header),
    )
