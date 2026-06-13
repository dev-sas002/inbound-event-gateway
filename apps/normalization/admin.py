from django.contrib import admin, messages
from django.utils.html import format_html

from apps.normalization.models import NormalizedEvent, VendorProfile
from apps.normalization.review import APPROVE, REJECT, apply_review_decision
from apps.normalization.vendors import invalidate


@admin.register(NormalizedEvent)
class NormalizedEventAdmin(admin.ModelAdmin):
    """
    The review console.

    The gate is only useful if someone can work the queue, and a separate
    frontend would be a lot of ceremony for a table with two buttons. The
    actions here call the same apply_review_decision() the API does, so a
    decision made in the admin and one made over HTTP cannot diverge.
    """

    list_display = (
        "id",
        "entity_type",
        "entity_id",
        "canonical_status",
        "confidence",
        "review_state",
        "vendor",
        "event_time",
        "llm_model",
        "created_at",
    )
    list_filter = (
        "requires_review",
        "review_decision",
        "entity_type",
        "canonical_status",
        "llm_model",
        "prompt_version",
        "created_at",
    )
    search_fields = ("entity_id", "webhook__id", "canonical_status", "llm_model")
    readonly_fields = ("created_at", "reviewed_at", "review_reason", "review_decision")
    list_select_related = ("webhook",)
    actions = ("approve_selected", "reject_selected")

    @admin.display(description="vendor", ordering="webhook__vendor")
    def vendor(self, obj: NormalizedEvent) -> str:
        return obj.webhook.vendor

    @admin.display(description="confidence", ordering="confidence_score")
    def confidence(self, obj: NormalizedEvent) -> str:
        # Colour tracks the gate's own decision rather than a threshold
        # hardcoded here, so the display cannot disagree with the pipeline.
        colour = "#b91c1c" if obj.requires_review else "#15803d"
        # format_html escapes each argument into a SafeString before applying
        # the format string, so a numeric spec like {:.2f} cannot be used on an
        # argument. Round first, interpolate second.
        return format_html('<b style="color:{}">{}</b>', colour, f"{obj.confidence_score:.2f}")

    @admin.display(description="review")
    def review_state(self, obj: NormalizedEvent) -> str:
        if not obj.requires_review:
            return format_html('<span style="color:#15803d">auto-applied</span>')
        if obj.reviewed_at:
            return format_html('<span style="color:#6b7280">decided</span>')
        return format_html('<b style="color:#b45309">awaiting review</b>')

    def _decide(self, request, queryset, decision: str) -> None:
        # Only events the gate actually held, and only ones nobody has ruled on
        # yet. Re-deciding a settled event would rewrite history.
        pending = queryset.filter(requires_review=True, reviewed_at__isnull=True)
        skipped = queryset.count() - pending.count()

        applied = 0
        for event in pending:
            if apply_review_decision(event, decision, actor=str(request.user)):
                applied += 1

        self.message_user(
            request,
            f"{decision}d {pending.count()} event(s); {applied} updated entity state."
            + (f" Skipped {skipped} not awaiting review." if skipped else ""),
            messages.SUCCESS,
        )

    @admin.action(description="Approve — let these update entity state")
    def approve_selected(self, request, queryset):
        self._decide(request, queryset, APPROVE)

    @admin.action(description="Reject — leave entity state untouched")
    def reject_selected(self, request, queryset):
        self._decide(request, queryset, REJECT)


@admin.register(VendorProfile)
class VendorProfileAdmin(admin.ModelAdmin):
    """
    What the normaliser has been told about each vendor.

    Editable here because a discovered profile is a proposal: the common
    workflow is to run discovery, read the status map, fill in the words it
    left unmapped, and save.
    """

    list_display = (
        "vendor",
        "entity_type",
        "source",
        "mapped_statuses",
        "sample_count",
        "updated_at",
    )
    list_filter = ("source", "entity_type")
    search_fields = ("vendor", "notes")
    readonly_fields = ("created_at", "updated_at")

    @admin.display(description="mapped statuses")
    def mapped_statuses(self, obj: VendorProfile) -> int:
        return len(obj.status_map or {})

    def save_model(self, request, obj, form, change):
        # Through the repository so the cached lookup in
        # apps.normalization.vendors cannot keep serving the old profile.
        super().save_model(request, obj, form, change)
        invalidate(obj.vendor)

    def delete_model(self, request, obj):
        vendor = obj.vendor
        super().delete_model(request, obj)
        invalidate(vendor)

    def delete_queryset(self, request, queryset):
        vendors = list(queryset.values_list("vendor", flat=True))
        super().delete_queryset(request, queryset)
        for vendor in vendors:
            invalidate(vendor)
