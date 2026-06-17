"""
The ingestion endpoint: what the vendor sees, and what survives a redelivery.

Two properties matter more than anything else here. The raw payload is stored
before anything interprets it, and a vendor that delivers the same event twice
gets one row — vendors retry aggressively, so "twice" is the normal case, not
the edge case.

No test here touches a broker: Celery dispatch is patched, and the only thing
asserted about it is *whether* it was asked for.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from django.conf import settings
from django.urls import reverse

from apps.ingestion.models import ProcessingStatus, RawWebhook

pytestmark = pytest.mark.django_db


@pytest.fixture
def enqueue():
    """Stand in for Celery so acceptance can be tested without a broker."""
    with patch("apps.ingestion.services.process_raw_webhook.apply_async") as mock:
        yield mock


def post(client, payload, **headers):
    return client.post(
        reverse("webhook-ingest"),
        data=json.dumps(payload),
        content_type="application/json",
        headers=headers,
    )


class TestAcceptance:
    def test_a_payload_is_accepted_and_stored_verbatim(self, client, enqueue):
        payload = {"vendor": "acme", "event": "delivered", "shipment_id": "SHIP-1"}

        response = post(client, payload, **{"Idempotency-Key": "demo-1"})

        assert response.status_code == 202
        assert response.json()["status"] == "accepted"
        webhook = RawWebhook.objects.get(id=response.json()["webhook_id"])
        # Stored byte-for-byte as sent: interpretation is derived data and has
        # to be reproducible from something that is not.
        assert webhook.raw_payload == payload
        assert webhook.processing_status == ProcessingStatus.RECEIVED

    def test_the_vendor_is_not_made_to_wait_for_interpretation(self, client, enqueue):
        response = post(client, {"shipment_id": "S", "vendor": "acme"})

        # 202, not 200: the payload is durable, nothing has interpreted it yet.
        assert response.status_code == 202
        assert RawWebhook.objects.get().processing_status == ProcessingStatus.RECEIVED

    def test_processing_is_enqueued_once_the_row_is_committed(
        self, client, enqueue, django_capture_on_commit_callbacks
    ):
        with django_capture_on_commit_callbacks(execute=True):
            response = post(client, {"shipment_id": "S", "vendor": "acme"})

        enqueue.assert_called_once_with(
            args=[response.json()["webhook_id"]], queue=settings.NORMALIZATION_QUEUE
        )

    def test_the_vendor_header_wins_over_the_payload(self, client, enqueue):
        post(
            client,
            {"vendor": "from-payload", "shipment_id": "S"},
            **{"X-Webhook-Vendor": "from-header"},
        )

        assert RawWebhook.objects.get().vendor == "from-header"

    def test_the_vendor_falls_back_to_the_payload_then_to_unknown(self, client, enqueue):
        post(client, {"carrier": "acme", "shipment_id": "S1"})
        post(client, {"shipment_id": "S2"})

        vendors = set(RawWebhook.objects.values_list("vendor", flat=True))
        assert vendors == {"acme", "unknown"}


class TestPayloadValidation:
    def test_an_empty_body_is_refused(self, client, enqueue):
        response = client.post(reverse("webhook-ingest"), data="", content_type="application/json")

        assert response.status_code == 400
        assert not RawWebhook.objects.exists()

    def test_a_bare_scalar_is_refused(self, client, enqueue):
        # Nothing to normalise and nothing to key state on. Accepting it only
        # defers the failure to a worker, where the vendor cannot see it.
        assert post(client, 42).status_code == 400
        assert not RawWebhook.objects.exists()

    def test_a_json_array_is_refused(self, client, enqueue):
        assert post(client, [{"shipment_id": "S"}]).status_code == 400

    def test_an_empty_object_is_refused(self, client, enqueue):
        assert post(client, {}).status_code == 400


class TestIdempotency:
    def test_a_redelivery_with_the_same_key_is_stored_once(self, client, enqueue):
        headers = {"Idempotency-Key": "delivery-1"}
        first = post(client, {"shipment_id": "S", "vendor": "acme"}, **headers)
        # The retry carries an extra field, as vendors' retries often do. The
        # key, not the bytes, decides that this is the same event.
        second = post(client, {"shipment_id": "S", "vendor": "acme", "retry": True}, **headers)

        assert first.status_code == second.status_code == 202
        assert first.json()["webhook_id"] == second.json()["webhook_id"]
        assert RawWebhook.objects.count() == 1

    def test_a_redelivery_is_not_enqueued_a_second_time(
        self, client, enqueue, django_capture_on_commit_callbacks
    ):
        headers = {"Idempotency-Key": "delivery-2"}
        with django_capture_on_commit_callbacks(execute=True):
            post(client, {"shipment_id": "S", "vendor": "acme"}, **headers)
            post(client, {"shipment_id": "S", "vendor": "acme"}, **headers)

        assert enqueue.call_count == 1

    def test_distinct_keys_keep_identical_payloads_apart(self, client, enqueue):
        payload = {"shipment_id": "S", "vendor": "acme", "status": "in_transit"}
        post(client, payload, **{"Idempotency-Key": "scan-1"})
        post(client, payload, **{"Idempotency-Key": "scan-2"})

        # Two scans of the same parcel with the same wording are two events.
        # Hashing the payload alone would have collapsed them.
        assert RawWebhook.objects.count() == 2

    def test_the_vendor_event_id_keys_idempotency_when_no_header_is_sent(self, client, enqueue):
        post(client, {"event_id": "EVT-1", "vendor": "acme", "shipment_id": "S"})
        post(client, {"event_id": "EVT-1", "vendor": "acme", "shipment_id": "S", "n": 2})

        assert RawWebhook.objects.count() == 1

    def test_identical_payloads_without_any_key_collapse(self, client, enqueue):
        payload = {"vendor": "acme", "shipment_id": "S", "status": "delivered"}
        post(client, payload)
        post(client, payload)

        assert RawWebhook.objects.count() == 1

    def test_the_same_event_id_under_a_new_key_is_still_a_duplicate(self, client, enqueue):
        # The uniqueness of (vendor, external_event_id) fires here rather than
        # the idempotency key's. Looking the row back up by key alone raised
        # DoesNotExist and turned a duplicate delivery into a 500.
        payload = {"event_id": "EVT-9", "vendor": "acme", "shipment_id": "S"}
        first = post(client, payload, **{"Idempotency-Key": "attempt-1"})
        second = post(client, payload, **{"Idempotency-Key": "attempt-2"})

        assert second.status_code == 202
        assert first.json()["webhook_id"] == second.json()["webhook_id"]
        assert RawWebhook.objects.count() == 1

    def test_an_over_long_key_still_fits_the_column(self, client, enqueue):
        response = post(client, {"shipment_id": "S"}, **{"Idempotency-Key": "k" * 400})

        assert response.status_code == 202
        assert len(RawWebhook.objects.get().idempotency_key) <= 255


class TestHealth:
    def test_health_reports_ok(self, client):
        response = client.get(reverse("health"))
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


class TestReplay:
    def test_replaying_a_finished_webhook_requeues_it(
        self, client, make_webhook, enqueue, django_capture_on_commit_callbacks
    ):
        webhook = make_webhook(processing_status=ProcessingStatus.NORMALIZED)

        with django_capture_on_commit_callbacks(execute=True):
            response = client.post(
                reverse("webhook-replay", args=[webhook.id]),
                data="{}",
                content_type="application/json",
            )

        assert response.status_code == 202
        assert response.json()["status"] == "accepted"
        webhook.refresh_from_db()
        assert webhook.processing_status == ProcessingStatus.RECEIVED
        # Replays go to the bulk lane so a backfill cannot delay live traffic.
        enqueue.assert_called_once_with(
            args=[str(webhook.id)], queue=settings.NORMALIZATION_BULK_QUEUE
        )

    def test_replaying_an_in_flight_webhook_is_a_conflict(self, client, make_webhook, enqueue):
        webhook = make_webhook(processing_status=ProcessingStatus.PROCESSING)

        response = client.post(
            reverse("webhook-replay", args=[webhook.id]),
            data="{}",
            content_type="application/json",
        )

        # Requeueing something a worker is holding would run it twice.
        assert response.status_code == 409
        assert response.json()["status"] == "skipped"
        enqueue.assert_not_called()

    def test_force_replays_even_an_in_flight_webhook(
        self, client, make_webhook, enqueue, django_capture_on_commit_callbacks
    ):
        # The escape hatch for a worker that died mid-task and left the row
        # stuck in PROCESSING forever.
        webhook = make_webhook(processing_status=ProcessingStatus.PROCESSING)

        with django_capture_on_commit_callbacks(execute=True):
            response = client.post(
                reverse("webhook-replay", args=[webhook.id]),
                data=json.dumps({"force": True}),
                content_type="application/json",
            )

        assert response.status_code == 202
        # Replays go to the bulk lane so a backfill cannot delay live traffic.
        enqueue.assert_called_once_with(
            args=[str(webhook.id)], queue=settings.NORMALIZATION_BULK_QUEUE
        )

    def test_replaying_clears_the_previous_error(self, client, make_webhook, enqueue):
        webhook = make_webhook(processing_status=ProcessingStatus.FAILED)
        webhook.error_message = "boom"
        webhook.save(update_fields=["error_message"])

        client.post(
            reverse("webhook-replay", args=[webhook.id]),
            data="{}",
            content_type="application/json",
        )

        webhook.refresh_from_db()
        assert webhook.error_message == ""

    def test_replaying_an_unknown_webhook_is_a_404(self, client, enqueue):
        response = client.post(
            reverse("webhook-replay", args=["f5b4f18b-703c-4bbd-9363-2cbb497fdb16"]),
            data="{}",
            content_type="application/json",
        )
        assert response.status_code == 404
