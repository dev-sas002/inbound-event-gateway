"""
The backend registry: the seam a new normalisation strategy plugs into.

Adding a backend is a two-line change here plus one module — no edit to the
Celery task, the confidence gate, or entity state, because all three depend on
the `Normalizer` protocol rather than on any concrete reader.

Resolution order mirrors the rest of this project's configuration: an explicit
`NORMALIZATION_BACKEND` wins, otherwise the presence of an OpenAI key decides,
otherwise the rule-based normaliser runs. Defaulting to rules rather than
failing is what makes the service startable, testable and demonstrable without
a billable key.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from django.conf import settings

from apps.normalization.backends.base import NormalizationResponse, Normalizer

logger = logging.getLogger("apps.normalization")

#: Backend name -> a zero-argument factory. Factories are lazy so importing
#: this module never imports a vendor SDK that may not be installed.
_REGISTRY: dict[str, Callable[[], Normalizer]] = {}

AUTO = "auto"
OPENAI = "openai"
RULES = "rules"


def register_backend(name: str, factory: Callable[[], Normalizer]) -> None:
    """Make a backend selectable by `NORMALIZATION_BACKEND=<name>`."""
    _REGISTRY[name.lower()] = factory


def available_backends() -> tuple[str, ...]:
    return tuple(sorted(_REGISTRY))


def _build_openai() -> Normalizer:
    from apps.normalization.backends.openai_backend import OpenAINormalizationService

    return OpenAINormalizationService()


def _build_rules() -> Normalizer:
    from apps.normalization.backends.rules_backend import RuleBasedNormalizer

    return RuleBasedNormalizer()


register_backend(OPENAI, _build_openai)
register_backend(RULES, _build_rules)


def resolve_backend_name() -> str:
    """Which backend the current configuration selects."""
    configured = (getattr(settings, "NORMALIZATION_BACKEND", "") or AUTO).lower()
    if configured != AUTO:
        return configured if configured in _REGISTRY else RULES
    return OPENAI if settings.OPENAI_API_KEY else RULES


def get_normalizer() -> Normalizer:
    configured = (getattr(settings, "NORMALIZATION_BACKEND", "") or AUTO).lower()
    name = resolve_backend_name()

    if configured == AUTO and name == RULES:
        logger.warning(
            "normalizer_selected",
            extra={
                "backend": RULES,
                "reason": "OPENAI_API_KEY is not set",
                "note": "unrecognised vendor vocabulary will score low confidence",
            },
        )
    else:
        logger.info("normalizer_selected", extra={"backend": name})

    return _REGISTRY[name]()


__all__ = [
    "AUTO",
    "OPENAI",
    "RULES",
    "NormalizationResponse",
    "Normalizer",
    "available_backends",
    "get_normalizer",
    "register_backend",
    "resolve_backend_name",
]
