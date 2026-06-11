"""
Vendor schema discovery: learn a payload shape from a handful of samples.

Onboarding a vendor used to mean reading their docs and hoping the normaliser's
generic key list happened to cover them — and when it did not, every one of
their webhooks scored low and piled up in the review queue. Discovery turns
that into a five-minute job: hand it a few real payloads and it proposes a
profile saying where the identifier, status and timestamp live and what the
vendor's status words mean.

Two readers, same output shape:

* **Statistical.** Always runs. Flattens the samples, and picks the paths that
  are present everywhere and behave the way an identifier, a status and a
  timestamp behave — an identifier varies across samples, a status repeats from
  a small vocabulary, a timestamp parses as one.
* **Model-assisted.** Runs as well when an OpenAI key is configured. The model
  reads the samples and proposes the same structure, using the field *names*
  and any prose in the payload, which frequency analysis cannot.

The model never gets the last word. Every path it proposes is checked against
the flattened samples and dropped if it does not exist, and every status it
maps is checked against the canonical vocabulary. What survives is merged on
top of the statistical result, so the model can add to the reading but cannot
replace a fact with a plausible invention. With no key set, the statistical
result is the answer and nothing about the endpoint changes.

Nothing here writes a profile on its own — `persist` is a separate, explicit
step, because a discovered profile is a proposal for a person to look at.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from django.conf import settings

from apps.normalization.backends.rules_backend import (
    ID_KEYS,
    INVOICE_HINTS,
    INVOICE_ID_KEYS,
    INVOICE_STATUS,
    SHIPMENT_HINTS,
    SHIPMENT_ID_KEYS,
    SHIPMENT_STATUS,
    STATUS_KEYS,
    TIME_KEYS,
    VENDOR_NAMING_KEYS,
    flatten_payload,
    parse_timestamp,
    status_token,
)
from apps.normalization.models import EntityType, ProfileSource
from apps.normalization.vendors import VendorProfileSpec

logger = logging.getLogger("apps.normalization")

STATISTICAL = "statistical"
MODEL_ASSISTED = "model-assisted"

#: How many samples the model is shown. Enough to see the shape, few enough to
#: keep the request small and cheap.
MAX_LLM_SAMPLES = 8

#: Longer than this and the field is prose (a description, a body), not a
#: status word this service could map onto a canonical value.
MAX_STATUS_LENGTH = 64

DISCOVERY_PROMPT_VERSION = "discovery-v1"

DISCOVERY_SYSTEM_PROMPT = """
You infer the schema of a vendor's webhook payloads.
You return only valid JSON and no additional text.
""".strip()

DISCOVERY_USER_PROMPT = """
Here are {count} sample webhook payloads from the vendor "{vendor}", already
flattened to dotted paths. Infer where each canonical field lives and what the
vendor's status words mean.

Rules:
1) Every path you return MUST appear verbatim in the samples below.
2) entity_type is one of SHIPMENT, INVOICE, UNCLASSIFIED, or "" if unclear.
3) SHIPMENT statuses map to: PICKED_UP, IN_TRANSIT, OUT_FOR_DELIVERY, DELIVERED.
4) INVOICE statuses map to: ISSUED, PAID, VOIDED, REFUNDED.
5) Map a vendor status word to null when you are not sure. Guessing here is
   worse than leaving it out: an unmapped word is held for review, a wrongly
   mapped one becomes a fact.
6) Order paths most reliable first.

Return JSON:
{{
  "entity_type": "SHIPMENT|INVOICE|UNCLASSIFIED|",
  "id_paths": ["string"],
  "status_paths": ["string"],
  "time_paths": ["string"],
  "status_map": {{"vendor_word": "CANONICAL_STATUS_OR_NULL"}},
  "notes": "one sentence on anything odd about this vendor"
}}

Samples:
{samples_json}
""".strip()


@dataclass(frozen=True)
class DiscoveryResult:
    """A proposed profile, and an honest account of how it was arrived at."""

    profile: VendorProfileSpec
    method: str
    sample_count: int
    #: Status words seen in the samples that could not be mapped to the
    #: canonical vocabulary. These are the ones a person has to decide.
    unmapped_statuses: tuple[str, ...] = ()
    warnings: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, Any]:
        return {
            "method": self.method,
            "sample_count": self.sample_count,
            "unmapped_statuses": list(self.unmapped_statuses),
            "warnings": list(self.warnings),
            "profile": self.profile.as_dict(),
        }


class DiscoveryError(Exception):
    """Discovery cannot run on what it was given."""


# -- statistical reading ---------------------------------------------------


def _path_values(samples: list[dict[str, Any]]) -> dict[str, list[Any]]:
    """Every flattened path, and the values it held across the samples."""
    values: dict[str, list[Any]] = {}
    for flat in samples:
        for path, value in flat.items():
            if value in (None, ""):
                continue
            values.setdefault(path, []).append(value)
    return values


def _name_score(path: str, keys: tuple[str, ...]) -> int:
    """How strongly a path's own name says what it holds."""
    leaf = path.split(".")[-1]
    if leaf in keys:
        # Earlier keys in the list are the more specific ones.
        return len(keys) - keys.index(leaf)
    lowered = leaf.lower()
    return 1 if any(key.lower() in lowered for key in keys) else 0


def _rank(
    values: dict[str, list[Any]],
    sample_count: int,
    keys: tuple[str, ...],
    predicate,
) -> list[str]:
    """Paths that satisfy `predicate`, best first."""
    scored: list[tuple[int, int, str]] = []
    for path, seen in values.items():
        if not predicate(path, seen):
            continue
        # Present in every sample beats present in some: a field that only
        # sometimes appears cannot be relied on to carry the identifier.
        coverage = len(seen)
        scored.append((_name_score(path, keys), coverage, path))
    scored.sort(key=lambda item: (-item[0], -item[1], item[2]))
    return [path for score, coverage, path in scored if score > 0 or coverage == sample_count]


def _looks_like_identifier(path: str, seen: list[Any]) -> bool:
    if not all(isinstance(value, (str, int)) for value in seen):
        return False
    if all(parse_timestamp(value) is not None for value in seen):
        # A timestamp is unique per event too, but it is not what state is
        # keyed on.
        return False
    distinct = len({str(value) for value in seen})
    # An identifier is different every time; a status is not.
    return distinct == len(seen) and distinct > 0


def _looks_like_status(path: str, seen: list[Any]) -> bool:
    """
    A status is a *word*, and that is the signal that actually separates it.

    Cardinality alone does not: with three samples an identifier and a status
    are both "three distinct values". But identifiers and timestamps carry
    digits and statuses do not, and a field whose value never changes across
    samples is a constant — the vendor's own name, a fixed source — rather
    than a vocabulary.
    """
    if not all(isinstance(value, str) for value in seen):
        return False
    if any(len(value) > MAX_STATUS_LENGTH for value in seen):
        return False
    if any(character.isdigit() for value in seen for character in value):
        return False

    distinct = len({value.strip().lower() for value in seen})
    if distinct == 1 or len(seen) == 1:
        # A constant, or a single sample with nothing to compare against. Only
        # the field's own name can vouch for it.
        return _name_score(path, STATUS_KEYS) > 0
    return True


def _looks_like_time(path: str, seen: list[Any]) -> bool:
    return all(parse_timestamp(value) is not None for value in seen)


def _classify_entity_type(samples: list[dict[str, Any]], id_path: str | None) -> str:
    """
    The same reading the rule-based normaliser will do, so a discovered
    profile cannot disagree with the backend that is going to use it.
    """
    leaf = (id_path or "").split(".")[-1].lower()
    if leaf in SHIPMENT_ID_KEYS:
        return EntityType.SHIPMENT.value
    if leaf in INVOICE_ID_KEYS:
        return EntityType.INVOICE.value

    blob = " ".join(
        f"{key} {value}"
        for flat in samples
        for key, value in flat.items()
        if key.split(".")[-1].lower() not in VENDOR_NAMING_KEYS
    ).lower()
    shipment = sum(1 for hint in SHIPMENT_HINTS if hint in blob)
    invoice = sum(1 for hint in INVOICE_HINTS if hint in blob)
    if shipment > invoice and shipment > 0:
        return EntityType.SHIPMENT.value
    if invoice > shipment and invoice > 0:
        return EntityType.INVOICE.value
    return ""


def _map_statuses(
    entity_type: str, tokens: list[str]
) -> tuple[dict[str, str | None], tuple[str, ...]]:
    """Map the vendor's words onto canonical statuses; leave the rest for a person."""
    if entity_type == EntityType.SHIPMENT.value:
        table = SHIPMENT_STATUS
    elif entity_type == EntityType.INVOICE.value:
        table = INVOICE_STATUS
    else:
        table = {}

    mapping: dict[str, str | None] = {}
    unmapped: list[str] = []
    for token in tokens:
        if token in table:
            mapping[token] = table[token]
            continue
        parts = token.split("_")
        matched = next(
            (table[s] for s in ("_".join(parts[i:]) for i in range(1, len(parts))) if s in table),
            None,
        )
        # Recorded as null rather than dropped, so the profile carries the
        # fact that this word was seen and nobody has decided what it means.
        mapping[token] = matched
        if matched is None:
            unmapped.append(token)
    return mapping, tuple(sorted(set(unmapped)))


def discover_statistically(vendor: str, payloads: list[Any]) -> DiscoveryResult:
    """Infer a profile from the samples alone. No network, no key, no cost."""
    samples = [flatten_payload(payload) for payload in payloads]
    values = _path_values(samples)
    count = len(samples)

    id_paths = _rank(values, count, ID_KEYS, _looks_like_identifier)
    status_paths = _rank(values, count, STATUS_KEYS, _looks_like_status)
    time_paths = _rank(values, count, TIME_KEYS, _looks_like_time)
    entity_type = _classify_entity_type(samples, id_paths[0] if id_paths else None)

    tokens: list[str] = []
    if status_paths:
        tokens = [status_token(value) for value in values.get(status_paths[0], [])]
    status_map, unmapped = _map_statuses(entity_type, sorted(set(tokens)))

    warnings: list[str] = []
    if not id_paths:
        warnings.append(
            "no field varied across every sample, so no identifier could be "
            "picked out; send more samples or set id_paths by hand"
        )
    if not status_paths:
        warnings.append("no field looked like a status vocabulary")
    if not entity_type:
        warnings.append("samples did not clearly say whether these are shipments or invoices")

    return DiscoveryResult(
        profile=VendorProfileSpec(
            vendor=vendor,
            entity_type=entity_type,
            id_paths=tuple(id_paths[:4]),
            status_paths=tuple(status_paths[:4]),
            time_paths=tuple(time_paths[:4]),
            status_map={token: value for token, value in status_map.items() if value},
            source=ProfileSource.DISCOVERED.value,
            sample_count=count,
        ),
        method=STATISTICAL,
        sample_count=count,
        unmapped_statuses=unmapped,
        warnings=tuple(warnings),
    )


# -- model-assisted reading ------------------------------------------------


def _canonical_values(entity_type: str) -> set[str]:
    if entity_type == EntityType.SHIPMENT.value:
        return set(SHIPMENT_STATUS.values())
    if entity_type == EntityType.INVOICE.value:
        return set(INVOICE_STATUS.values())
    return set()


def _ask_model(vendor: str, samples: list[dict[str, Any]], client) -> dict[str, Any]:
    shown = samples[:MAX_LLM_SAMPLES]
    prompt = DISCOVERY_USER_PROMPT.format(
        count=len(shown),
        vendor=vendor,
        samples_json=json.dumps(shown, ensure_ascii=False, indent=2, default=str),
    )
    completion = client.chat.completions.create(
        model=settings.OPENAI_MODEL,
        response_format={"type": "json_object"},
        temperature=0,
        messages=[
            {"role": "system", "content": DISCOVERY_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
    )
    return json.loads(completion.choices[0].message.content or "{}")


def _merge_model_proposal(
    base: DiscoveryResult, proposal: dict[str, Any], samples: list[dict[str, Any]]
) -> DiscoveryResult:
    """
    Take what the model got right and discard the rest.

    Every path is checked against the real samples and every status against the
    canonical vocabulary, so a confident invention is dropped rather than
    written into a profile that then normalises live traffic.
    """
    known_paths = {path for flat in samples for path in flat}
    warnings = list(base.warnings)

    def keep_paths(raw: Any, existing: tuple[str, ...]) -> tuple[str, ...]:
        proposed = [str(path) for path in raw or [] if isinstance(path, (str, int))]
        valid = [path for path in proposed if path in known_paths]
        dropped = len(proposed) - len(valid)
        if dropped:
            warnings.append(f"dropped {dropped} proposed path(s) that are not in the samples")
        # Model order first (it read the field names), then anything the
        # statistical pass found that the model missed.
        return (*valid, *(path for path in existing if path not in valid))

    entity_type = str(proposal.get("entity_type") or base.profile.entity_type or "").upper()
    if entity_type not in {*EntityType.values, ""}:
        warnings.append(f"ignored unrecognised entity_type {entity_type!r} from the model")
        entity_type = base.profile.entity_type

    allowed = _canonical_values(entity_type)
    status_map = dict(base.profile.status_map)
    unmapped = set(base.unmapped_statuses)
    for token, canonical in (proposal.get("status_map") or {}).items():
        if canonical is None:
            continue
        token, canonical = status_token(token), str(canonical).upper()
        if canonical not in allowed:
            warnings.append(f"ignored status mapping {token!r} -> {canonical!r}: not canonical")
            continue
        status_map[token] = canonical
        unmapped.discard(token)

    notes = str(proposal.get("notes") or "").strip()[:500]

    return DiscoveryResult(
        profile=VendorProfileSpec(
            vendor=base.profile.vendor,
            entity_type=entity_type,
            id_paths=keep_paths(proposal.get("id_paths"), base.profile.id_paths)[:4],
            status_paths=keep_paths(proposal.get("status_paths"), base.profile.status_paths)[:4],
            time_paths=keep_paths(proposal.get("time_paths"), base.profile.time_paths)[:4],
            status_map=status_map,
            source=ProfileSource.DISCOVERED.value,
            sample_count=base.sample_count,
            notes=notes,
        ),
        method=MODEL_ASSISTED,
        sample_count=base.sample_count,
        unmapped_statuses=tuple(sorted(unmapped)),
        warnings=tuple(warnings),
    )


def discover_profile(
    vendor: str, payloads: list[Any], *, use_model: bool | None = None
) -> DiscoveryResult:
    """
    Propose a profile for one vendor.

    `use_model` defaults to "whenever a key is configured". The statistical
    reading always runs first and is the fallback for every model failure, so
    this function has no path that raises because OpenAI was unavailable.
    """
    payloads = [payload for payload in payloads if isinstance(payload, dict) and payload]
    if not payloads:
        raise DiscoveryError("At least one non-empty JSON object sample is required.")

    base = discover_statistically(vendor, payloads)
    if use_model is None:
        use_model = bool(settings.OPENAI_API_KEY)
    if not use_model:
        return base

    samples = [flatten_payload(payload) for payload in payloads]
    try:
        from openai import OpenAI

        client = OpenAI(api_key=settings.OPENAI_API_KEY, timeout=settings.OPENAI_TIMEOUT_SECONDS)
        proposal = _ask_model(vendor, samples, client)
    except Exception as exc:
        # Discovery is an assist, not a dependency. A model that is down, rate
        # limited or talking nonsense must not stop a vendor being onboarded.
        logger.warning(
            "vendor_discovery_model_failed",
            extra={"vendor": vendor, "error": str(exc)},
        )
        return DiscoveryResult(
            profile=base.profile,
            method=STATISTICAL,
            sample_count=base.sample_count,
            unmapped_statuses=base.unmapped_statuses,
            warnings=(*base.warnings, f"model-assisted discovery failed: {exc}"),
        )

    return _merge_model_proposal(base, proposal, samples)


def sample_status_counts(payloads: list[Any], status_path: str) -> Counter[str]:
    """How often each status word appears; useful when reviewing a proposal."""
    return Counter(
        status_token(flat[status_path])
        for flat in (flatten_payload(payload) for payload in payloads)
        if flat.get(status_path) not in (None, "")
    )
