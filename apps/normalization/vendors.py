"""
Vendor profiles: what the normaliser knows about one sender's payload shape.

This is the extension seam. Supporting a new vendor well means telling the
normaliser three things — where the identifier lives, where the status lives,
and what that vendor's status words mean — and none of those are facts about
*code*. So they are data:

* profiles registered in code (`register_profile`) for vendors worth pinning
  down in the repository, and
* profiles stored in the database, which is where `apps.normalization.discovery`
  writes the ones it proposes from sample payloads.

The rule-based normaliser consults `profile_for()` and falls back to its
heuristics when there is nothing. Nothing else in the pipeline changes, which
is the point: onboarding a vendor never touches the task, the gate or entity
state.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field

from django.core.cache import cache

logger = logging.getLogger("apps.normalization")

#: Profiles are read on every normalisation and change rarely. A short TTL
#: keeps a worker from querying on each message without making an edit wait.
PROFILE_CACHE_SECONDS = 60
_CACHE_PREFIX = "vendor-profile:v1:"

_MISS = "__miss__"


@dataclass(frozen=True)
class VendorProfileSpec:
    """
    A profile as the normaliser consumes it: plain data, no database.

    Keeping this separate from the model means a backend can be unit-tested
    with a hand-built profile, and a profile can be proposed and inspected
    before anything is persisted.
    """

    vendor: str
    entity_type: str = ""
    id_paths: tuple[str, ...] = ()
    status_paths: tuple[str, ...] = ()
    time_paths: tuple[str, ...] = ()
    status_map: dict[str, str] = field(default_factory=dict)
    source: str = "MANUAL"
    sample_count: int = 0
    notes: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "vendor": self.vendor,
            "entity_type": self.entity_type,
            "id_paths": list(self.id_paths),
            "status_paths": list(self.status_paths),
            "time_paths": list(self.time_paths),
            "status_map": dict(self.status_map),
            "source": self.source,
            "sample_count": self.sample_count,
            "notes": self.notes,
        }


#: Profiles pinned in code. Checked before the database so a vendor the team
#: has deliberately described cannot be silently overridden by a discovery run.
_CODE_PROFILES: dict[str, VendorProfileSpec] = {}


def _key(vendor: str) -> str:
    return (vendor or "").strip().lower()


def _cache_key(vendor_key: str) -> str:
    # Vendor names contain spaces and punctuation; a cache key may not.
    digest = hashlib.sha1(vendor_key.encode("utf-8")).hexdigest()[:20]
    return _CACHE_PREFIX + digest


def register_profile(spec: VendorProfileSpec) -> None:
    """Pin a profile in code. Wins over anything stored in the database."""
    _CODE_PROFILES[_key(spec.vendor)] = spec


def registered_profiles() -> tuple[VendorProfileSpec, ...]:
    return tuple(_CODE_PROFILES.values())


def spec_from_model(profile) -> VendorProfileSpec:
    """Convert a stored VendorProfile row into the spec the backends use."""
    return VendorProfileSpec(
        vendor=profile.vendor,
        entity_type=profile.entity_type or "",
        id_paths=tuple(profile.id_paths or ()),
        status_paths=tuple(profile.status_paths or ()),
        time_paths=tuple(profile.time_paths or ()),
        # Discovery records tokens it could not interpret as null. Dropping
        # them here means an uninterpreted token falls through to the
        # heuristics and scores low, rather than mapping onto None and
        # blowing up in the status lookup.
        status_map={
            str(token): str(canonical)
            for token, canonical in (profile.status_map or {}).items()
            if canonical
        },
        source=profile.source,
        sample_count=profile.sample_count,
        notes=profile.notes,
    )


def profile_for(vendor: str) -> VendorProfileSpec | None:
    """The profile for one vendor, or None. Cached; see `invalidate`."""
    key = _key(vendor)
    if not key:
        return None
    if key in _CODE_PROFILES:
        return _CODE_PROFILES[key]

    cached = cache.get(_cache_key(key))
    if cached is not None:
        return None if cached == _MISS else cached

    from apps.normalization.models import VendorProfile

    row = VendorProfile.objects.filter(vendor__iexact=key).first()
    spec = spec_from_model(row) if row is not None else None
    # A miss is cached too: unprofiled vendors are the common case, and
    # re-querying for every one of their webhooks is the whole cost this
    # cache exists to avoid.
    cache.set(_cache_key(key), spec if spec is not None else _MISS, PROFILE_CACHE_SECONDS)
    return spec


def invalidate(vendor: str) -> None:
    """Drop the cached profile for one vendor after it has been written."""
    cache.delete(_cache_key(_key(vendor)))
