"""
What every normalisation backend must look like.

The pipeline does not care whether a payload was read by a model or by a
lookup table; it cares that it gets back one canonical event and an honest
confidence score. Pinning that contract here is what lets the backend be
swapped by configuration and lets the whole service run with no API key.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from apps.normalization.schemas import NormalizationResult


@dataclass(frozen=True)
class NormalizationResponse:
    """One backend's reading of one payload, plus who did the reading."""

    normalized: NormalizationResult
    llm_model: str
    prompt_version: str


@runtime_checkable
class Normalizer(Protocol):
    """The only method the ingestion task calls."""

    def normalize(self, payload: Any, *, vendor: str = "") -> NormalizationResponse:
        """
        Read one vendor payload into a canonical event.

        `vendor` is passed because it is the single most useful piece of
        context available: it selects the learned vendor profile, and a
        backend that ignores it is still correct, only less certain.
        """
        ...  # pragma: no cover - protocol declaration
