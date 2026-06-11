"""
The operational edges: bulk recovery, the admin's replay action, and the
correlation id that ties a request to the log lines it produced.

These are the parts someone reaches for at 3am, which is exactly when a
silently skipped row or a missing id costs the most.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from django.contrib import admin
from django.urls import reverse

from apps.ingestion.admin import RawWebhookAdmin
from apps.ingestion.models import ProcessingStatus, RawWebhook
from apps.ingestion.tasks import replay_failed_webhooks

pytestmark = pytest.mark.django_db


@pytest.fixture
def enqueue():
    with patch("apps.ingestion.services.process_raw_webhook.apply_async") as mock:
        yield mock


class TestBulkReplay:
    def test_it_replays_failed_and_dead_lettered_webhooks(
        self, make_webhook, enqueue, django_capture_on_commit_callbacks
    ):
        failed = make_webhook(processing_status=ProcessingStatus.FAILED)
        dead = make_webhook(processing_status=ProcessingStatus.DEAD_LETTER)
        healthy = make_webhook(processing_status=ProcessingStatus.NORMALIZED)

        with django_capture_on_commit_callbacks(execute=True):
            replayed = replay_failed_webhooks()

        assert replayed == 2
        for webhook in (failed, dead):
            webhook.refresh_from_db()
            assert webhook.processing_status == ProcessingStatus.RECEIVED
        healthy.refresh_from_db()
        assert healthy.processing_status == ProcessingStatus.NORMALIZED
        assert enqueue.call_count == 2

    def test_the_batch_is_bounded(self, make_webhook, enqueue):
        for _ in range(4):
            make_webhook(processing_status=ProcessingStatus.FAILED)

        # An unbounded sweep would enqueue the entire backlog at once.
        assert replay_failed_webhooks(limit=2) == 2

    def test_nothing_to_replay_is_not_an_error(self, enqueue):
        assert replay_failed_webhooks() == 0


class TestAdminReplayAction:
    def _admin(self):
        return RawWebhookAdmin(RawWebhook, admin.site)

    def test_it_recovers_a_webhook_stuck_in_processing(
        self, rf, django_user_model, make_webhook, enqueue
    ):
        # A worker that dies mid-task leaves the row in PROCESSING, where the
        # bulk sweep will not look and an unforced replay refuses to touch it.
        # The admin is the last place it can be recovered from.
        stuck = make_webhook(processing_status=ProcessingStatus.PROCESSING)
        request = rf.post("/admin/")
        request.user = django_user_model.objects.create_superuser(
            username="ops", email="ops@example.com", password="x"
        )

        with patch.object(RawWebhookAdmin, "message_user"):
            self._admin().replay_selected(request, RawWebhook.objects.all())

        stuck.refresh_from_db()
        assert stuck.processing_status == ProcessingStatus.RECEIVED


class TestCorrelationId:
    def test_a_supplied_id_is_echoed_back(self, client):
        response = client.get(reverse("health"), headers={"X-Correlation-ID": "trace-me-123"})

        assert response["X-Correlation-ID"] == "trace-me-123"

    def test_one_is_generated_when_the_caller_sends_none(self, client):
        response = client.get(reverse("health"))

        assert response["X-Correlation-ID"]

    def test_each_request_gets_its_own(self, client):
        first = client.get(reverse("health"))["X-Correlation-ID"]
        second = client.get(reverse("health"))["X-Correlation-ID"]

        assert first != second
