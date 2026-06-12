from django.contrib import admin

from apps.entities.models import EntityState


@admin.register(EntityState)
class EntityStateAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "entity_type",
        "entity_id",
        "latest_status",
        "latest_event_time",
        "latest_event",
        "updated_at",
    )
    list_filter = ("entity_type", "latest_status", "updated_at")
    search_fields = ("entity_type", "entity_id", "latest_status")
    readonly_fields = ("updated_at",)
