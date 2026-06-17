"""
The model boundary.

Nothing here talks to OpenAI: the client class is replaced wholesale, so the
tests describe what the service does with an answer rather than what a model
would say. The two things worth pinning are the classification of failures —
a rate limit must be retried, a malformed answer must not be — and the refusal
to trust a response that does not match the schema, because a model will
occasionally return confident nonsense in valid JSON.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from openai import APIError, APITimeoutError, RateLimitError

from apps.normalization.backends import get_normalizer
from apps.normalization.backends.openai_backend import OpenAINormalizationService
from apps.normalization.backends.rules_backend import RuleBasedNormalizer
from apps.normalization.exceptions import (
    NormalizationError,
    RetryableNormalizationError,
)

VALID_RESPONSE = {
    "entity_type": "SHIPMENT",
    "entity_id": "SHIP-1",
    "canonical_status": "DELIVERED",
    "event_time": "2026-03-02T14:00:00Z",
    "confidence_score": 0.91,
    "normalized_payload": {"summary": "delivered to consignee"},
}

PAYLOAD = {"vendor": "acme", "event": "delivered", "shipment_id": "SHIP-1"}


def completion(content: str, model: str = "gpt-4.1-mini-2026-01-01"):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
        model=model,
    )


@pytest.fixture
def openai_client(settings):
    """A stand-in for the OpenAI client, wired into the service."""
    settings.OPENAI_API_KEY = "sk-test-not-a-real-key"
    settings.OPENAI_MODEL = "gpt-4.1-mini"
    client = MagicMock()
    with patch("apps.normalization.backends.openai_backend.OpenAI", return_value=client) as ctor:
        client.constructor = ctor
        yield client


def _request():
    """A minimal stand-in for the httpx request the SDK errors carry."""
    return SimpleNamespace(method="POST", url="https://api.openai.com/v1/chat/completions")


def _response(status_code: int):
    return SimpleNamespace(status_code=status_code, headers={}, request=_request())


class TestConfiguration:
    def test_the_service_refuses_to_start_without_a_key(self, settings):
        settings.OPENAI_API_KEY = ""

        with pytest.raises(NormalizationError, match="OPENAI_API_KEY"):
            OpenAINormalizationService()

    def test_the_configured_model_and_timeout_are_used(self, openai_client, settings):
        settings.OPENAI_TIMEOUT_SECONDS = 7
        openai_client.chat.completions.create.return_value = completion(json.dumps(VALID_RESPONSE))

        OpenAINormalizationService().normalize(PAYLOAD)

        openai_client.constructor.assert_called_once_with(
            api_key="sk-test-not-a-real-key", timeout=7
        )
        kwargs = openai_client.chat.completions.create.call_args.kwargs
        assert kwargs["model"] == "gpt-4.1-mini"
        # JSON mode and temperature 0: the response is parsed strictly, so
        # anything that makes the model chattier breaks the caller.
        assert kwargs["response_format"] == {"type": "json_object"}
        assert kwargs["temperature"] == 0

    def test_the_payload_is_sent_to_the_model_verbatim(self, openai_client):
        openai_client.chat.completions.create.return_value = completion(json.dumps(VALID_RESPONSE))

        OpenAINormalizationService().normalize(PAYLOAD)

        prompt = openai_client.chat.completions.create.call_args.kwargs["messages"][1]
        assert "SHIP-1" in prompt["content"]


class TestSuccessfulNormalisation:
    def test_a_valid_response_becomes_a_normalisation(self, openai_client):
        openai_client.chat.completions.create.return_value = completion(json.dumps(VALID_RESPONSE))

        response = OpenAINormalizationService().normalize(PAYLOAD)

        assert response.normalized.entity_id == "SHIP-1"
        assert response.normalized.canonical_status == "DELIVERED"
        assert response.normalized.confidence_score == 0.91
        # The model actually used, not the alias asked for: a deployment needs
        # to know which snapshot produced a reading it later disputes.
        assert response.llm_model == "gpt-4.1-mini-2026-01-01"

    def test_the_prompt_version_is_recorded(self, openai_client, settings):
        settings.NORMALIZATION_PROMPT_VERSION = "v9"
        openai_client.chat.completions.create.return_value = completion(json.dumps(VALID_RESPONSE))

        response = OpenAINormalizationService().normalize(PAYLOAD)

        assert response.prompt_version == "v9"


class TestTransientFailures:
    """These must be retried: the payload is fine, the API was not."""

    @pytest.mark.parametrize(
        "error",
        [
            RateLimitError("rate limited", response=_response(429), body=None),
            APITimeoutError(_request()),
            APIError("upstream error", _request(), body=None),
        ],
        ids=["rate_limit", "timeout", "api_error"],
    )
    def test_api_failures_are_retryable(self, openai_client, error):
        openai_client.chat.completions.create.side_effect = error

        with pytest.raises(RetryableNormalizationError):
            OpenAINormalizationService().normalize(PAYLOAD)


class TestPermanentFailures:
    """These must not be retried: the same call would fail the same way."""

    def test_an_unexpected_client_error_is_permanent(self, openai_client):
        openai_client.chat.completions.create.side_effect = ValueError("bad argument")

        with pytest.raises(NormalizationError, match="OpenAI invocation failed"):
            OpenAINormalizationService().normalize(PAYLOAD)

    def test_a_non_json_answer_is_refused(self, openai_client):
        openai_client.chat.completions.create.return_value = completion(
            "Sure! Here is the shipment status:"
        )

        with pytest.raises(NormalizationError, match="Malformed model response"):
            OpenAINormalizationService().normalize(PAYLOAD)

    def test_an_empty_answer_is_refused(self, openai_client):
        openai_client.chat.completions.create.return_value = completion(None)

        with pytest.raises(NormalizationError):
            OpenAINormalizationService().normalize(PAYLOAD)

    def test_a_status_outside_the_canonical_vocabulary_is_refused(self, openai_client):
        # Valid JSON, confident, and wrong. Accepting it would put a status
        # nothing downstream understands into entity state.
        invented = VALID_RESPONSE | {"canonical_status": "LOST_AT_SEA"}
        openai_client.chat.completions.create.return_value = completion(json.dumps(invented))

        with pytest.raises(NormalizationError):
            OpenAINormalizationService().normalize(PAYLOAD)

    def test_a_confidence_outside_the_range_is_refused(self, openai_client):
        openai_client.chat.completions.create.return_value = completion(
            json.dumps(VALID_RESPONSE | {"confidence_score": 1.4})
        )

        with pytest.raises(NormalizationError):
            OpenAINormalizationService().normalize(PAYLOAD)


class TestBackendSelection:
    def test_no_key_selects_the_rule_engine(self, settings):
        settings.OPENAI_API_KEY = ""
        settings.NORMALIZATION_BACKEND = "auto"

        assert isinstance(get_normalizer(), RuleBasedNormalizer)

    def test_a_key_selects_the_model(self, openai_client):
        # openai_client sets a key and replaces the client class.
        assert isinstance(get_normalizer(), OpenAINormalizationService)

    def test_rules_can_be_forced_even_with_a_key(self, openai_client, settings):
        settings.NORMALIZATION_BACKEND = "rules"

        assert isinstance(get_normalizer(), RuleBasedNormalizer)

    def test_forcing_openai_without_a_key_fails_loudly(self, settings):
        # Silently falling back to rules would make a deployment that thinks
        # it is running the model quietly run something else.
        settings.OPENAI_API_KEY = ""
        settings.NORMALIZATION_BACKEND = "openai"

        with pytest.raises(NormalizationError):
            get_normalizer()

    def test_an_unknown_backend_name_falls_back_to_rules(self, settings):
        settings.NORMALIZATION_BACKEND = "nonsense"

        assert isinstance(get_normalizer(), RuleBasedNormalizer)
