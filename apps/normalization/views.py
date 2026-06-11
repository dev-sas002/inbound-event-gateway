from __future__ import annotations

import logging

from django.conf import settings
from drf_spectacular.utils import OpenApiParameter, extend_schema, inline_serializer
from rest_framework import serializers, status
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.normalization.calibration import calibration_report
from apps.normalization.discovery import DiscoveryError, discover_profile
from apps.normalization.models import NormalizedEvent
from apps.normalization.paging import PageError, paginate
from apps.normalization.repositories import VendorProfileRepository
from apps.normalization.review import (
    VALID_DECISIONS,
    AlreadyDecided,
    apply_review_decision,
)
from apps.normalization.serializers import (
    DiscoveryRequestSerializer,
    NormalizedEventSerializer,
    VendorProfileSerializer,
    VendorProfileWriteSerializer,
)
from apps.normalization.vendors import VendorProfileSpec, profile_for

logger = logging.getLogger("apps.normalization")

#: Every listing in this app answers with the same envelope.
_PAGE_FIELDS = {
    "count": serializers.IntegerField(),
    "limit": serializers.IntegerField(),
    "offset": serializers.IntegerField(),
    "next_offset": serializers.IntegerField(allow_null=True),
    "previous_offset": serializers.IntegerField(allow_null=True),
}

_PAGE_PARAMETERS = [
    OpenApiParameter(
        name="limit",
        type=int,
        location=OpenApiParameter.QUERY,
        required=False,
        description="Page size. Defaults to DEFAULT_PAGE_SIZE, capped at MAX_PAGE_SIZE.",
    ),
    OpenApiParameter(
        name="offset",
        type=int,
        location=OpenApiParameter.QUERY,
        required=False,
        description="Row to start from. `next_offset` in a response is the next page.",
    ),
]

#: A queue with four thousand rows in it is not a queue a person can work.
#: Reviewers triage one vendor at a time — usually the one that just started
#: failing — so the queue takes a vendor filter alongside the paging.
_VENDOR_PARAMETER = OpenApiParameter(
    name="vendor",
    type=str,
    location=OpenApiParameter.QUERY,
    required=False,
    description="Only events from this vendor. Matched case-insensitively.",
)


def _error(detail: str, code: int = status.HTTP_400_BAD_REQUEST) -> Response:
    return Response({"detail": detail}, status=code)


class LowConfidenceQueueView(APIView):
    """Inspect low-confidence normalized events for manual review workflows."""

    authentication_classes = []
    permission_classes = []

    @extend_schema(
        operation_id="low_confidence_events",
        summary="List low-confidence normalized events",
        parameters=[
            OpenApiParameter(
                name="threshold",
                type=float,
                location=OpenApiParameter.QUERY,
                required=False,
                description=(
                    "Return events where confidence_score is below this threshold. "
                    "Defaults to the configured LOW_CONFIDENCE_THRESHOLD."
                ),
            ),
            *_PAGE_PARAMETERS,
        ],
        responses={
            200: inline_serializer(
                name="LowConfidenceEventListResponse",
                fields={**_PAGE_FIELDS, "results": NormalizedEventSerializer(many=True)},
            ),
            400: inline_serializer(
                name="LowConfidenceEventListError",
                fields={"detail": serializers.CharField()},
            ),
        },
    )
    def get(self, request: Request, *args, **kwargs) -> Response:
        raw_threshold = request.query_params.get("threshold")
        if raw_threshold is None:
            # The default has to be the gate's own threshold. Hardcoding 0.7
            # here meant that lowering LOW_CONFIDENCE_THRESHOLD silently left
            # this view reporting events the pipeline had in fact trusted.
            threshold = settings.LOW_CONFIDENCE_THRESHOLD
        else:
            try:
                threshold = float(raw_threshold)
            except (TypeError, ValueError):
                # Unparseable input is the caller's mistake, not a server
                # fault; the unguarded float() turned it into a 500.
                return _error("threshold must be a number between 0 and 1.")
            if not 0.0 <= threshold <= 1.0:
                return _error("threshold must be a number between 0 and 1.")

        # select_related because the serializer reads webhook.id and
        # webhook.vendor on every row; without it a page is N+1 queries.
        events = (
            NormalizedEvent.objects.select_related("webhook")
            .filter(confidence_score__lt=threshold)
            .order_by("-created_at")
        )
        try:
            page = paginate(events, request.query_params)
        except PageError as exc:
            return _error(str(exc))
        return Response(
            page.envelope(NormalizedEventSerializer(page.items, many=True).data),
            status=status.HTTP_200_OK,
        )


class ReviewQueueView(APIView):
    """
    Events the pipeline declined to trust.

    Distinct from the threshold query above: this lists what the *gate* held at
    the time it ran, which is the durable decision. Re-querying by a threshold
    today would silently reclassify history whenever the threshold changed.
    """

    authentication_classes = []
    permission_classes = []

    @extend_schema(
        operation_id="review_queue",
        summary="List normalized events held for human review",
        parameters=[*_PAGE_PARAMETERS, _VENDOR_PARAMETER],
        responses={
            200: inline_serializer(
                name="ReviewQueueResponse",
                fields={**_PAGE_FIELDS, "results": NormalizedEventSerializer(many=True)},
            )
        },
    )
    def get(self, request: Request, *args, **kwargs) -> Response:
        events = (
            NormalizedEvent.objects.select_related("webhook")
            .filter(requires_review=True, reviewed_at__isnull=True)
            .order_by("created_at")
        )
        vendor = (request.query_params.get("vendor") or "").strip()
        if vendor:
            # iexact rather than exact: vendors are whatever the sender put in
            # the header, and "Maersk" and "maersk" are the same carrier.
            events = events.filter(webhook__vendor__iexact=vendor)
        try:
            page = paginate(events, request.query_params)
        except PageError as exc:
            return _error(str(exc))
        return Response(
            page.envelope(NormalizedEventSerializer(page.items, many=True).data),
            status=status.HTTP_200_OK,
        )


class ReviewDecisionView(APIView):
    """Accept or reject one held event."""

    authentication_classes = []
    permission_classes = []

    @extend_schema(
        operation_id="review_decision",
        summary="Approve or reject a held event",
        request=inline_serializer(
            name="ReviewDecisionRequest",
            fields={"decision": serializers.ChoiceField(choices=["approve", "reject"])},
        ),
        responses={
            200: inline_serializer(
                name="ReviewDecisionResponse",
                fields={
                    "id": serializers.IntegerField(),
                    "decision": serializers.CharField(),
                    "entity_state_updated": serializers.BooleanField(),
                },
            ),
            409: inline_serializer(
                name="ReviewDecisionConflict",
                fields={"detail": serializers.CharField()},
            ),
        },
    )
    def post(self, request: Request, event_id: int, *args, **kwargs) -> Response:
        body = request.data if isinstance(request.data, dict) else {}
        decision = str(body.get("decision", "")).lower()
        if decision not in VALID_DECISIONS:
            return _error('decision must be "approve" or "reject"')

        try:
            event = NormalizedEvent.objects.get(id=event_id, requires_review=True)
        except NormalizedEvent.DoesNotExist:
            return _error("No held event with that id.", status.HTTP_404_NOT_FOUND)

        try:
            updated = apply_review_decision(event, decision, actor="api")
        except AlreadyDecided as exc:
            # A decision is final. Re-deciding would promote the event to
            # entity state a second time and rewrite its audit trail.
            return _error(str(exc), status.HTTP_409_CONFLICT)

        return Response(
            {"id": event.id, "decision": decision, "entity_state_updated": updated},
            status=status.HTTP_200_OK,
        )


class CalibrationView(APIView):
    """
    Whether the confidence threshold is in the right place, per vendor.

    Reads the decisions reviewers actually made and reports where confidence
    did and did not separate the approvals from the rejections. See
    `apps.normalization.calibration` for what the numbers can and cannot prove.
    """

    authentication_classes = []
    permission_classes = []

    @extend_schema(
        operation_id="calibration_report",
        summary="Per-vendor confidence gate calibration",
        responses={
            200: inline_serializer(
                name="CalibrationResponse",
                fields={
                    "current_threshold": serializers.FloatField(),
                    "count": serializers.IntegerField(),
                    "results": serializers.ListField(child=serializers.DictField()),
                },
            )
        },
    )
    def get(self, request: Request, *args, **kwargs) -> Response:
        return Response(calibration_report().as_dict(), status=status.HTTP_200_OK)


class VendorProfileListView(APIView):
    """The profiles the normaliser will use, whichever backend is running."""

    authentication_classes = []
    permission_classes = []

    @extend_schema(
        operation_id="vendor_profiles",
        summary="List stored vendor profiles",
        responses={
            200: inline_serializer(
                name="VendorProfileListResponse",
                fields={
                    "count": serializers.IntegerField(),
                    "results": VendorProfileSerializer(many=True),
                },
            )
        },
    )
    def get(self, request: Request, *args, **kwargs) -> Response:
        specs = VendorProfileRepository.all_specs()
        results = [spec.as_dict() for spec in specs]
        return Response({"count": len(results), "results": results}, status=status.HTTP_200_OK)


class VendorProfileDetailView(APIView):
    """Read, write or remove one vendor's profile."""

    authentication_classes = []
    permission_classes = []

    @extend_schema(
        operation_id="vendor_profile_detail",
        summary="Get one vendor profile",
        responses={200: VendorProfileSerializer, 404: None},
    )
    def get(self, request: Request, vendor: str, *args, **kwargs) -> Response:
        spec = profile_for(vendor)
        if spec is None:
            return _error("No profile for that vendor.", status.HTTP_404_NOT_FOUND)
        return Response(spec.as_dict(), status=status.HTTP_200_OK)

    @extend_schema(
        operation_id="vendor_profile_upsert",
        summary="Create or replace one vendor profile",
        request=VendorProfileWriteSerializer,
        responses={200: VendorProfileSerializer, 400: None},
    )
    def put(self, request: Request, vendor: str, *args, **kwargs) -> Response:
        serializer = VendorProfileWriteSerializer(data=request.data or {})
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        spec = VendorProfileSpec(
            vendor=vendor,
            entity_type=data["entity_type"],
            id_paths=tuple(data["id_paths"]),
            status_paths=tuple(data["status_paths"]),
            time_paths=tuple(data["time_paths"]),
            status_map=data["status_map"],
            source="MANUAL",
            notes=data["notes"],
        )
        VendorProfileRepository.save(spec)
        logger.info("vendor_profile_saved", extra={"vendor": vendor, "source": "MANUAL"})
        return Response(spec.as_dict(), status=status.HTTP_200_OK)

    @extend_schema(
        operation_id="vendor_profile_delete",
        summary="Delete one vendor profile",
        responses={204: None, 404: None},
    )
    def delete(self, request: Request, vendor: str, *args, **kwargs) -> Response:
        if not VendorProfileRepository.delete(vendor):
            return _error("No profile for that vendor.", status.HTTP_404_NOT_FOUND)
        logger.info("vendor_profile_deleted", extra={"vendor": vendor})
        return Response(status=status.HTTP_204_NO_CONTENT)


class VendorDiscoveryView(APIView):
    """
    Propose a profile for a vendor from sample payloads.

    The response is a proposal, not a change: `persist` has to be asked for
    explicitly, because what discovery reads out of three payloads is a
    hypothesis about a vendor and somebody should look at it.
    """

    authentication_classes = []
    permission_classes = []

    @extend_schema(
        operation_id="discover_vendor_profile",
        summary="Infer a vendor profile from sample payloads",
        request=DiscoveryRequestSerializer,
        responses={
            200: inline_serializer(
                name="DiscoveryResponse",
                fields={
                    "method": serializers.CharField(),
                    "sample_count": serializers.IntegerField(),
                    "persisted": serializers.BooleanField(),
                    "unmapped_statuses": serializers.ListField(child=serializers.CharField()),
                    "warnings": serializers.ListField(child=serializers.CharField()),
                    "profile": VendorProfileSerializer(),
                },
            ),
            400: inline_serializer(
                name="DiscoveryError", fields={"detail": serializers.CharField()}
            ),
        },
    )
    def post(self, request: Request, *args, **kwargs) -> Response:
        serializer = DiscoveryRequestSerializer(data=request.data or {})
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        try:
            result = discover_profile(
                data["vendor"], list(data["samples"]), use_model=data.get("use_model")
            )
        except DiscoveryError as exc:
            return _error(str(exc))

        persisted = False
        if data["persist"]:
            VendorProfileRepository.save(result.profile)
            persisted = True

        logger.info(
            "vendor_profile_discovered",
            extra={
                "vendor": data["vendor"],
                "method": result.method,
                "sample_count": result.sample_count,
                "persisted": persisted,
            },
        )
        return Response({**result.as_dict(), "persisted": persisted}, status=status.HTTP_200_OK)
