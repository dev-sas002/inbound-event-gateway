"""
The model-backed normaliser.

Kept deliberately thin: prompt in, strict JSON out, every failure sorted into
"retry this" or "do not retry this" before it leaves the module. Everything
downstream — the confidence gate, entity state, the review queue — is identical
whether the reading came from here or from the rule-based backend.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from django.conf import settings
from openai import APIError, APITimeoutError, OpenAI, RateLimitError

from apps.normalization.backends.base import NormalizationResponse
from apps.normalization.exceptions import NormalizationError, RetryableNormalizationError
from apps.normalization.prompts import PROMPT_VERSION, SYSTEM_PROMPT, USER_PROMPT_TEMPLATE
from apps.normalization.schemas import NormalizationResult
from apps.normalization.vendors import profile_for

logger = logging.getLogger("apps.normalization")


class OpenAINormalizationService:
    """Encapsulates OpenAI prompt construction and strict-response parsing."""

    def __init__(self) -> None:
        if not settings.OPENAI_API_KEY:
            raise NormalizationError("OPENAI_API_KEY is not configured")
        self.client = OpenAI(
            api_key=settings.OPENAI_API_KEY, timeout=settings.OPENAI_TIMEOUT_SECONDS
        )

    def normalize(self, payload: Any, *, vendor: str = "") -> NormalizationResponse:
        payload_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
        prompt = USER_PROMPT_TEMPLATE.format(
            payload_json=payload_json,
            vendor_hint=self._vendor_hint(vendor),
        )

        try:
            completion = self.client.chat.completions.create(
                model=settings.OPENAI_MODEL,
                response_format={"type": "json_object"},
                temperature=0,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
            )
        except (RateLimitError, APITimeoutError, APIError) as exc:
            raise RetryableNormalizationError(str(exc)) from exc
        except Exception as exc:
            raise NormalizationError(f"OpenAI invocation failed: {exc}") from exc

        try:
            content = completion.choices[0].message.content or "{}"
            parsed = json.loads(content)
            normalized = NormalizationResult.from_dict(parsed)
        except Exception as exc:
            raise NormalizationError(f"Malformed model response: {exc}") from exc

        return NormalizationResponse(
            normalized=normalized,
            llm_model=completion.model,
            prompt_version=settings.NORMALIZATION_PROMPT_VERSION or PROMPT_VERSION,
        )

    @staticmethod
    def _vendor_hint(vendor: str) -> str:
        """
        Fold what is already known about the vendor into the prompt.

        A discovered profile is the cheapest possible grounding: it tells the
        model where this sender puts its fields and what its status words mean,
        which is exactly the context a single payload does not carry.
        """
        profile = profile_for(vendor)
        if profile is None:
            return f"The sending vendor is {vendor or 'unknown'}. No profile is on file."

        known = {token: canonical for token, canonical in profile.status_map.items() if canonical}
        lines = [f"The sending vendor is {profile.vendor}. A profile is on file:"]
        if profile.entity_type:
            lines.append(f"- this vendor always sends {profile.entity_type} events")
        if profile.id_paths:
            lines.append(f"- identifier lives at: {', '.join(profile.id_paths)}")
        if profile.status_paths:
            lines.append(f"- status lives at: {', '.join(profile.status_paths)}")
        if profile.time_paths:
            lines.append(f"- event time lives at: {', '.join(profile.time_paths)}")
        if known:
            mapped = ", ".join(f"{token} -> {canonical}" for token, canonical in known.items())
            lines.append(f"- known status vocabulary: {mapped}")
        lines.append("Prefer the profile where it applies; say so in confidence if it does not.")
        return "\n".join(lines)
