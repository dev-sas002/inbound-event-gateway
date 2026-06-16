from __future__ import annotations

from rest_framework import serializers


class WebhookAcceptedSerializer(serializers.Serializer):
    status = serializers.CharField()
    webhook_id = serializers.UUIDField()


class HealthSerializer(serializers.Serializer):
    status = serializers.CharField()


class ReplayWebhookSerializer(serializers.Serializer):
    force = serializers.BooleanField(required=False, default=False)


class ReplayWebhookResponseSerializer(serializers.Serializer):
    status = serializers.CharField()
    webhook_id = serializers.UUIDField()
    processing_status = serializers.CharField()


class ErrorSerializer(serializers.Serializer):
    detail = serializers.CharField()
