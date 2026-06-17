"""
Strict parsing of a normalisation, whoever produced it.

Both backends funnel through `NormalizationResult.from_dict`, which is the
last point at which a reading can be refused before it is written down. It is
deliberately unforgiving: a status outside the canonical vocabulary, a
timestamp without an offset or a confidence outside [0, 1] is a defect in the
producer, and accepting it would push the problem into entity state where it
is much harder to see.
"""

from __future__ import annotations

from datetime import UTC

import pytest

from apps.normalization.schemas import NormalizationResult

VALID = {
    "entity_type": "SHIPMENT",
    "entity_id": "SHIP-1",
    "canonical_status": "DELIVERED",
    "event_time": "2026-03-02T14:00:00Z",
    "confidence_score": 0.9,
    "normalized_payload": {"summary": "ok"},
}


class TestAcceptedInput:
    def test_a_well_formed_reading_parses(self):
        result = NormalizationResult.from_dict(VALID)

        assert result.entity_id == "SHIP-1"
        assert result.event_time.tzinfo is not None
        assert result.event_time.astimezone(UTC).hour == 14

    def test_lowercase_vocabulary_is_normalised_upward(self):
        result = NormalizationResult.from_dict(
            VALID | {"entity_type": "shipment", "canonical_status": "delivered"}
        )

        assert result.entity_type == "SHIPMENT"
        assert result.canonical_status == "DELIVERED"

    def test_an_explicit_offset_is_preserved(self):
        result = NormalizationResult.from_dict(VALID | {"event_time": "2026-03-02T14:00:00+08:00"})

        assert result.event_time.astimezone(UTC).hour == 6

    def test_a_missing_normalized_payload_defaults_to_empty(self):
        data = {k: v for k, v in VALID.items() if k != "normalized_payload"}

        assert NormalizationResult.from_dict(data).normalized_payload == {}

    @pytest.mark.parametrize("confidence", [0.0, 1.0])
    def test_the_confidence_bounds_are_inclusive(self, confidence):
        assert (
            NormalizationResult.from_dict(VALID | {"confidence_score": confidence}).confidence_score
            == confidence
        )


class TestRefusedInput:
    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("entity_type", "PARCEL"),
            ("canonical_status", "LOST_AT_SEA"),
            ("entity_id", "   "),
            ("event_time", "2026-03-02T14:00:00"),
            ("event_time", "last Tuesday"),
            ("confidence_score", 1.5),
            ("confidence_score", -0.1),
            ("normalized_payload", "not an object"),
        ],
    )
    def test_a_malformed_field_is_refused(self, field, value):
        with pytest.raises(ValueError):
            NormalizationResult.from_dict(VALID | {field: value})

    def test_a_shipment_status_is_not_valid_for_an_invoice(self):
        # Each vocabulary belongs to its own entity type; crossing them is how
        # a plausible-looking reading becomes a meaningless one.
        with pytest.raises(ValueError):
            NormalizationResult.from_dict(
                VALID | {"entity_type": "INVOICE", "canonical_status": "DELIVERED"}
            )

    def test_unclassified_accepts_only_unknown(self):
        with pytest.raises(ValueError):
            NormalizationResult.from_dict(
                VALID | {"entity_type": "UNCLASSIFIED", "canonical_status": "DELIVERED"}
            )

        assert (
            NormalizationResult.from_dict(
                VALID | {"entity_type": "UNCLASSIFIED", "canonical_status": "UNKNOWN"}
            ).canonical_status
            == "UNKNOWN"
        )

    @pytest.mark.parametrize("field", list(VALID)[:-1])
    def test_a_missing_required_field_is_refused(self, field):
        data = {k: v for k, v in VALID.items() if k != field}

        with pytest.raises((KeyError, ValueError)):
            NormalizationResult.from_dict(data)
