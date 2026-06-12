from __future__ import annotations

from apps.entities.models import EntityState


class EntityProjectionService:
    """Read model access for current entity projections."""

    @staticmethod
    def get_current_state(entity_type: str, entity_id: str) -> EntityState | None:
        return (
            EntityState.objects.filter(entity_type=entity_type, entity_id=entity_id)
            .select_related("latest_event")
            .first()
        )
