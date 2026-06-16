"""
Structured logging.

Every log line in this service is an event with fields, not a sentence: the
message is a stable name (`webhook_processed`, `normalization_held_for_review`)
and everything that varies goes in `extra`. That is what makes a log searchable
by vendor or by webhook id rather than by regex.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

from config.observability import get_correlation_id

#: Attributes every LogRecord carries whether or not anyone asked for them.
#: Everything on a record that is *not* in here was put there by a call site's
#: `extra=`, and is therefore worth emitting.
#:
#: This replaces an allow-list of known field names. The allow-list had to be
#: edited every time a call site added a field, and forgetting was silent — the
#: field simply never appeared in the logs. `normalization_held_for_review`
#: logged the threshold it compared against and the event id it held; neither
#: survived the formatter, so the one line an operator would read to understand
#: a hold was missing both numbers that explained it.
_RECORD_ATTRS = frozenset(
    set(vars(logging.LogRecord("", 0, "", 0, "", None, None))) | {"message", "asctime", "taskName"}
)


class JsonFormatter(logging.Formatter):
    """Render a record as one JSON object per line."""

    def format(self, record: logging.LogRecord) -> str:
        log_data: dict[str, object] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "correlation_id": get_correlation_id(),
        }

        for key, value in record.__dict__.items():
            # Leading underscores are private bookkeeping, not event fields.
            if key not in _RECORD_ATTRS and not key.startswith("_"):
                log_data[key] = value

        if record.exc_info:
            log_data["exception"] = self.formatException(record.exc_info)

        return json.dumps(log_data, default=str)
