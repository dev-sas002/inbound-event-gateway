"""
Which queue a webhook goes on, and why it is not always the same one.

The bottleneck in this service is not the database and it is not the HTTP
front door — ingestion is one insert and a dispatch, and it does not care how
many vendors are shouting. It is the normalisation worker pool, because
normalising is the slow step (a model call, in the configured deployment) and
every vendor shares it.

One queue plus one pool means one vendor's burst is everybody's latency: fifty
thousand messages from a chatty carrier sit in front of the invoice webhook
that arrived a second later, and that invoice waits for the whole burst to
drain. Nothing is lost, but the service is effectively down for every other
vendor while it happens.

So bursts are bulkheaded. A vendor's recent arrival rate is counted in a fixed
window, and once it crosses `NOISY_VENDOR_BURST` its work is routed to a
separate queue served by its own worker. The noisy vendor keeps being processed
— slower, on dedicated capacity — and the default lane stays short for
everybody else. It is the smallest change that makes tail latency a property of
one vendor rather than of the whole service.

The counter lives in the cache, not the database: it is approximate by design,
resets with the window, and must never be a write on the ingestion path. When
the cache is unreachable the routing decision degrades to "default queue",
which is exactly the behaviour this service had before.
"""

from __future__ import annotations

import hashlib
import logging
import time

from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger("apps.ingestion")

_PREFIX = "vendor-rate:v1:"


def _digest(vendor: str) -> str:
    return hashlib.sha1((vendor or "unknown").strip().lower().encode("utf-8")).hexdigest()[:20]


def _window_key(vendor: str, window_seconds: int, now: float) -> str:
    """
    A fixed window, keyed by its own start time.

    Fixed rather than sliding on purpose: a sliding window needs a sorted set
    and a round trip per event, and the decision here only has to be roughly
    right. The worst case is a burst straddling a boundary being counted as
    two smaller ones, which delays bulkheading by at most one window.
    """
    bucket = int(now // window_seconds)
    # Hashed, not interpolated: vendor names carry spaces and punctuation, and
    # a cache key containing either is rejected outright by memcached and
    # warned about by Django's own key validator.
    return f"{_PREFIX}{_digest(vendor)}:{window_seconds}:{bucket}"


def record_arrival(vendor: str, *, now: float | None = None) -> int:
    """Count one webhook from this vendor and return the count in this window."""
    window = int(getattr(settings, "NOISY_VENDOR_WINDOW_SECONDS", 60)) or 60
    key = _window_key(vendor, window, now if now is not None else time.time())
    try:
        # add() then incr() rather than get/set: two callers racing on a new
        # window would otherwise both read zero and both write one.
        cache.add(key, 0, window * 2)
        return int(cache.incr(key))
    except Exception:
        # A rate counter is never worth failing an ingest over.
        logger.warning("vendor_rate_counter_unavailable", extra={"vendor": vendor})
        return 0


def queue_for_vendor(vendor: str, arrivals: int) -> str:
    """The queue name this webhook should be dispatched to."""
    default_queue = getattr(settings, "NORMALIZATION_QUEUE", "normalization")
    burst = int(getattr(settings, "NOISY_VENDOR_BURST", 0) or 0)
    if burst <= 0 or arrivals <= burst:
        return default_queue

    bulk_queue = getattr(settings, "NORMALIZATION_BULK_QUEUE", "normalization.bulk")
    if arrivals == burst + 1:
        # Logged once per window rather than once per message: the point is to
        # tell an operator a vendor has started shouting, not to join in.
        logger.warning(
            "vendor_burst_bulkheaded",
            extra={"vendor": vendor, "arrivals": arrivals, "queue": bulk_queue},
        )
    return bulk_queue


def route_for(vendor: str) -> str:
    """Record one arrival and return the queue it should go to."""
    return queue_for_vendor(vendor, record_arrival(vendor))
