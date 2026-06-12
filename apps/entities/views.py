from __future__ import annotations

from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.entities.serializers import EntityStateSerializer
from apps.entities.services import EntityProjectionService


class EntityStateView(APIView):
    """Read-only endpoint for latest normalized state by entity identity."""

    authentication_classes = []
    permission_classes = []

    @extend_schema(
        operation_id="entity_state",
        summary="Get latest projected entity state",
        parameters=[
            OpenApiParameter(
                name="entity_type",
                type=str,
                location=OpenApiParameter.QUERY,
                required=True,
                description="Entity type such as SHIPMENT or INVOICE.",
            ),
            OpenApiParameter(
                name="entity_id",
                type=str,
                location=OpenApiParameter.QUERY,
                required=True,
                description="Canonical entity identifier.",
            ),
        ],
        responses={200: EntityStateSerializer},
    )
    def get(self, request, *args, **kwargs):
        entity_type = request.query_params.get("entity_type")
        entity_id = request.query_params.get("entity_id")
        if not entity_type or not entity_id:
            return Response(
                {"detail": "entity_type and entity_id are required query params."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        state = EntityProjectionService.get_current_state(
            entity_type=entity_type, entity_id=entity_id
        )
        if state is None:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)

        return Response(EntityStateSerializer(state).data, status=status.HTTP_200_OK)
