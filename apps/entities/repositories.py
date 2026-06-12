from __future__ import annotations

from django.db import transaction

from apps.entities.models import EntityState
from apps.normalization.models import NormalizedEvent


class EntityStateRepository:
    """Maintains entity latest-state with out-of-order protection."""

    @staticmethod
    @transaction.atomic
    def upsert_if_newer(event: NormalizedEvent) -> EntityState:
        state, created = EntityState.objects.select_for_update().get_or_create(
            entity_type=event.entity_type,
            entity_id=event.entity_id,
            defaults={
                "latest_status": event.canonical_status,
                "latest_event_time": event.event_time,
                "latest_event": event,
            },
        )

        if created:
            return state

        if event.event_time > state.latest_event_time:
            state.latest_status = event.canonical_status
            state.latest_event_time = event.event_time
            state.latest_event = event
            state.save(
                update_fields=[
                    "latest_status",
                    "latest_event_time",
                    "latest_event",
                    "updated_at",
                ]
            )

        return state
