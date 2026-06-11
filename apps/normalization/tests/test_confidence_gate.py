"""
Tests for the confidence gate and the review queue.

The gate is the reason this service can be trusted with an unattended feed:
a normalisation the pipeline is not sure about is recorded, but is not allowed
to become the system's belief about a shipment or an invoice until a person
says so. These tests pin that boundary in both directions.
"""

from __future__ import annotations

import uuid

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.entities.models import EntityState
from apps.ingestion.models import ProcessingStatus, RawWebhook
from apps.normalization.models import EntityType, NormalizedEvent
from apps.normalization.review import (
    AlreadyDecided,
    apply_confidence_gate,
    apply_review_decision,
)

pytestmark = pytest.mark.django_db


def make_event(*, confidence: float, entity_id: str, status: str = "DELIVERED"):
    # A unique idempotency key per fixture. The table enforces uniqueness on
    # (vendor, idempotency_key) — that constraint is the ingestion idempotency
    # guarantee, so the fixtures have to respect it rather than work around it.
    webhook = RawWebhook.objects.create(
        id=uuid.uuid4(),
        vendor="test-vendor",
        idempotency_key=f"test-{entity_id}-{uuid.uuid4().hex[:8]}",
        raw_payload={"shipment_id": entity_id},
        processing_status=ProcessingStatus.NORMALIZED,
    )
    return NormalizedEvent.objects.create(
        webhook=webhook,
        entity_type=EntityType.SHIPMENT,
        entity_id=entity_id,
        canonical_status=status,
        event_time=timezone.now(),
        normalized_payload={},
        confidence_score=confidence,
        llm_model="rules-based-normalizer",
        prompt_version="rules-v1",
    )


class TestReviewQueue:
    def test_it_lists_only_events_that_were_held(self, client):
        make_event(confidence=0.95, entity_id="TRUSTED")
        held = make_event(confidence=0.30, entity_id="HELD")
        held.requires_review = True
        held.review_reason = "confidence 0.30 is below the 0.70 threshold"
        held.save()

        response = client.get(reverse("review-queue"))
        body = response.json()

        assert response.status_code == 200
        assert body["count"] == 1
        assert body["results"][0]["entity_id"] == "HELD"

    def test_an_already_reviewed_event_leaves_the_queue(self, client):
        event = make_event(confidence=0.30, entity_id="DONE")
        event.requires_review = True
        event.reviewed_at = timezone.now()
        event.save()

        assert client.get(reverse("review-queue")).json()["count"] == 0

    def test_the_queue_exposes_an_id_so_it_can_be_acted_on(self, client):
        event = make_event(confidence=0.30, entity_id="ACTIONABLE")
        event.requires_review = True
        event.save()

        result = client.get(reverse("review-queue")).json()["results"][0]
        assert result["id"] == event.id


class TestDecisions:
    def test_approving_promotes_the_event_to_entity_state(self, client):
        event = make_event(confidence=0.30, entity_id="APPROVE-ME")
        event.requires_review = True
        event.save()

        response = client.post(
            reverse("review-decision", args=[event.id]),
            data={"decision": "approve"},
            content_type="application/json",
        )

        assert response.status_code == 200
        assert response.json()["entity_state_updated"] is True
        assert EntityState.objects.filter(entity_id="APPROVE-ME").exists()

    def test_rejecting_leaves_entity_state_untouched(self, client):
        # The whole point of the gate: a rejected guess must never become the
        # system's belief.
        event = make_event(confidence=0.30, entity_id="REJECT-ME")
        event.requires_review = True
        event.save()

        response = client.post(
            reverse("review-decision", args=[event.id]),
            data={"decision": "reject"},
            content_type="application/json",
        )

        assert response.status_code == 200
        assert response.json()["entity_state_updated"] is False
        assert not EntityState.objects.filter(entity_id="REJECT-ME").exists()

    def test_a_decision_is_recorded_so_the_event_does_not_reappear(self, client):
        event = make_event(confidence=0.30, entity_id="ONCE")
        event.requires_review = True
        event.save()

        client.post(
            reverse("review-decision", args=[event.id]),
            data={"decision": "approve"},
            content_type="application/json",
        )

        event.refresh_from_db()
        assert event.reviewed_at is not None
        assert "approve" in event.review_reason
        assert client.get(reverse("review-queue")).json()["count"] == 0

    def test_an_unknown_decision_is_rejected(self, client):
        event = make_event(confidence=0.30, entity_id="BAD-INPUT")
        event.requires_review = True
        event.save()

        response = client.post(
            reverse("review-decision", args=[event.id]),
            data={"decision": "maybe"},
            content_type="application/json",
        )
        assert response.status_code == 400

    def test_a_second_decision_on_the_same_event_is_refused(self, client):
        # A decision is final. Re-deciding would promote the event to entity
        # state twice and append a second note to its audit trail, so the
        # record would no longer say what actually happened.
        event = make_event(confidence=0.30, entity_id="TWICE")
        event.requires_review = True
        event.save()
        url = reverse("review-decision", args=[event.id])

        first = client.post(url, data={"decision": "reject"}, content_type="application/json")
        second = client.post(url, data={"decision": "approve"}, content_type="application/json")

        assert first.status_code == 200
        assert second.status_code == 409
        assert not EntityState.objects.filter(entity_id="TWICE").exists()

    def test_the_rule_itself_refuses_a_settled_event(self):
        event = make_event(confidence=0.30, entity_id="SETTLED")
        event.requires_review = True
        event.save()
        apply_review_decision(event, "approve")

        with pytest.raises(AlreadyDecided):
            apply_review_decision(event, "reject")

    def test_deciding_on_an_event_that_was_never_held_is_a_404(self, client):
        event = make_event(confidence=0.95, entity_id="NEVER-HELD")

        response = client.post(
            reverse("review-decision", args=[event.id]),
            data={"decision": "approve"},
            content_type="application/json",
        )
        assert response.status_code == 404
        assert not EntityState.objects.filter(entity_id="NEVER-HELD").exists()


class TestTheGateItself:
    """
    The gate function directly, rather than through the task.

    Extracting it from the Celery task is what makes these possible: the rule
    can now be exercised without a broker, a worker, or a normaliser.
    """

    def test_a_confident_event_becomes_entity_state_immediately(self, settings):
        settings.LOW_CONFIDENCE_THRESHOLD = 0.7
        event = make_event(confidence=0.90, entity_id="CONFIDENT")

        outcome = apply_confidence_gate(event)

        assert outcome.held is False
        assert EntityState.objects.filter(entity_id="CONFIDENT").exists()

    def test_an_unsure_event_is_held_and_kept_out_of_entity_state(self, settings):
        settings.LOW_CONFIDENCE_THRESHOLD = 0.7
        event = make_event(confidence=0.30, entity_id="UNSURE")

        outcome = apply_confidence_gate(event)
        event.refresh_from_db()

        assert outcome.held is True
        assert event.requires_review is True
        assert "0.30" in event.review_reason and "0.70" in event.review_reason
        assert not EntityState.objects.filter(entity_id="UNSURE").exists()

    def test_the_threshold_is_configuration_not_a_constant(self, settings):
        # The same event must be trusted or held depending only on settings —
        # a deployment handling low-stakes data can lower the bar.
        settings.LOW_CONFIDENCE_THRESHOLD = 0.5
        assert apply_confidence_gate(make_event(confidence=0.60, entity_id="A")).held is False

        settings.LOW_CONFIDENCE_THRESHOLD = 0.95
        assert apply_confidence_gate(make_event(confidence=0.60, entity_id="B")).held is True

    def test_an_event_exactly_at_the_threshold_is_trusted(self, settings):
        # The boundary is documented as "below the threshold is held", so the
        # threshold value itself must pass. Pinning it stops the comparison
        # flipping to <= during a later edit.
        settings.LOW_CONFIDENCE_THRESHOLD = 0.7
        assert apply_confidence_gate(make_event(confidence=0.7, entity_id="EXACT")).held is False

    def test_an_invalid_decision_is_refused_at_the_rule_not_the_view(self):
        # The API validates too, but the admin and any future caller rely on
        # the rule itself refusing nonsense.
        event = make_event(confidence=0.30, entity_id="BAD")
        with pytest.raises(ValueError, match="decision must be"):
            apply_review_decision(event, "maybe")


class TestTheLowConfidenceListing:
    """
    A separate view from the review queue: it re-queries by a threshold today,
    where the queue lists what the gate actually held at the time it ran.
    """

    def test_it_defaults_to_the_configured_threshold(self, client, settings):
        # Hardcoding 0.7 here meant that lowering the gate's threshold left
        # this view still reporting events the pipeline had in fact trusted.
        settings.LOW_CONFIDENCE_THRESHOLD = 0.5
        make_event(confidence=0.60, entity_id="TRUSTED-AT-0.5")
        make_event(confidence=0.40, entity_id="BELOW")

        body = client.get(reverse("low-confidence-events")).json()

        assert [row["entity_id"] for row in body["results"]] == ["BELOW"]

    def test_an_explicit_threshold_overrides_it(self, client, settings):
        settings.LOW_CONFIDENCE_THRESHOLD = 0.5
        make_event(confidence=0.60, entity_id="SIXTY")

        body = client.get(reverse("low-confidence-events"), {"threshold": "0.9"}).json()

        assert body["count"] == 1

    @pytest.mark.parametrize("threshold", ["abc", "", "2", "-1"])
    def test_an_unusable_threshold_is_a_400_not_a_500(self, client, threshold):
        # The unguarded float() turned a caller's typo into a server error.
        response = client.get(reverse("low-confidence-events"), {"threshold": threshold})

        assert response.status_code == 400
