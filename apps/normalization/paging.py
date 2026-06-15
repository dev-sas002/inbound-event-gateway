"""
Limit/offset paging for the listing endpoints.

The listings used to take a bare `[:100]` slice and report `count` as the
length of that slice, which meant a queue of four thousand held events
truthfully answered "100" and gave the caller no way to reach the rest. A
reviewer working the queue would have believed they were done.

`count` here is the real total from the database, the page size is capped so a
caller cannot ask for the whole table, and the response says where the next
page starts.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from django.conf import settings
from django.db.models import QuerySet


class PageError(ValueError):
    """The caller asked for a page that does not make sense."""


@dataclass(frozen=True)
class Page:
    items: list[Any]
    total: int
    limit: int
    offset: int

    @property
    def next_offset(self) -> int | None:
        nxt = self.offset + self.limit
        return nxt if nxt < self.total else None

    @property
    def previous_offset(self) -> int | None:
        return max(self.offset - self.limit, 0) if self.offset else None

    def envelope(self, results: Any) -> dict[str, Any]:
        return {
            "count": self.total,
            "limit": self.limit,
            "offset": self.offset,
            "next_offset": self.next_offset,
            "previous_offset": self.previous_offset,
            "results": results,
        }


def _int_param(params, name: str, default: int, *, minimum: int, maximum: int | None) -> int:
    raw = params.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise PageError(f"{name} must be an integer.") from None
    if value < minimum or (maximum is not None and value > maximum):
        bound = f" and at most {maximum}" if maximum is not None else ""
        raise PageError(f"{name} must be at least {minimum}{bound}.")
    return value


def paginate(queryset: QuerySet, params) -> Page:
    """
    Slice one page out of a queryset, with a true total.

    Two queries — a COUNT and the page — rather than one that drags the whole
    table into memory to measure it.
    """
    limit = _int_param(
        params,
        "limit",
        settings.DEFAULT_PAGE_SIZE,
        minimum=1,
        maximum=settings.MAX_PAGE_SIZE,
    )
    offset = _int_param(params, "offset", 0, minimum=0, maximum=None)
    total = queryset.count()
    return Page(
        items=list(queryset[offset : offset + limit]),
        total=total,
        limit=limit,
        offset=offset,
    )
