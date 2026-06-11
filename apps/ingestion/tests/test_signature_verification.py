"""
Webhook authenticity.

Without verification, anyone who learns the URL can write into entity state,
and the service has no way to tell a vendor's delivery from a forgery. The
rule is deliberately per vendor and opt-in: a service with no secrets
configured behaves exactly as it did before, which is what keeps the offline
demo runnable, and a vendor that has been given a secret is held to it from
that moment on.
"""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest
from django.urls import reverse

from apps.ingestion.models import RawWebhook
from apps.ingestion.security import SignatureError, verify_signature
from config.settings import _parse_signing_secrets

pytestmark = pytest.mark.django_db

SECRET = "whsec_acme_1"


def sign(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def deliver(client, payload: dict, signature: str | None = None, vendor: str = "acme"):
    body = json.dumps(payload).encode()
    headers = {"X-Webhook-Vendor": vendor}
    if signature is not None:
        headers["X-Webhook-Signature"] = signature
    return client.post(
        reverse("webhook-ingest"),
        data=body,
        content_type="application/json",
        headers=headers,
    )


@pytest.fixture
def signed_vendor(settings):
    settings.WEBHOOK_SIGNING_SECRETS = {"acme": SECRET}
    settings.WEBHOOK_REQUIRE_SIGNATURE = False
    return settings


class TestUnconfiguredVendors:
    def test_nothing_is_verified_when_no_secrets_are_configured(self, client, settings):
        settings.WEBHOOK_SIGNING_SECRETS = {}

        assert deliver(client, {"shipment_id": "S"}).status_code == 202

    def test_a_vendor_without_a_secret_is_still_accepted(self, client, signed_vendor):
        # "acme" is verified; "initech" has not been onboarded yet and must
        # not be broken by another vendor's rollout.
        assert deliver(client, {"shipment_id": "S"}, vendor="initech").status_code == 202

    def test_require_signature_closes_that_door(self, client, settings):
        settings.WEBHOOK_SIGNING_SECRETS = {"acme": SECRET}
        settings.WEBHOOK_REQUIRE_SIGNATURE = True

        response = deliver(client, {"shipment_id": "S"}, vendor="initech")

        assert response.status_code == 401
        assert not RawWebhook.objects.exists()


class TestVerification:
    def test_a_correctly_signed_delivery_is_accepted(self, client, signed_vendor):
        payload = {"shipment_id": "S", "status": "delivered"}
        body = json.dumps(payload).encode()

        response = deliver(client, payload, signature=f"sha256={sign(SECRET, body)}")

        assert response.status_code == 202
        assert RawWebhook.objects.count() == 1

    def test_the_algorithm_prefix_is_optional(self, client, signed_vendor):
        payload = {"shipment_id": "S"}
        body = json.dumps(payload).encode()

        assert deliver(client, payload, signature=sign(SECRET, body)).status_code == 202

    def test_an_unsigned_delivery_from_a_signed_vendor_is_refused(self, client, signed_vendor):
        response = deliver(client, {"shipment_id": "S"})

        assert response.status_code == 401
        assert not RawWebhook.objects.exists()

    def test_a_signature_from_the_wrong_secret_is_refused(self, client, signed_vendor):
        payload = {"shipment_id": "S"}
        body = json.dumps(payload).encode()

        response = deliver(client, payload, signature=sign("not-the-secret", body))

        assert response.status_code == 401
        assert not RawWebhook.objects.exists()

    def test_a_tampered_body_is_refused(self, client, signed_vendor):
        # The signature is genuine, for a different body. This is the attack
        # the verification exists to stop: a replayed header over edited JSON.
        original = json.dumps({"shipment_id": "S", "status": "in_transit"}).encode()
        signature = f"sha256={sign(SECRET, original)}"

        response = deliver(client, {"shipment_id": "S", "status": "delivered"}, signature=signature)

        assert response.status_code == 401

    @pytest.mark.parametrize(
        "signature", ["sha256=zzzz", "", "sha256=", "sha256=ünïcödé", "x" * 5000]
    )
    def test_garbage_in_the_header_is_refused_not_crashed(self, client, signed_vendor, signature):
        # The header is attacker-controlled, so every shape of nonsense has to
        # come back as a refusal rather than a stack trace.
        response = deliver(client, {"shipment_id": "S"}, signature=signature)

        assert response.status_code == 401

    def test_the_vendor_name_is_matched_case_insensitively(self, client, signed_vendor):
        payload = {"shipment_id": "S"}
        body = json.dumps(payload).encode()

        response = deliver(client, payload, signature=sign(SECRET, body), vendor="ACME")

        assert response.status_code == 202

    def test_a_wildcard_secret_covers_every_vendor(self, client, settings):
        settings.WEBHOOK_SIGNING_SECRETS = {"*": SECRET}
        payload = {"shipment_id": "S"}
        body = json.dumps(payload).encode()

        assert (
            deliver(client, payload, signature=sign(SECRET, body), vendor="anyone").status_code
            == 202
        )
        assert deliver(client, {"shipment_id": "T"}, vendor="anyone").status_code == 401


class TestTheRuleItself:
    def test_verification_is_skipped_for_an_unknown_vendor(self, settings):
        settings.WEBHOOK_SIGNING_SECRETS = {"acme": SECRET}
        settings.WEBHOOK_REQUIRE_SIGNATURE = False

        verify_signature(vendor="initech", body=b"{}", provided=None)

    def test_a_mismatch_raises(self, settings):
        settings.WEBHOOK_SIGNING_SECRETS = {"acme": SECRET}

        with pytest.raises(SignatureError):
            verify_signature(vendor="acme", body=b"{}", provided="sha256=deadbeef")


class TestSecretParsing:
    def test_pairs_are_read_into_a_mapping(self):
        assert _parse_signing_secrets("acme=one,initech=two") == {
            "acme": "one",
            "initech": "two",
        }

    def test_a_base64_secret_containing_equals_survives(self):
        # Splitting on every "=" would truncate the secret and silently reject
        # every delivery from that vendor.
        assert _parse_signing_secrets("acme=c2VjcmV0==") == {"acme": "c2VjcmV0=="}

    def test_blank_and_malformed_entries_are_ignored(self):
        assert _parse_signing_secrets(" , nonsense , acme=one ,=x,b=") == {"acme": "one"}

    def test_an_empty_setting_verifies_nobody(self):
        assert _parse_signing_secrets("") == {}
