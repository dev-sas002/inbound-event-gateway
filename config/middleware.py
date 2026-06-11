from __future__ import annotations

import uuid
from collections.abc import Callable

from django.http import HttpRequest, HttpResponse

from config.observability import set_correlation_id


class CorrelationIdMiddleware:
    """Attach and propagate request correlation IDs."""

    header_name = "HTTP_X_CORRELATION_ID"
    response_header = "X-Correlation-ID"

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        correlation_id = request.META.get(self.header_name, str(uuid.uuid4()))
        token = set_correlation_id(correlation_id)
        request.correlation_id = correlation_id
        try:
            response = self.get_response(request)
            response[self.response_header] = correlation_id
            return response
        finally:
            set_correlation_id(None, token)
