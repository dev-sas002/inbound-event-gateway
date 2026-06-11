"""
The worker: the state machine, the retry ladder, and what happens when the
normaliser fails.

Every normaliser here is a stub. The point of these tests is not what a model
would say about a payload — it is what the pipeline does with the answer, and
with the four ways getting one can fail: a transient error, a permanent one, a
bug in the worker, and a duplicate delivery that should never reach a
normaliser at all.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import patch

import pytest

from apps.entities.models import EntityState
from apps.ingestion.models import ProcessingStatus
from apps.normalization.exceptions import (
    NormalizationError,
    RetryableNormalizationError,
)
from apps.normalization.models import NormalizedEvent
from apps.normalization.tasks import process_raw_webhook
from conftest import StubNormalizer

pytestmark = pytest.mark.django_db


class FlakyNormalizer(StubNormalizer):
    """Fails the first `failures` calls, then answers."""

    def __init__(self, *, failures: int, error: Exception, result):
        super().__init__(result=result)
        self._remaining = failures
        self._error = error

    def normalize(self, payload, *, vendor: str = ""):
        self.calls.append(payload)
        self.vendors.append(vendor)
        if self._remaining > 0:
            self._remaining -= 1
            raise self._error
        return self.respond()


def run(webhook_id, normalizer, **apply_kwargs):
    """Run the task eagerly with a given normaliser."""
    with patch("apps.normalization.tasks.get_normalizer", return_value=normalizer):
        return process_raw_webhook.apply(args=[str(webhook_id)], **apply_kwargs)


class TestTheHappyPath:
    def test_a_confident_normalisation_becomes_entity_state(
        self, make_webhook, normalization_result
    ):
        webhook = make_webhook()
        stub = StubNormalizer(result=normalization_result(confidence=0.92))

        result = run(webhook.id, stub)

        assert result.successful()
        webhook.refresh_from_db()
        assert webhook.processing_status == ProcessingStatus.NORMALIZED
        event = NormalizedEvent.objects.get(webhook=webhook)
        assert event.requires_review is False
        assert EntityState.objects.get(entity_id="SHIP-1").latest_status == "DELIVERED"

    def test_the_normaliser_sees_the_stored_payload(self, make_webhook, normalization_result):
        webhook = make_webhook(payload={"shipment_id": "SHIP-1", "odd": "shape"})
        stub = StubNormalizer(result=normalization_result())

        run(webhook.id, stub)

        assert stub.calls == [{"shipment_id": "SHIP-1", "odd": "shape"}]

    def test_an_unsure_normalisation_is_held_out_of_entity_state(
        self, make_webhook, normalization_result, settings
    ):
        settings.LOW_CONFIDENCE_THRESHOLD = 0.7
        webhook = make_webhook()
        stub = StubNormalizer(result=normalization_result(confidence=0.3))

        run(webhook.id, stub)

        event = NormalizedEvent.objects.get(webhook=webhook)
        assert event.requires_review is True
        assert "0.30" in event.review_reason
        # Recorded in full, but not believed.
        assert not EntityState.objects.exists()
        webhook.refresh_from_db()
        # The webhook itself normalised fine; it is the *reading* that is held.
        assert webhook.processing_status == ProcessingStatus.NORMALIZED


class TestDuplicateWork:
    def test_an_already_normalized_webhook_is_not_normalised_again(
        self, make_webhook, normalization_result
    ):
        webhook = make_webhook(processing_status=ProcessingStatus.NORMALIZED)
        stub = StubNormalizer(result=normalization_result())

        run(webhook.id, stub)

        assert stub.calls == []

    def test_a_webhook_another_worker_holds_is_left_alone(self, make_webhook, normalization_result):
        # The second worker sees PROCESSING under the row lock and backs off;
        # doing the work anyway would double-apply the event.
        webhook = make_webhook(processing_status=ProcessingStatus.PROCESSING)
        stub = StubNormalizer(result=normalization_result())

        run(webhook.id, stub)

        assert stub.calls == []

    def test_a_missing_webhook_is_not_an_error(self, normalization_result):
        # The row can be gone by the time the task runs. That is not a failure
        # worth retrying or alerting on.
        stub = StubNormalizer(result=normalization_result())

        result = run("f5b4f18b-703c-4bbd-9363-2cbb497fdb16", stub)

        assert result.successful()
        assert stub.calls == []


class TestFailureHandling:
    def test_a_permanent_failure_is_recorded_and_not_retried(self, make_webhook):
        stub = StubNormalizer(error=NormalizationError("no identifier in payload"))
        webhook = make_webhook()

        result = run(webhook.id, stub)

        webhook.refresh_from_db()
        assert webhook.processing_status == ProcessingStatus.FAILED
        assert "no identifier" in webhook.error_message
        # Retrying a payload that will never normalise is pure noise.
        assert result.successful()

    def test_a_transient_failure_is_retried_rather_than_abandoned(
        self, make_webhook, normalization_result
    ):
        # Fails once, then succeeds — the shape of a rate limit or a timeout.
        # The event must end up in entity state without anyone intervening.
        webhook = make_webhook()
        flaky = FlakyNormalizer(
            failures=1,
            error=RetryableNormalizationError("429 from the API"),
            result=normalization_result(),
        )

        result = run(webhook.id, flaky)

        webhook.refresh_from_db()
        assert result.successful()
        assert webhook.processing_status == ProcessingStatus.NORMALIZED
        assert webhook.retry_count == 1
        assert EntityState.objects.filter(entity_id="SHIP-1").exists()

    def test_a_permanently_transient_failure_climbs_the_whole_ladder(self, make_webhook):
        stub = StubNormalizer(error=RetryableNormalizationError("still failing"))
        webhook = make_webhook()

        run(webhook.id, stub)

        webhook.refresh_from_db()
        # One first attempt plus max_retries rungs, and then it stops: the
        # bound is what keeps a broken vendor from looping forever.
        assert len(stub.calls) == process_raw_webhook.max_retries + 1
        assert webhook.retry_count == process_raw_webhook.max_retries + 1
        assert webhook.processing_status == ProcessingStatus.DEAD_LETTER

    def test_the_last_rung_dead_letters_instead_of_erroring(self, make_webhook):
        # Celery re-raises the *original* exception rather than
        # MaxRetriesExceededError when retry() is given an exc, so catching
        # only MaxRetriesExceededError left this branch unreachable and the
        # webhook sat in FAILED forever while every delivery errored.
        stub = StubNormalizer(error=RetryableNormalizationError("still failing"))
        webhook = make_webhook()

        result = run(webhook.id, stub, retries=process_raw_webhook.max_retries)

        webhook.refresh_from_db()
        assert webhook.processing_status == ProcessingStatus.DEAD_LETTER
        assert "Max retries exceeded" in webhook.error_message
        # Dead-lettering is the resolution, not a crash loop.
        assert result.successful()

    def test_a_bug_in_the_worker_fails_the_task_loudly(self, make_webhook):
        # Recorded on the webhook *and* re-raised. Swallowing it reported the
        # task as succeeded, so a deploy with a programming error looked like
        # a healthy worker while every webhook silently went to FAILED.
        stub = StubNormalizer(error=TypeError("unhashable type"))
        webhook = make_webhook()

        result = run(webhook.id, stub)

        webhook.refresh_from_db()
        assert webhook.processing_status == ProcessingStatus.FAILED
        assert "Unexpected worker failure" in webhook.error_message
        assert result.failed()
        assert isinstance(result.result, TypeError)


class TestReplay:
    def test_replaying_clears_the_previous_verdict(
        self, make_webhook, normalization_result, settings
    ):
        settings.LOW_CONFIDENCE_THRESHOLD = 0.7
        webhook = make_webhook()
        run(webhook.id, StubNormalizer(result=normalization_result(confidence=0.3)))
        assert NormalizedEvent.objects.get(webhook=webhook).requires_review is True

        # The normaliser improved; replay re-reads the stored payload.
        webhook.processing_status = ProcessingStatus.RECEIVED
        webhook.save(update_fields=["processing_status"])
        run(webhook.id, StubNormalizer(result=normalization_result(confidence=0.95)))

        event = NormalizedEvent.objects.get(webhook=webhook)
        assert event.requires_review is False
        assert event.review_reason == ""
        assert EntityState.objects.get(entity_id="SHIP-1").latest_status == "DELIVERED"

    def test_replaying_does_not_create_a_second_event_for_one_webhook(
        self, make_webhook, normalization_result
    ):
        webhook = make_webhook()
        run(webhook.id, StubNormalizer(result=normalization_result()))
        webhook.processing_status = ProcessingStatus.RECEIVED
        webhook.save(update_fields=["processing_status"])
        run(
            webhook.id,
            StubNormalizer(
                result=normalization_result(
                    canonical_status="IN_TRANSIT",
                    event_time=datetime(2026, 3, 3, 9, 0, tzinfo=UTC),
                )
            ),
        )

        assert NormalizedEvent.objects.filter(webhook=webhook).count() == 1
        assert NormalizedEvent.objects.get(webhook=webhook).canonical_status == "IN_TRANSIT"
