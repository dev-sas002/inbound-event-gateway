"""
Fixtures shared by the whole suite.

Two things every test needs: a stored webhook to hang a normalisation off, and
a normaliser that answers without a network call. Both live here so no test
file has to reinvent them, and so nothing in the suite can reach OpenAI by
accident.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from django.core.cache import cache

from apps.ingestion.models import ProcessingStatus, RawWebhook
from apps.normalization.models import EntityType, NormalizedEvent
from apps.normalization.schemas import NormalizationResult


@pytest.fixture(autouse=True)
def no_openai_key(settings):
    """
    No test may select the OpenAI backend by accident.

    The factory chooses by the presence of a key, so a key in the developer's
    environment would silently route tests at a billable API. Tests that want
    the OpenAI path set the key themselves and patch the client.
    """
    settings.OPENAI_API_KEY = ""
    settings.NORMALIZATION_BACKEND = "auto"


@pytest.fixture(autouse=True)
def clean_cache():
    """
    The cache holds vendor profiles and arrival counters, and LocMemCache
    outlives a test. Leaking either between tests would make results depend on
    ordering, which is the worst kind of flake to chase.
    """
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def make_webhook():
    def _make(
        *,
        vendor: str = "test-vendor",
        payload: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        external_event_id: str | None = None,
        processing_status: str = ProcessingStatus.RECEIVED,
    ) -> RawWebhook:
        return RawWebhook.objects.create(
            id=uuid.uuid4(),
            vendor=vendor,
            external_event_id=external_event_id,
            # (vendor, idempotency_key) is unique — the constraint that *is*
            # the idempotency guarantee — so fixtures respect it rather than
            # working around it.
            idempotency_key=idempotency_key or f"fixture-{uuid.uuid4().hex}",
            raw_payload=payload if payload is not None else {"shipment_id": "SHIP-1"},
            processing_status=processing_status,
        )

    return _make


@pytest.fixture
def make_event(make_webhook):
    def _make(
        *,
        confidence: float = 0.9,
        entity_id: str = "SHIP-1",
        entity_type: str = EntityType.SHIPMENT,
        canonical_status: str = "DELIVERED",
        event_time: datetime | None = None,
        webhook: RawWebhook | None = None,
    ) -> NormalizedEvent:
        return NormalizedEvent.objects.create(
            webhook=webhook or make_webhook(processing_status=ProcessingStatus.NORMALIZED),
            entity_type=entity_type,
            entity_id=entity_id,
            canonical_status=canonical_status,
            event_time=event_time or datetime(2026, 3, 2, 14, 0, tzinfo=UTC),
            normalized_payload={},
            confidence_score=confidence,
            llm_model="rules-based-normalizer",
            prompt_version="rules-v1",
        )

    return _make


class StubNormalizerResponse:
    """The shape both real normalisers return."""

    def __init__(self, normalized: NormalizationResult, llm_model: str, prompt_version: str):
        self.normalized = normalized
        self.llm_model = llm_model
        self.prompt_version = prompt_version


class StubNormalizer:
    """
    A normaliser that answers from memory.

    It records the payloads and vendors it was handed, which is how the task
    tests assert that a duplicate or an already-processed webhook never
    reached a normaliser at all.
    """

    def __init__(
        self,
        *,
        result: NormalizationResult | None = None,
        error: Exception | None = None,
    ):
        self.result = result
        self.error = error
        self.calls: list[Any] = []
        #: The vendor each call was made for. The task is supposed to pass it,
        #: because that is what selects the vendor profile.
        self.vendors: list[str] = []

    def normalize(self, payload: Any, *, vendor: str = "") -> StubNormalizerResponse:
        self.calls.append(payload)
        self.vendors.append(vendor)
        if self.error is not None:
            raise self.error
        return self.respond()

    def respond(self) -> StubNormalizerResponse:
        assert self.result is not None, "StubNormalizer needs a result or an error"
        return StubNormalizerResponse(
            normalized=self.result,
            llm_model="stub-normalizer",
            prompt_version="stub-v1",
        )


@pytest.fixture
def normalization_result():
    def _make(
        *,
        entity_id: str = "SHIP-1",
        entity_type: str = EntityType.SHIPMENT,
        canonical_status: str = "DELIVERED",
        confidence: float = 0.92,
        event_time: datetime | None = None,
    ) -> NormalizationResult:
        return NormalizationResult(
            entity_type=str(entity_type),
            entity_id=entity_id,
            canonical_status=canonical_status,
            event_time=event_time or datetime(2026, 3, 2, 14, 0, tzinfo=UTC),
            confidence_score=confidence,
            normalized_payload={"normalizer": "stub"},
        )

    return _make
