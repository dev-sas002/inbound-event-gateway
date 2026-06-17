"""
Tests for the rule-based normaliser.

This is the component that decides what a vendor's webhook *means*, so the
property that matters most is not how much it recognises — it is that it
reports low confidence when it is guessing, rather than asserting something
nobody verified. The confidence gate downstream depends on that being honest.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from apps.normalization.backends.rules_backend import RuleBasedNormalizer
from apps.normalization.exceptions import NormalizationError
from apps.normalization.models import EntityType


@pytest.fixture
def normalizer():
    return RuleBasedNormalizer()


class TestShipments:
    def test_it_recognises_a_shipment_and_maps_the_status(self, normalizer):
        result = normalizer.normalize(
            {
                "event": "delivered",
                "shipment_id": "SHIP-100",
                "carrier": "acme-freight",
                "timestamp": "2026-03-02T14:00:00Z",
            }
        ).normalized
        assert result.entity_type == EntityType.SHIPMENT
        assert result.entity_id == "SHIP-100"
        assert result.canonical_status == "DELIVERED"
        assert result.confidence_score > 0.7

    def test_vendor_synonyms_map_to_one_canonical_status(self, normalizer):
        # Three vendors, three words, one meaning. Collapsing that is the
        # entire point of the service.
        for word in ("in_transit", "dispatched", "shipped"):
            result = normalizer.normalize(
                {"status": word, "tracking_number": "T1", "carrier": "x"}
            ).normalized
            assert result.canonical_status == "IN_TRANSIT", word

    def test_an_id_nested_deep_in_the_payload_is_still_found(self, normalizer):
        result = normalizer.normalize(
            {
                "data": {"object": {"shipment": {"tracking_number": "DEEP-9"}}},
                "status": "delivered",
                "carrier": "x",
            }
        ).normalized
        assert result.entity_id == "DEEP-9"


class TestInvoices:
    def test_it_recognises_an_invoice(self, normalizer):
        result = normalizer.normalize(
            {
                "type": "invoice.payment_succeeded",
                "invoice_id": "INV-7",
                "status": "paid",
                "amount_due": 0,
                "occurred_at": "2026-03-02T09:30:00+00:00",
            }
        ).normalized
        assert result.entity_type == EntityType.INVOICE
        assert result.canonical_status == "PAID"

    def test_invoice_and_shipment_vocabulary_do_not_collide(self, normalizer):
        # "cancelled" is valid for both, and must resolve per entity type.
        # "delivered" is shipment vocabulary and means nothing for an invoice;
        # each must resolve within its own entity type.
        invoice = normalizer.normalize(
            {"invoice_id": "I1", "status": "voided", "billing": True}
        ).normalized
        shipment = normalizer.normalize(
            {"shipment_id": "S1", "status": "delivered", "carrier": "x"}
        ).normalized
        assert invoice.canonical_status == "VOIDED"
        assert shipment.canonical_status == "DELIVERED"


class TestHonestConfidence:
    def test_unrecognised_status_scores_low_rather_than_guessing(self, normalizer):
        # The failure this guards against: confidently asserting a status the
        # normaliser has never seen. Low confidence routes it for review.
        result = normalizer.normalize(
            {
                "shipment_id": "S2",
                "status": "quantum_entangled",
                "carrier": "x",
                "timestamp": "2026-03-02T14:00:00Z",
            }
        ).normalized
        assert result.confidence_score < 0.5

    def test_an_unclassifiable_payload_says_so(self, normalizer):
        result = normalizer.normalize({"id": "X1", "foo": "bar"}).normalized
        assert result.entity_type == EntityType.UNCLASSIFIED
        assert result.canonical_status == "UNKNOWN"
        assert result.confidence_score < 0.5

    def test_a_missing_timestamp_lowers_confidence(self, normalizer):
        with_time = normalizer.normalize(
            {
                "shipment_id": "S3",
                "status": "delivered",
                "carrier": "x",
                "timestamp": "2026-03-02T14:00:00Z",
            }
        ).normalized
        without_time = normalizer.normalize(
            {"shipment_id": "S3", "status": "delivered", "carrier": "x"}
        ).normalized
        assert without_time.confidence_score < with_time.confidence_score

    def test_confidence_is_governed_by_the_weakest_signal(self, normalizer):
        # Clear entity type, unknown status: the result must not inherit the
        # confidence of the part it got right.
        result = normalizer.normalize(
            {
                "shipment_id": "S4",
                "carrier": "acme",
                "tracking_number": "T",
                "status": "???",
                "timestamp": "2026-03-02T14:00:00Z",
            }
        ).normalized
        assert result.confidence_score < 0.5


class TestTimestamps:
    @pytest.mark.parametrize(
        "raw",
        ["2026-03-02T14:00:00Z", "2026-03-02T14:00:00+00:00", 1772460000, 1772460000000],
    )
    def test_it_accepts_the_timestamp_formats_vendors_actually_send(self, normalizer, raw):
        result = normalizer.normalize(
            {"shipment_id": "S5", "status": "delivered", "carrier": "x", "timestamp": raw}
        ).normalized
        assert result.event_time.tzinfo is not None

    def test_an_unparseable_timestamp_falls_back_to_now_with_low_confidence(self, normalizer):
        before = datetime.now(UTC)
        result = normalizer.normalize(
            {
                "shipment_id": "S6",
                "status": "delivered",
                "carrier": "x",
                "timestamp": "last Tuesday",
            }
        ).normalized
        assert result.event_time >= before
        assert result.confidence_score < 0.5


class TestHardFailures:
    def test_a_payload_with_no_identifier_is_rejected(self, normalizer):
        # There is nothing to key entity state on, so this cannot be a
        # low-confidence pass — it has to fail.
        with pytest.raises(NormalizationError, match="identifier"):
            normalizer.normalize({"status": "delivered", "carrier": "acme"})


class TestResponseShape:
    def test_it_returns_the_same_shape_as_the_openai_service(self, normalizer):
        response = normalizer.normalize(
            {"shipment_id": "S7", "status": "delivered", "carrier": "x"}
        )
        assert hasattr(response, "normalized")
        assert response.llm_model == "rules-based-normalizer"
        assert response.prompt_version.startswith("rules")


class TestNamespacedEventNames:
    """
    Vendors rarely send a bare status. They send "invoice.payment_succeeded"
    or "parcel.out_for_delivery", and the meaning is in the suffix.
    """

    def test_a_dotted_event_name_resolves_to_its_suffix(self, normalizer):
        result = normalizer.normalize(
            {
                "type": "parcel.out_for_delivery",
                "tracking_id": "SHIP-1",
                "carrier": "initech",
                "ts": 1756454400,
            }
        ).normalized
        assert result.canonical_status == "OUT_FOR_DELIVERY"
        assert result.confidence_score > 0.7

    def test_the_longest_suffix_wins(self, normalizer):
        # "out_for_delivery" and "delivery" both end this string; matching the
        # short one first would silently produce the wrong status.
        result = normalizer.normalize(
            {
                "event": "shipment.status.out_for_delivery",
                "shipment_id": "S",
                "carrier": "x",
                "timestamp": "2026-03-02T14:00:00Z",
            }
        ).normalized
        assert result.canonical_status == "OUT_FOR_DELIVERY"

    def test_an_explicit_status_field_beats_a_dotted_type(self, normalizer):
        # Both are present and they disagree; "status" is the more direct
        # statement of intent and must win.
        result = normalizer.normalize(
            {
                "type": "invoice.created",
                "status": "paid",
                "invoice_id": "I",
                "billing": True,
                "occurred_at": "2026-03-02T14:00:00Z",
            }
        ).normalized
        assert result.canonical_status == "PAID"


class TestWhatItReportsAboutItself:
    """
    The normalised payload is the only record of how much the normaliser had
    to fall back on, so it has to describe this payload rather than a fixed
    list of fields it might have found.
    """

    def test_it_reports_the_fields_it_actually_found(self, normalizer):
        result = normalizer.normalize(
            {
                "shipment_id": "S8",
                "status": "delivered",
                "carrier": "x",
                "timestamp": "2026-03-02T14:00:00Z",
            }
        ).normalized

        assert result.normalized_payload["fields_found"] == [
            "entity_id",
            "event_time",
            "status",
        ]

    def test_a_field_it_had_to_invent_is_not_reported_as_found(self, normalizer):
        result = normalizer.normalize({"shipment_id": "S9", "carrier": "x"}).normalized

        # No status and no timestamp in the payload: the default status and the
        # arrival time are guesses, and saying otherwise would hide that.
        assert result.normalized_payload["fields_found"] == ["entity_id"]
        assert result.normalized_payload["source_status"] is None

    def test_the_source_status_is_kept_for_auditing(self, normalizer):
        result = normalizer.normalize(
            {"shipment_id": "S10", "status": "dispatched", "carrier": "x"}
        ).normalized

        assert result.canonical_status == "IN_TRANSIT"
        assert result.normalized_payload["source_status"] == "dispatched"


class TestVendorIdentifierNames:
    @pytest.mark.parametrize(
        "key",
        ["shipment_id", "tracking_number", "tracking_id", "container_no", "shipment_reference"],
    )
    def test_the_identifier_names_real_carriers_use_are_found(self, normalizer, key):
        # The project's own sample payloads use container_no and
        # shipment_reference; not knowing them made those payloads a hard
        # failure rather than a low-confidence reading.
        result = normalizer.normalize(
            {key: "ID-1", "status": "delivered", "carrier": "x"}
        ).normalized

        assert result.entity_id == "ID-1"


class TestClassification:
    """
    What kind of thing a payload is about.

    Counting domain words in the body was the only signal, and it was a poor
    one: a container-shipping event that never used the word "shipment" came
    out UNCLASSIFIED and was held for review every single time, while a vendor
    whose *name* contained "freight" was pushed toward SHIPMENT no matter what
    it had sent.
    """

    def test_the_identifier_key_names_the_entity(self, normalizer):
        result = normalizer.normalize(
            {
                "container_no": "MAEU240498712",
                "event_description": "delivered",
                "event_time": "2026-04-21T22:47:00+00:00",
            }
        ).normalized
        # Nothing here says "shipment", but a container number is a shipment.
        assert result.entity_type == EntityType.SHIPMENT
        assert result.canonical_status == "DELIVERED"
        assert result.confidence_score >= 0.7

    def test_an_invoice_identifier_names_an_invoice(self, normalizer):
        result = normalizer.normalize({"invoice_number": "INV-9", "message": "paid"}).normalized
        assert result.entity_type == EntityType.INVOICE
        assert result.canonical_status == "PAID"

    def test_the_vendor_name_is_not_evidence(self, normalizer):
        # "GlobalFreightPay" contains "freight"; that says nothing about what
        # this particular payload is about.
        result = normalizer.normalize(
            {"vendor": "GlobalFreightPay", "id": "X-1", "invoice_id": "INV-3", "status": "paid"}
        ).normalized
        assert result.entity_type == EntityType.INVOICE

    def test_a_carrier_key_alone_does_not_make_a_shipment(self, normalizer):
        # `carrier` is both a vendor-naming key and a shipment hint word, so a
        # payload of any kind used to score as a shipment purely for having it.
        result = normalizer.normalize(
            {"carrier": "Someone", "id": "REF-1", "title": "advisory bulletin"}
        ).normalized
        assert result.entity_type == EntityType.UNCLASSIFIED

    def test_a_generic_identifier_still_falls_back_to_the_words(self, normalizer):
        result = normalizer.normalize(
            {"id": "REF-1", "billing": {"amount_due": "10.00"}, "status": "paid"}
        ).normalized
        assert result.entity_type == EntityType.INVOICE

    def test_an_unrecognisable_payload_is_still_unclassified(self, normalizer):
        result = normalizer.normalize(
            {"id": "MAR-1", "title": "Port congestion warning", "severity": "medium"}
        ).normalized
        # Held for review, which is the correct answer to "I do not know".
        assert result.entity_type == EntityType.UNCLASSIFIED
        assert result.confidence_score < 0.7
