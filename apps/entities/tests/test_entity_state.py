"""
Entity state: the one thing the service actually asserts about the world.

The property that matters is that updates are monotonic in *event* time, not
arrival time. Webhooks arrive out of order all the time — a retried
`in_transit` can land after the `delivered` it preceded — and letting arrival
order win would make the system report a parcel as undelivered after it had
been delivered.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from django.urls import reverse

from apps.entities.models import EntityState
from apps.entities.repositories import EntityStateRepository
from apps.normalization.models import EntityType

pytestmark = pytest.mark.django_db

EARLY = datetime(2026, 3, 1, 8, 0, tzinfo=UTC)
LATER = datetime(2026, 3, 2, 8, 0, tzinfo=UTC)


class TestMonotonicUpdates:
    def test_the_first_event_creates_the_state(self, make_event):
        event = make_event(entity_id="S1", canonical_status="IN_TRANSIT", event_time=EARLY)

        state = EntityStateRepository.upsert_if_newer(event)

        assert state.latest_status == "IN_TRANSIT"
        assert state.latest_event_id == event.id

    def test_a_newer_event_advances_the_state(self, make_event):
        EntityStateRepository.upsert_if_newer(
            make_event(entity_id="S2", canonical_status="IN_TRANSIT", event_time=EARLY)
        )
        newer = make_event(entity_id="S2", canonical_status="DELIVERED", event_time=LATER)

        EntityStateRepository.upsert_if_newer(newer)

        state = EntityState.objects.get(entity_type=EntityType.SHIPMENT, entity_id="S2")
        assert state.latest_status == "DELIVERED"
        assert state.latest_event_id == newer.id

    def test_a_late_arriving_older_event_does_not_win(self, make_event):
        delivered = make_event(entity_id="S3", canonical_status="DELIVERED", event_time=LATER)
        EntityStateRepository.upsert_if_newer(delivered)

        # Arrives second, happened first.
        EntityStateRepository.upsert_if_newer(
            make_event(entity_id="S3", canonical_status="IN_TRANSIT", event_time=EARLY)
        )

        state = EntityState.objects.get(entity_id="S3")
        assert state.latest_status == "DELIVERED"
        assert state.latest_event_id == delivered.id

    def test_a_duplicate_timestamp_does_not_overwrite(self, make_event):
        first = make_event(entity_id="S4", canonical_status="DELIVERED", event_time=LATER)
        EntityStateRepository.upsert_if_newer(first)

        EntityStateRepository.upsert_if_newer(
            make_event(entity_id="S4", canonical_status="IN_TRANSIT", event_time=LATER)
        )

        # Strictly newer wins; a tie leaves what is already believed alone.
        assert EntityState.objects.get(entity_id="S4").latest_status == "DELIVERED"

    def test_one_row_per_entity(self, make_event):
        for status in ("PICKED_UP", "IN_TRANSIT", "DELIVERED"):
            EntityStateRepository.upsert_if_newer(
                make_event(entity_id="S5", canonical_status=status, event_time=LATER)
            )

        assert EntityState.objects.filter(entity_id="S5").count() == 1

    def test_the_same_id_under_two_entity_types_is_two_entities(self, make_event):
        EntityStateRepository.upsert_if_newer(
            make_event(
                entity_id="X",
                entity_type=EntityType.SHIPMENT,
                canonical_status="DELIVERED",
                event_time=LATER,
            )
        )
        EntityStateRepository.upsert_if_newer(
            make_event(
                entity_id="X",
                entity_type=EntityType.INVOICE,
                canonical_status="PAID",
                event_time=LATER,
            )
        )

        assert EntityState.objects.filter(entity_id="X").count() == 2


class TestTheStateEndpoint:
    def url(self, **params):
        query = "&".join(f"{k}={v}" for k, v in params.items())
        return f"{reverse('entity-state')}?{query}"

    def test_it_reports_what_the_system_believes(self, client, make_event):
        event = make_event(entity_id="S6", canonical_status="DELIVERED", event_time=LATER)
        EntityStateRepository.upsert_if_newer(event)

        response = client.get(self.url(entity_type="SHIPMENT", entity_id="S6"))

        assert response.status_code == 200
        assert response.json() == {
            "entity_type": "SHIPMENT",
            "entity_id": "S6",
            "latest_status": "DELIVERED",
            "latest_event_time": response.json()["latest_event_time"],
            "latest_event_id": event.id,
        }

    def test_an_unknown_entity_is_a_404(self, client):
        response = client.get(self.url(entity_type="SHIPMENT", entity_id="NOPE"))
        assert response.status_code == 404

    @pytest.mark.parametrize(
        "params",
        [{}, {"entity_type": "SHIPMENT"}, {"entity_id": "S"}],
        ids=["neither", "type_only", "id_only"],
    )
    def test_both_query_parameters_are_required(self, client, params):
        assert client.get(self.url(**params)).status_code == 400

    def test_a_held_event_is_not_reported_as_state(self, client, make_event):
        # The gate's whole purpose, seen from the read side: an event that was
        # never promoted must not be visible here.
        make_event(entity_id="S7", confidence=0.2)

        assert client.get(self.url(entity_type="SHIPMENT", entity_id="S7")).status_code == 404
