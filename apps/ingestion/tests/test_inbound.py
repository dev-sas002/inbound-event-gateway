"""
Reading a delivery out of a request.

These rules used to live in the view and could only be exercised through a
request factory. They are facts about webhooks, so they are tested as facts.
"""

from __future__ import annotations

import pytest
from django.http.request import RawPostDataException
from django.test import RequestFactory

from apps.ingestion.inbound import (
    IDEMPOTENCY_HEADER,
    UNKNOWN_VENDOR,
    VENDOR_HEADER,
    InvalidPayload,
    read_request,
    resolve_idempotency_key,
    resolve_vendor,
)


def headers(**values) -> dict[str, str]:
    return values


class TestVendorResolution:
    def test_the_header_wins(self):
        vendor = resolve_vendor({VENDOR_HEADER: "FromHeader"}, {"vendor": "FromBody"})
        assert vendor == "FromHeader"

    def test_the_payload_is_the_fallback(self):
        assert resolve_vendor({}, {"provider": "Globex"}) == "Globex"

    def test_payload_keys_are_tried_in_order(self):
        assert resolve_vendor({}, {"carrier": "Last", "vendor": "First"}) == "First"

    def test_an_unnamed_sender_is_unknown_rather_than_missing(self):
        assert resolve_vendor({}, {"shipment_id": "S1"}) == UNKNOWN_VENDOR

    def test_whitespace_is_not_a_vendor_name(self):
        assert resolve_vendor({VENDOR_HEADER: "   "}, {}) == UNKNOWN_VENDOR

    def test_an_over_long_name_is_cut_to_the_column(self):
        assert len(resolve_vendor({VENDOR_HEADER: "v" * 500}, {})) == 128


class TestIdempotencyKeyResolution:
    def test_the_header_is_taken_as_sent(self):
        assert resolve_idempotency_key({IDEMPOTENCY_HEADER: " abc "}) == "abc"

    def test_no_header_means_no_key(self):
        assert resolve_idempotency_key({}) is None

    def test_an_empty_header_is_not_a_key(self):
        # "" would collapse every unkeyed delivery from a vendor onto one row.
        assert resolve_idempotency_key({IDEMPOTENCY_HEADER: "  "}) is None

    def test_an_over_long_key_is_cut_to_the_column(self):
        assert len(resolve_idempotency_key({IDEMPOTENCY_HEADER: "k" * 900})) == 255


class TestReadingTheRequest:
    def _request(self, body: bytes = b"{}", **extra):
        return RequestFactory().post(
            "/api/webhooks/", data=body, content_type="application/json", **extra
        )

    def test_the_raw_body_is_kept_for_the_signature(self):
        body = b'{"shipment_id": "S1",  "status":"delivered"}'
        inbound = read_request(
            self._request(body), {"shipment_id": "S1", "status": "delivered"}, body, "X-Sig"
        )
        # Byte for byte, spacing included: re-serialising the parsed payload
        # would not reproduce what was signed.
        assert inbound.raw_body == body

    def test_it_never_reads_the_body_off_the_request(self):
        """
        Regression: reading `request.body` after DRF has parsed `request.data`
        raises RawPostDataException on a real WSGI server, which turned every
        webhook into a 500. The bytes are an argument precisely so that this
        function cannot reach for a stream somebody else has already drained.
        """
        request = self._request()

        class Drained:
            """A request whose stream has already been consumed."""

            headers = request.headers

            @property
            def body(self):
                raise RawPostDataException("stream already read")

        inbound = read_request(Drained(), {"shipment_id": "S1"}, b"{}", "X-Sig")
        assert inbound.raw_body == b"{}"

    @pytest.mark.parametrize("payload", [None, {}, [], "text", 7])
    def test_anything_that_is_not_an_object_is_refused(self, payload):
        with pytest.raises(InvalidPayload):
            read_request(self._request(), payload, b"", "X-Sig")
