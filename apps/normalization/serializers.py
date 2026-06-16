from __future__ import annotations

from rest_framework import serializers

from apps.normalization.models import EntityType


class NormalizedEventSerializer(serializers.Serializer):
    # `id` is needed so a caller can act on a held event via the review
    # endpoint; without it the queue is readable but not actionable.
    id = serializers.IntegerField()
    webhook_id = serializers.UUIDField(source="webhook.id")
    vendor = serializers.CharField(source="webhook.vendor")
    entity_type = serializers.CharField()
    entity_id = serializers.CharField()
    canonical_status = serializers.CharField()
    event_time = serializers.DateTimeField()
    confidence_score = serializers.FloatField()
    llm_model = serializers.CharField()
    prompt_version = serializers.CharField()
    created_at = serializers.DateTimeField()
    requires_review = serializers.BooleanField()
    review_reason = serializers.CharField()
    review_decision = serializers.CharField()
    reviewed_at = serializers.DateTimeField(allow_null=True)


class VendorProfileSerializer(serializers.Serializer):
    vendor = serializers.CharField()
    entity_type = serializers.CharField(allow_blank=True)
    id_paths = serializers.ListField(child=serializers.CharField())
    status_paths = serializers.ListField(child=serializers.CharField())
    time_paths = serializers.ListField(child=serializers.CharField())
    status_map = serializers.DictField(child=serializers.CharField())
    source = serializers.CharField()
    sample_count = serializers.IntegerField()
    notes = serializers.CharField(allow_blank=True)


class DiscoveryRequestSerializer(serializers.Serializer):
    """What a caller has to supply to get a profile proposed."""

    vendor = serializers.CharField(max_length=128)
    samples = serializers.ListField(
        child=serializers.DictField(),
        min_length=1,
        max_length=50,
        help_text="Real payloads from this vendor. Two or three is usually enough.",
    )
    persist = serializers.BooleanField(
        required=False,
        default=False,
        help_text="Save the proposal as this vendor's profile instead of only returning it.",
    )
    use_model = serializers.BooleanField(
        required=False,
        allow_null=True,
        default=None,
        help_text=(
            "Force the model-assisted reader on or off. Defaults to on when an "
            "OpenAI key is configured, off otherwise."
        ),
    )


class VendorProfileWriteSerializer(serializers.Serializer):
    """A profile written by hand, or a discovered one a person has corrected."""

    entity_type = serializers.ChoiceField(
        choices=[*EntityType.values, ""], required=False, allow_blank=True, default=""
    )
    id_paths = serializers.ListField(child=serializers.CharField(), required=False, default=list)
    status_paths = serializers.ListField(
        child=serializers.CharField(), required=False, default=list
    )
    time_paths = serializers.ListField(child=serializers.CharField(), required=False, default=list)
    status_map = serializers.DictField(child=serializers.CharField(), required=False, default=dict)
    notes = serializers.CharField(required=False, allow_blank=True, default="")

    def validate(self, attrs: dict) -> dict:
        """
        A status may only map onto a status the entity type actually has.

        Checked here rather than at normalisation time: a profile that maps a
        word onto a value the schema rejects would not fail until a live
        webhook arrived, and then it would fail every time.
        """
        from apps.normalization.schemas import allowed_statuses

        entity_type = attrs.get("entity_type") or ""
        allowed = allowed_statuses(entity_type)
        offending = {
            token: value
            for token, value in (attrs.get("status_map") or {}).items()
            if str(value).upper() not in allowed
        }
        if offending:
            raise serializers.ValidationError(
                {
                    "status_map": (
                        f"{sorted(offending)} do not map to a canonical "
                        f"{entity_type or 'entity'} status. Allowed: {sorted(allowed)}."
                    )
                }
            )
        attrs["status_map"] = {
            str(token).lower(): str(value).upper()
            for token, value in (attrs.get("status_map") or {}).items()
        }
        return attrs
