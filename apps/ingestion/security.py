"""
Per-vendor webhook signature verification.

Real vendors sign their payloads; accepting an unsigned body means anyone who
learns the URL can write into entity state. Verification is opt-in per vendor
so the service still runs with no configuration at all: a vendor with no
configured secret is accepted as before, which keeps the offline demo working,
and `WEBHOOK_REQUIRE_SIGNATURE=true` turns that permissiveness off for a
deployment that has finished onboarding its vendors.

The signature is an HMAC-SHA256 over the exact bytes of the request body,
compared in constant time. It is computed over the raw body rather than the
parsed payload because re-serialising JSON does not round-trip byte for byte.
"""

from __future__ import annotations

import hashlib
import hmac

from django.conf import settings

SIGNATURE_HEADER = "X-Webhook-Signature"

#: Accepted prefixes for the header value. Vendors differ on whether they
#: prefix the digest with the algorithm; both forms are the same digest.
_PREFIXES = ("sha256=", "")


class SignatureError(Exception):
    """The request could not be authenticated as coming from the vendor."""


def _secrets() -> dict[str, str]:
    configured = getattr(settings, "WEBHOOK_SIGNING_SECRETS", {}) or {}
    return {str(vendor).lower(): str(secret) for vendor, secret in configured.items()}


def secret_for_vendor(vendor: str) -> str | None:
    """The signing secret for one vendor, or the wildcard secret, or None."""
    secrets = _secrets()
    return secrets.get((vendor or "").lower()) or secrets.get("*")


def expected_signature(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def verify_signature(*, vendor: str, body: bytes, provided: str | None) -> None:
    """
    Raise SignatureError unless this request is authentic.

    No secret configured for the vendor means verification is not enabled for
    it — unless WEBHOOK_REQUIRE_SIGNATURE is set, in which case an unknown
    vendor is refused rather than silently trusted.
    """
    secret = secret_for_vendor(vendor)
    if secret is None:
        if getattr(settings, "WEBHOOK_REQUIRE_SIGNATURE", False):
            raise SignatureError("No signing secret configured for this vendor.")
        return

    if not provided:
        raise SignatureError(f"Missing {SIGNATURE_HEADER} header.")

    digest = expected_signature(secret, body).encode("ascii")
    candidate = provided.strip()
    for prefix in _PREFIXES:
        if not candidate.startswith(prefix):
            continue
        # Compared as bytes: compare_digest raises TypeError on a str holding
        # anything outside ASCII, and a header is attacker-controlled.
        offered = candidate[len(prefix) :].lower().encode("utf-8", "replace")
        if hmac.compare_digest(offered, digest):
            return
    raise SignatureError("Signature does not match the request body.")
