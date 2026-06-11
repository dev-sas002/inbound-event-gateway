from __future__ import annotations

from django.db import models


class EntityState(models.Model):
    entity_type = models.CharField(max_length=32)
    entity_id = models.CharField(max_length=255)
    latest_status = models.CharField(max_length=64)
    latest_event_time = models.DateTimeField(db_index=True)
    latest_event = models.ForeignKey(
        "normalization.NormalizedEvent",
        on_delete=models.CASCADE,
        related_name="state_snapshots",
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["entity_type", "entity_id"], name="uq_entity_type_id"),
        ]
        indexes = [
            models.Index(fields=["entity_type", "entity_id"], name="entity_lookup_idx"),
            models.Index(fields=["latest_event_time"], name="entity_latest_time_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.entity_type}:{self.entity_id}:{self.latest_status}"
