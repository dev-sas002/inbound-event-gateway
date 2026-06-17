"""
Threshold calibration.

The report's value is in what it refuses to claim. It must not recommend a
number off two decisions, must not pretend it can see events the gate never
held, and must say plainly when confidence is simply not separating good
readings from bad ones for a vendor.
"""

from __future__ import annotations

import pytest
from django.urls import reverse

from apps.normalization.calibration import (
    ALL_REJECTED,
    INSUFFICIENT,
    OVERLAPPING,
    SEPARATED,
    calibration_report,
)
from apps.normalization.review import APPROVE, REJECT, apply_review_decision

pytestmark = pytest.mark.django_db


@pytest.fixture
def held_event(make_webhook, make_event):
    """An event the gate held, for one named vendor, at a given confidence."""

    def _make(vendor: str, confidence: float, decision: str | None = None):
        webhook = make_webhook(vendor=vendor)
        event = make_event(webhook=webhook, confidence=confidence, entity_id=f"E-{confidence}")
        event.requires_review = True
        event.save(update_fields=["requires_review"])
        if decision:
            apply_review_decision(event, decision, actor="test")
        return event

    return _make


def row_for(report, vendor: str):
    return next(row for row in report.vendors if row.vendor == vendor)


class TestWhatItCounts:
    def test_it_reports_hold_rate_per_vendor(self, held_event, make_webhook, make_event):
        held_event("acme", 0.3)
        make_event(webhook=make_webhook(vendor="acme"), confidence=0.95)

        row = row_for(calibration_report(), "acme")
        assert row.events == 2
        assert row.held == 1
        assert row.hold_rate == 0.5

    def test_vendors_are_not_mixed_together(self, held_event):
        held_event("acme", 0.3)
        held_event("globex", 0.4)
        report = calibration_report()
        assert {row.vendor for row in report.vendors} == {"acme", "globex"}
        assert row_for(report, "acme").events == 1

    def test_it_reports_the_threshold_it_is_judging_against(self, settings):
        settings.LOW_CONFIDENCE_THRESHOLD = 0.55
        assert calibration_report().current_threshold == 0.55


class TestWhatItConcludes:
    def test_too_few_decisions_produces_no_advice(self, held_event):
        held_event("acme", 0.3, APPROVE)
        held_event("acme", 0.4, APPROVE)
        row = row_for(calibration_report(), "acme")
        # Two approvals is not evidence; recommending a threshold off it would
        # be worse than saying nothing.
        assert row.verdict == INSUFFICIENT
        assert row.suggested_threshold is None

    def test_separated_confidences_yield_a_suggestion(self, held_event, settings):
        settings.LOW_CONFIDENCE_THRESHOLD = 0.7
        for confidence in (0.55, 0.58, 0.6, 0.62):
            held_event("acme", confidence, APPROVE)
        held_event("acme", 0.3, REJECT)

        row = row_for(calibration_report(), "acme")
        assert row.verdict == SEPARATED
        # Everything approved sat at or above 0.55 and everything rejected
        # below it, so 0.55 is the line that would have held exactly the
        # rejections and released exactly the approvals.
        assert row.suggested_threshold == 0.55
        assert row.approval_rate == 0.8

    def test_overlapping_confidences_are_reported_as_such(self, held_event):
        for confidence in (0.4, 0.45, 0.5):
            held_event("acme", confidence, APPROVE)
        for confidence in (0.42, 0.55):
            held_event("acme", confidence, REJECT)

        row = row_for(calibration_report(), "acme")
        # The rejections are interleaved with the approvals, so no threshold
        # separates them; the answer is a vendor profile, not a number.
        assert row.verdict == OVERLAPPING
        assert row.suggested_threshold is None
        assert "profile" in row.note

    def test_all_rejected_means_the_gate_is_working(self, held_event):
        for confidence in (0.2, 0.25, 0.3, 0.35, 0.4):
            held_event("acme", confidence, REJECT)
        row = row_for(calibration_report(), "acme")
        assert row.verdict == ALL_REJECTED
        assert row.suggested_threshold is None

    def test_a_suggestion_at_the_current_threshold_is_not_a_finding(self, held_event, settings):
        settings.LOW_CONFIDENCE_THRESHOLD = 0.5
        for confidence in (0.5, 0.55, 0.6, 0.65, 0.7):
            held_event("acme", confidence, APPROVE)
        row = row_for(calibration_report(), "acme")
        # Nothing to move, so nothing is recommended.
        assert row.verdict == SEPARATED
        assert row.suggested_threshold is None


class TestTheEndpoint:
    def test_it_answers_with_the_report(self, client, held_event):
        held_event("acme", 0.3, REJECT)
        body = client.get(reverse("calibration-report")).json()
        assert body["count"] == 1
        assert body["results"][0]["vendor"] == "acme"
        assert body["results"][0]["rejected"] == 1

    def test_an_empty_system_is_not_an_error(self, client):
        body = client.get(reverse("calibration-report")).json()
        assert body["count"] == 0
        assert body["results"] == []


class TestTheDecisionRecord:
    def test_a_decision_is_stored_as_data_not_prose(self, held_event):
        event = held_event("acme", 0.3, APPROVE)
        event.refresh_from_db()
        # Calibration reads this column. Parsing it back out of the audit note
        # would break the first time somebody reworded the note.
        assert event.review_decision == APPROVE
        assert "approved by test" in event.review_reason
