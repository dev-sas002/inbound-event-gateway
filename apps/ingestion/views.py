from __future__ import annotations

import logging
from typing import Any

from django.shortcuts import get_object_or_404
from drf_spectacular.utils import OpenApiExample, OpenApiResponse, extend_schema
from rest_framework import status
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.ingestion.inbound import InvalidPayload, read_request
from apps.ingestion.models import RawWebhook
from apps.ingestion.security import SIGNATURE_HEADER, SignatureError, verify_signature
from apps.ingestion.serializers import (
    ErrorSerializer,
    HealthSerializer,
    ReplayWebhookResponseSerializer,
    ReplayWebhookSerializer,
    WebhookAcceptedSerializer,
)
from apps.ingestion.services import IngestionService, ReplayService

logger = logging.getLogger("apps.ingestion")


class WebhookIngestionView(APIView):
    authentication_classes: list[Any] = []
    permission_classes: list[Any] = []

    @extend_schema(
        operation_id="ingest_webhook",
        summary="Ingest vendor webhook payload",
        description=(
            "Accepts arbitrary JSON payload, persists raw webhook with idempotency protection, "
            "and enqueues asynchronous normalization."
        ),
        request={"application/json": {}},
        responses={
            202: OpenApiResponse(response=WebhookAcceptedSerializer),
            400: OpenApiResponse(response=ErrorSerializer),
            401: OpenApiResponse(response=ErrorSerializer),
        },
        examples=[
            OpenApiExample(
                "Accepted",
                value={"status": "accepted", "webhook_id": "f5b4f18b-703c-4bbd-9363-2cbb497fdb16"},
                response_only=True,
            )
        ],
    )
    def post(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        # First statement in the handler, before anything reads request.data:
        # parsing consumes the request stream, and Django will then refuse to
        # return the body at all. The signature is over these exact bytes, so
        # losing them means every signed webhook fails.
        raw_body = request._request.body

        try:
            inbound = read_request(request._request, request.data, raw_body, SIGNATURE_HEADER)
        except InvalidPayload as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        try:
            verify_signature(
                vendor=inbound.vendor,
                body=inbound.raw_body,
                provided=inbound.signature,
            )
        except SignatureError as exc:
            logger.warning("webhook_signature_rejected", extra={"vendor": inbound.vendor})
            return Response({"detail": str(exc)}, status=status.HTTP_401_UNAUTHORIZED)

        result = IngestionService.ingest(
            payload=inbound.payload,
            vendor=inbound.vendor,
            idempotency_key=inbound.idempotency_key,
        )
        return Response(
            {"status": "accepted", "webhook_id": str(result.webhook.id)},
            status=status.HTTP_202_ACCEPTED,
        )


class HealthView(APIView):
    authentication_classes: list[Any] = []
    permission_classes: list[Any] = []

    @extend_schema(
        operation_id="health_check",
        summary="Health check",
        responses={200: OpenApiResponse(response=HealthSerializer)},
    )
    def get(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        return Response({"status": "ok"}, status=status.HTTP_200_OK)


class WebhookReplayView(APIView):
    authentication_classes: list[Any] = []
    permission_classes: list[Any] = []

    @extend_schema(
        operation_id="replay_webhook",
        summary="Replay a webhook",
        description="Requeues a previously ingested webhook for normalization.",
        request=ReplayWebhookSerializer,
        responses={
            202: OpenApiResponse(response=ReplayWebhookResponseSerializer),
            404: OpenApiResponse(response=ErrorSerializer),
            409: OpenApiResponse(response=ReplayWebhookResponseSerializer),
        },
    )
    def post(self, request: Request, webhook_id: str, *args: Any, **kwargs: Any) -> Response:
        webhook = get_object_or_404(RawWebhook, id=webhook_id)
        serializer = ReplayWebhookSerializer(data=request.data or {})
        serializer.is_valid(raise_exception=True)
        replayed = ReplayService.replay(webhook, force=serializer.validated_data["force"])

        status_code = status.HTTP_202_ACCEPTED if replayed else status.HTTP_409_CONFLICT
        return Response(
            {
                "status": "accepted" if replayed else "skipped",
                "webhook_id": str(webhook.id),
                "processing_status": webhook.processing_status,
            },
            status=status_code,
        )
