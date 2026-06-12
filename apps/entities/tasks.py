from __future__ import annotations

import logging

from celery import shared_task

from apps.entities.models import EntityState

logger = logging.getLogger("apps.entities")


@shared_task
def log_entity_state_count() -> int:
    """Operational helper task for visibility into entity projection growth."""
    count = EntityState.objects.count()
    logger.info("entity_state_count", extra={"count": count})
    return count
