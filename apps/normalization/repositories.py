from __future__ import annotations

from django.db import transaction

from apps.ingestion.models import ProcessingStatus, RawWebhook
from apps.normalization.models import NormalizedEvent, ProfileSource, VendorProfile
from apps.normalization.schemas import NormalizationResult
from apps.normalization.vendors import VendorProfileSpec, invalidate, spec_from_model


class NormalizedEventRepository:
    @staticmethod
    @transaction.atomic
    def create_for_webhook(
        *,
        webhook: RawWebhook,
        result: NormalizationResult,
        llm_model: str,
        prompt_version: str,
    ) -> NormalizedEvent:
        normalized_event, _created = NormalizedEvent.objects.update_or_create(
            webhook=webhook,
            defaults={
                "entity_type": result.entity_type,
                "entity_id": result.entity_id,
                "canonical_status": result.canonical_status,
                "event_time": result.event_time,
                "normalized_payload": result.normalized_payload,
                "confidence_score": result.confidence_score,
                "llm_model": llm_model,
                "prompt_version": prompt_version,
                # A replay re-normalises the stored payload, so the previous
                # run's verdict no longer describes this row. Leaving the flags
                # behind would either keep a now-confident event in the review
                # queue forever, or carry a stale "reviewed" mark onto an
                # interpretation nobody has actually seen. The gate runs again
                # immediately after this and sets them afresh.
                "requires_review": False,
                "review_reason": "",
                "reviewed_at": None,
                "review_decision": "",
            },
        )
        webhook.processing_status = ProcessingStatus.NORMALIZED
        webhook.error_message = ""
        webhook.save(update_fields=["processing_status", "error_message"])
        return normalized_event


class VendorProfileRepository:
    """
    Persistence for vendor profiles.

    Writes go through here rather than through the model directly so the
    cached lookup in `apps.normalization.vendors` is always invalidated with
    the row. A profile that has been edited but is still being read from a
    worker's cache is the kind of bug that only shows up in production.
    """

    @staticmethod
    def save(spec: VendorProfileSpec) -> VendorProfile:
        profile, _created = VendorProfile.objects.update_or_create(
            vendor=spec.vendor,
            defaults={
                "entity_type": spec.entity_type,
                "id_paths": list(spec.id_paths),
                "status_paths": list(spec.status_paths),
                "time_paths": list(spec.time_paths),
                "status_map": dict(spec.status_map),
                "source": spec.source or ProfileSource.MANUAL,
                "sample_count": spec.sample_count,
                "notes": spec.notes,
            },
        )
        invalidate(spec.vendor)
        return profile

    @staticmethod
    def all_specs() -> list[VendorProfileSpec]:
        return [spec_from_model(profile) for profile in VendorProfile.objects.all()]

    @staticmethod
    def delete(vendor: str) -> int:
        deleted, _ = VendorProfile.objects.filter(vendor__iexact=vendor).delete()
        invalidate(vendor)
        return deleted
