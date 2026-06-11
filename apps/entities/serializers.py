from __future__ import annotations

from rest_framework import serializers


class EntityStateSerializer(serializers.Serializer):
    entity_type = serializers.CharField()
    entity_id = serializers.CharField()
    latest_status = serializers.CharField()
    latest_event_time = serializers.DateTimeField()
    latest_event_id = serializers.IntegerField(source="latest_event.id")
