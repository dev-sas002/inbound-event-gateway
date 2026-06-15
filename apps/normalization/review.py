"""
The confidence gate, and the decisions people make about what it holds.

This module is the single place that decides whether a normalisation becomes
the system's belief about a shipment or an invoice. Three callers need that
rule — the Celery task that runs after every normalisation, the review API, and
the admin console — and a rule about trust that is written down three times is
a rule that will eventually disagree with itself.

It deliberately imports no vendor SDK, so the gate can be tested and reasoned
about without an OpenAI key present.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.entities.repositories import EntityStateRepository
from apps.normalization.models import NormalizedEvent, ReviewDecision
from apps.normalization.utils import is_low_confidence

logger = logging.getLogger("apps.normalization")

APPROVE = ReviewDecision.APPROVE.value
REJECT = ReviewDecision.REJECT.value
VALID_DECISIONS = (APPROVE, REJECT)

#: NormalizedEvent.review_reason is a varchar(255) and the reason grows by a
#: note each time a decision is recorded, so it is trimmed before it is saved
#: rather than letting PostgreSQL refuse the write.
_MAX_REVIEW_REASON = 255


class AlreadyDecided(Exception):
    """
    Someone has already ruled on this event.

    Applying a second decision would re-run the promotion to entity state and
    append a second note to the audit trail, so the event's history would no
    longer say what actually happened. The rule refuses it here rather than in
    each caller, because the admin and the API must agree that a decision is
    final.
    """


@dataclass(frozen=True)
class GateOutcome:
    """What the gate did with one event."""

    held: bool
    reason: str = ""


def apply_confidence_gate(event: NormalizedEvent) -> GateOutcome:
    """
    Decide whether an event is trusted enough to update entity state.

    Below the threshold the event is still recorded in full — nothing is
    discarded — but it is withheld from entity state until a person approves
    it. The alternative, letting a guess through and correcting later, means
    downstream consumers act on a status nobody verified.
    """
    threshold = settings.LOW_CONFIDENCE_THRESHOLD

    if is_low_confidence(event.confidence_score, threshold):
        reason = f"confidence {event.confidence_score:.2f} is below the {threshold:.2f} threshold"
        event.requires_review = True
        event.review_reason = reason[:_MAX_REVIEW_REASON]
        event.save(update_fields=["requires_review", "review_reason"])
        logger.warning(
            "normalization_held_for_review",
            extra={
                "event_id": event.id,
                "entity_id": event.entity_id,
                "confidence": event.confidence_score,
                "threshold": threshold,
            },
        )
        return GateOutcome(held=True, reason=reason)

    EntityStateRepository.upsert_if_newer(event)
    return GateOutcome(held=False)


def apply_review_decision(
    event: NormalizedEvent, decision: str, *, actor: str = "reviewer"
) -> bool:
    """
    Record a person's decision about a held event.

    Returns whether entity state was updated. Approval is the human supplying
    the confidence the normaliser lacked; rejection leaves entity state exactly
    as it was, which is the entire point of holding the event in the first
    place.

    The event is marked reviewed either way, so a decision is final and the
    event does not reappear in the queue.
    """
    if decision not in VALID_DECISIONS:
        raise ValueError(f'decision must be one of {VALID_DECISIONS}, got "{decision}"')
    if event.reviewed_at is not None:
        raise AlreadyDecided(f"event {event.id} was already reviewed at {event.reviewed_at}")

    updated = False
    with transaction.atomic():
        if decision == APPROVE:
            EntityStateRepository.upsert_if_newer(event)
            updated = True
        event.reviewed_at = timezone.now()
        event.review_decision = decision
        note = f"{decision}d by {actor}"
        event.review_reason = (f"{event.review_reason} | {note}" if event.review_reason else note)[
            :_MAX_REVIEW_REASON
        ]
        event.save(update_fields=["reviewed_at", "review_decision", "review_reason"])

    logger.info(
        "review_decision_recorded",
        extra={
            "event_id": event.id,
            "decision": decision,
            "actor": actor,
            "entity_state_updated": updated,
        },
    )
    return updated
