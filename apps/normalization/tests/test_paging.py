"""
Paging on the listings.

The bug these replace was quiet and expensive: `count` reported the size of the
page rather than the size of the queue, so a reviewer who cleared 100 events
believed the queue was empty when there were thousands behind it.
"""

from __future__ import annotations

import pytest
from django.urls import reverse

pytestmark = pytest.mark.django_db


@pytest.fixture
def held_events(make_webhook, make_event):
    def _make(total: int, confidence: float = 0.3, vendor: str = "acme"):
        events = []
        for index in range(total):
            event = make_event(
                webhook=make_webhook(vendor=vendor),
                confidence=confidence,
                entity_id=f"E-{index:03d}",
            )
            event.requires_review = True
            event.save(update_fields=["requires_review"])
            events.append(event)
        return events

    return _make


class TestTheReviewQueue:
    def test_count_is_the_whole_queue_not_the_page(self, client, held_events):
        held_events(7)
        body = client.get(reverse("review-queue"), {"limit": 3}).json()
        assert body["count"] == 7
        assert len(body["results"]) == 3

    def test_the_next_offset_walks_the_queue(self, client, held_events):
        held_events(7)
        seen: list[int] = []
        offset: int | None = 0
        while offset is not None:
            body = client.get(reverse("review-queue"), {"limit": 3, "offset": offset}).json()
            seen.extend(row["id"] for row in body["results"])
            offset = body["next_offset"]
        # Every held event is reachable, exactly once.
        assert len(seen) == len(set(seen)) == 7

    def test_the_last_page_says_there_is_no_next(self, client, held_events):
        held_events(3)
        body = client.get(reverse("review-queue"), {"limit": 5}).json()
        assert body["next_offset"] is None
        assert body["previous_offset"] is None

    def test_a_page_size_beyond_the_cap_is_refused(self, client, settings):
        settings.MAX_PAGE_SIZE = 10
        response = client.get(reverse("review-queue"), {"limit": 5000})
        # A caller must not be able to ask for the whole table in one request.
        assert response.status_code == 400

    def test_a_nonsense_page_size_is_a_client_error(self, client):
        assert client.get(reverse("review-queue"), {"limit": "many"}).status_code == 400
        assert client.get(reverse("review-queue"), {"limit": 0}).status_code == 400
        assert client.get(reverse("review-queue"), {"offset": -1}).status_code == 400

    def test_the_default_page_size_is_configuration(self, client, held_events, settings):
        settings.DEFAULT_PAGE_SIZE = 2
        held_events(5)
        assert len(client.get(reverse("review-queue")).json()["results"]) == 2

    def test_a_row_carries_its_vendor(self, client, held_events):
        held_events(1)
        row = client.get(reverse("review-queue")).json()["results"][0]
        # Without it a reviewer cannot tell whose payload they are judging.
        assert row["vendor"] == "acme"


class TestFilteringTheQueueByVendor:
    """
    A reviewer triages one vendor at a time, usually the one that just broke.
    Without this the only way to reach a vendor's events is to page through
    everybody else's.
    """

    def test_only_that_vendors_events_come_back(self, client, held_events):
        held_events(3, vendor="acme")
        held_events(2, vendor="globex")
        body = client.get(reverse("review-queue"), {"vendor": "globex"}).json()
        assert body["count"] == 2
        assert {row["vendor"] for row in body["results"]} == {"globex"}

    def test_the_count_is_the_filtered_total_not_the_whole_queue(self, client, held_events):
        held_events(5, vendor="acme")
        held_events(4, vendor="globex")
        body = client.get(reverse("review-queue"), {"vendor": "globex", "limit": 2}).json()
        # If count ignored the filter the reviewer would page off the end.
        assert body["count"] == 4
        assert body["next_offset"] == 2

    def test_case_does_not_matter(self, client, held_events):
        held_events(2, vendor="Maersk")
        # The vendor is whatever the sender put in a header; it is not a slug.
        assert client.get(reverse("review-queue"), {"vendor": "maersk"}).json()["count"] == 2

    def test_a_blank_vendor_is_not_a_filter(self, client, held_events):
        held_events(3, vendor="acme")
        assert client.get(reverse("review-queue"), {"vendor": "  "}).json()["count"] == 3

    def test_an_unknown_vendor_is_an_empty_queue_not_an_error(self, client, held_events):
        held_events(3, vendor="acme")
        response = client.get(reverse("review-queue"), {"vendor": "nobody"})
        assert response.status_code == 200
        assert response.json()["count"] == 0


class TestTheLowConfidenceListing:
    def test_it_pages_too(self, client, held_events):
        held_events(4, confidence=0.2)
        body = client.get(reverse("low-confidence-events"), {"limit": 2}).json()
        assert body["count"] == 4
        assert len(body["results"]) == 2

    def test_the_threshold_still_filters(self, client, held_events):
        held_events(2, confidence=0.2)
        held_events(3, confidence=0.9)
        body = client.get(reverse("low-confidence-events"), {"threshold": 0.5}).json()
        assert body["count"] == 2

    def test_an_invalid_threshold_is_still_a_client_error(self, client):
        assert client.get(reverse("low-confidence-events"), {"threshold": "x"}).status_code == 400
        assert client.get(reverse("low-confidence-events"), {"threshold": 2}).status_code == 400


class TestQueryCount:
    def test_a_page_does_not_grow_queries_with_rows(
        self, client, held_events, django_assert_max_num_queries
    ):
        held_events(20)
        # Two queries: the COUNT and the page. The serializer reads
        # webhook.vendor on every row, which without select_related would be
        # one more query per row.
        with django_assert_max_num_queries(3):
            client.get(reverse("review-queue"), {"limit": 20})
