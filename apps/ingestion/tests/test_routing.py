"""
The noisy-vendor bulkhead.

What matters here is not that a counter counts, but that a vendor's burst is
moved off the lane every other vendor shares — and that the mechanism is never
able to break ingestion when the cache it depends on is unavailable.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from django.urls import reverse

from apps.ingestion.routing import queue_for_vendor, record_arrival, route_for

pytestmark = pytest.mark.django_db


@pytest.fixture
def enqueue():
    with patch("apps.ingestion.services.process_raw_webhook.apply_async") as mock:
        yield mock


class TestArrivalCounting:
    def test_arrivals_accumulate_within_one_window(self, settings):
        settings.NOISY_VENDOR_WINDOW_SECONDS = 60
        counts = [record_arrival("acme", now=1_000_000.0) for _ in range(3)]
        assert counts == [1, 2, 3]

    def test_a_new_window_starts_from_zero(self, settings):
        settings.NOISY_VENDOR_WINDOW_SECONDS = 60
        record_arrival("acme", now=1_000_000.0)
        record_arrival("acme", now=1_000_000.0)
        # The next window is a fresh count: a vendor that was loud a minute ago
        # is not still being punished for it.
        assert record_arrival("acme", now=1_000_060.0) == 1

    def test_vendors_are_counted_separately(self, settings):
        settings.NOISY_VENDOR_WINDOW_SECONDS = 60
        record_arrival("loud", now=1_000_000.0)
        record_arrival("loud", now=1_000_000.0)
        assert record_arrival("quiet", now=1_000_000.0) == 1

    def test_vendor_names_are_case_insensitive(self, settings):
        settings.NOISY_VENDOR_WINDOW_SECONDS = 60
        record_arrival("Acme", now=1_000_000.0)
        assert record_arrival("ACME", now=1_000_000.0) == 2

    def test_an_unreachable_cache_does_not_raise(self):
        # Ingestion must not fail because a rate counter is having a bad day.
        with patch("apps.ingestion.routing.cache.add", side_effect=RuntimeError("down")):
            assert record_arrival("acme") == 0


class TestQueueSelection:
    def test_a_normal_vendor_stays_on_the_default_lane(self, settings):
        settings.NOISY_VENDOR_BURST = 10
        assert queue_for_vendor("acme", 1) == settings.NORMALIZATION_QUEUE
        assert queue_for_vendor("acme", 10) == settings.NORMALIZATION_QUEUE

    def test_crossing_the_burst_moves_the_vendor_to_the_bulk_lane(self, settings):
        settings.NOISY_VENDOR_BURST = 10
        assert queue_for_vendor("acme", 11) == settings.NORMALIZATION_BULK_QUEUE

    def test_bulkheading_can_be_switched_off(self, settings):
        # Zero means "one lane", which is what a deployment with a single
        # worker wants; it must not route everything to a queue nobody reads.
        settings.NOISY_VENDOR_BURST = 0
        assert queue_for_vendor("acme", 10_000) == settings.NORMALIZATION_QUEUE

    def test_a_degraded_counter_keeps_the_default_lane(self, settings):
        settings.NOISY_VENDOR_BURST = 10
        with patch("apps.ingestion.routing.cache.add", side_effect=RuntimeError("down")):
            assert route_for("acme") == settings.NORMALIZATION_QUEUE


class TestRoutingThroughIngestion:
    def _post(self, client, callbacks, index: int, vendor: str = "loud"):
        # Dispatch happens in on_commit, so the callbacks have to be run for
        # the queue choice to be observable at all.
        with callbacks(execute=True):
            return client.post(
                reverse("webhook-ingest"),
                data=json.dumps({"shipment_id": f"S-{index}", "status": "delivered"}),
                content_type="application/json",
                headers={"X-Webhook-Vendor": vendor},
            )

    def test_a_burst_is_dispatched_to_the_bulk_queue(
        self, client, enqueue, settings, django_capture_on_commit_callbacks
    ):
        settings.NOISY_VENDOR_BURST = 3
        for index in range(5):
            self._post(client, django_capture_on_commit_callbacks, index)

        queues = [call.kwargs["queue"] for call in enqueue.call_args_list]
        assert queues[:3] == [settings.NORMALIZATION_QUEUE] * 3
        assert queues[3:] == [settings.NORMALIZATION_BULK_QUEUE] * 2

    def test_a_quiet_vendor_is_unaffected_by_a_loud_one(
        self, client, enqueue, settings, django_capture_on_commit_callbacks
    ):
        settings.NOISY_VENDOR_BURST = 2
        for index in range(4):
            self._post(client, django_capture_on_commit_callbacks, index, vendor="loud")
        self._post(client, django_capture_on_commit_callbacks, 99, vendor="quiet")

        # The whole point of the bulkhead: the quiet vendor still gets the
        # short lane even while the loud one is being throttled off it.
        assert enqueue.call_args_list[-1].kwargs["queue"] == settings.NORMALIZATION_QUEUE
