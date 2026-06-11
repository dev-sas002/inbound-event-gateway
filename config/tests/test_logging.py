"""
The JSON log formatter.

These exist because the failure they guard against is invisible: a call site
adds a field to `extra=`, the formatter drops it, and nothing anywhere reports
a problem. The only symptom is an operator reading a log line that is missing
the number they needed.
"""

from __future__ import annotations

import json
import logging
import sys

from config.logging import JsonFormatter


def _render(**extra) -> dict:
    record = logging.LogRecord(
        name="apps.normalization",
        level=logging.WARNING,
        pathname=__file__,
        lineno=1,
        msg="normalization_held_for_review",
        args=None,
        exc_info=None,
    )
    for key, value in extra.items():
        setattr(record, key, value)
    return json.loads(JsonFormatter().format(record))


class TestTheEnvelope:
    def test_every_line_carries_the_standard_fields(self):
        rendered = _render()
        assert rendered["level"] == "WARNING"
        assert rendered["logger"] == "apps.normalization"
        assert rendered["message"] == "normalization_held_for_review"
        assert "timestamp" in rendered
        # Present even when unset, so a consumer can rely on the shape.
        assert "correlation_id" in rendered

    def test_the_line_is_one_json_object(self):
        assert (
            JsonFormatter()
            .format(logging.LogRecord("x", logging.INFO, __file__, 1, "hello", None, None))
            .count("\n")
            == 0
        )


class TestExtraFields:
    def test_a_field_a_call_site_passes_is_emitted(self):
        # The regression: this formatter used an allow-list, so `threshold`
        # and `event_id` were dropped from the one line that explains a hold.
        rendered = _render(event_id=17, confidence=0.3, threshold=0.7)
        assert rendered["event_id"] == 17
        assert rendered["confidence"] == 0.3
        assert rendered["threshold"] == 0.7

    def test_a_field_nobody_thought_of_is_emitted_too(self):
        assert _render(a_field_added_next_year="yes")["a_field_added_next_year"] == "yes"

    def test_record_bookkeeping_does_not_leak_into_the_event(self):
        rendered = _render(vendor="acme")
        for noise in ("msg", "args", "levelno", "pathname", "lineno", "exc_info"):
            assert noise not in rendered

    def test_a_value_json_cannot_encode_does_not_lose_the_line(self):
        # A log line is diagnostics; failing to write one must never raise
        # inside the code being diagnosed.
        rendered = _render(payload=object())
        assert rendered["payload"].startswith("<object object")


class TestExceptions:
    def test_a_traceback_is_carried_as_a_field(self):
        try:
            raise ValueError("boom")
        except ValueError:
            record = logging.LogRecord(
                "apps.normalization",
                logging.ERROR,
                __file__,
                1,
                "webhook_worker_unexpected_failure",
                None,
                exc_info=sys.exc_info(),
            )
            rendered = json.loads(JsonFormatter().format(record))
        assert "ValueError: boom" in rendered["exception"]
