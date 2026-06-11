"""
Vendor schema discovery.

The interesting assertions are the ones about restraint: discovery must not
invent a mapping it cannot justify, must not accept a path the model made up,
and must not stop working when the model is unavailable. No test here makes a
network call — the OpenAI client is replaced wholesale.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from django.urls import reverse

from apps.normalization.discovery import (
    MODEL_ASSISTED,
    STATISTICAL,
    DiscoveryError,
    discover_profile,
    discover_statistically,
)
from apps.normalization.models import EntityType, ProfileSource, VendorProfile

pytestmark = pytest.mark.django_db


def samples() -> list[dict]:
    """Three payloads from one imaginary vendor, shaped unlike the others."""
    return [
        {
            "consignment": {"ref": "NC-1001"},
            "lifecycle": {"phase": "collected"},
            "stamp": "2026-04-21T22:47:00+00:00",
            "carrier": "Novacarrier",
        },
        {
            "consignment": {"ref": "NC-1002"},
            "lifecycle": {"phase": "in_transit"},
            "stamp": "2026-04-22T09:05:00+00:00",
            "carrier": "Novacarrier",
        },
        {
            "consignment": {"ref": "NC-1003"},
            "lifecycle": {"phase": "handed_to_courier"},
            "stamp": "2026-04-23T11:30:00+00:00",
            "carrier": "Novacarrier",
        },
    ]


def model_returning(payload: dict):
    """An OpenAI client stand-in that answers with one JSON body."""
    client = MagicMock()
    completion = MagicMock()
    completion.choices = [MagicMock()]
    completion.choices[0].message.content = json.dumps(payload)
    client.chat.completions.create.return_value = completion
    return client


class TestStatisticalDiscovery:
    def test_it_finds_the_identifier_by_how_it_behaves(self):
        result = discover_statistically("Novacarrier", samples())
        # The ref is different in every sample; the status is not.
        assert "consignment.ref" in result.profile.id_paths
        assert "consignment.ref" not in result.profile.status_paths

    def test_it_finds_the_status_by_how_it_repeats(self):
        result = discover_statistically("Novacarrier", samples() * 3)
        assert "lifecycle.phase" in result.profile.status_paths

    def test_it_finds_the_timestamp_by_parsing_it(self):
        result = discover_statistically("Novacarrier", samples())
        assert "stamp" in result.profile.time_paths

    def test_it_maps_the_words_it_recognises(self):
        result = discover_statistically("Novacarrier", samples())
        assert result.profile.entity_type == EntityType.SHIPMENT.value
        assert result.profile.status_map["collected"] == "PICKED_UP"
        assert result.profile.status_map["in_transit"] == "IN_TRANSIT"

    def test_it_says_which_words_it_could_not_map(self):
        result = discover_statistically("Novacarrier", samples())
        # The honest output: this word was seen, and nobody has decided what
        # it means. Guessing would put a wrong status into entity state.
        assert "handed_to_courier" in result.unmapped_statuses
        assert "handed_to_courier" not in result.profile.status_map

    def test_it_warns_when_it_cannot_find_an_identifier(self):
        result = discover_statistically("Flat", [{"status": "paid"}, {"status": "paid"}])
        assert result.profile.id_paths == ()
        assert any("identifier" in warning for warning in result.warnings)

    def test_it_needs_at_least_one_sample(self):
        with pytest.raises(DiscoveryError):
            discover_profile("Nobody", [])

    def test_it_runs_with_no_api_key(self, settings):
        settings.OPENAI_API_KEY = ""
        result = discover_profile("Novacarrier", samples())
        assert result.method == STATISTICAL


class TestModelAssistedDiscovery:
    @pytest.fixture
    def with_key(self, settings):
        settings.OPENAI_API_KEY = "sk-test-not-a-real-key"
        return settings

    def test_the_model_can_add_a_mapping_the_rules_missed(self, with_key):
        client = model_returning(
            {
                "entity_type": "SHIPMENT",
                "id_paths": ["consignment.ref"],
                "status_paths": ["lifecycle.phase"],
                "time_paths": ["stamp"],
                "status_map": {"handed_to_courier": "OUT_FOR_DELIVERY"},
                "notes": "phase names are courier-centric",
            }
        )
        with patch("openai.OpenAI", return_value=client):
            result = discover_profile("Novacarrier", samples())

        assert result.method == MODEL_ASSISTED
        assert result.profile.status_map["handed_to_courier"] == "OUT_FOR_DELIVERY"
        # And the statistical mappings survive alongside it.
        assert result.profile.status_map["in_transit"] == "IN_TRANSIT"
        assert result.unmapped_statuses == ()
        assert result.profile.notes == "phase names are courier-centric"

    def test_a_path_that_is_not_in_the_samples_is_dropped(self, with_key):
        client = model_returning(
            {
                "entity_type": "SHIPMENT",
                "id_paths": ["shipment.tracking_number"],
                "status_paths": [],
                "time_paths": [],
                "status_map": {},
            }
        )
        with patch("openai.OpenAI", return_value=client):
            result = discover_profile("Novacarrier", samples())

        # The model's invention is not written into a profile that then reads
        # live traffic; what the samples actually contain survives.
        assert "shipment.tracking_number" not in result.profile.id_paths
        assert "consignment.ref" in result.profile.id_paths
        assert any("not in the samples" in warning for warning in result.warnings)

    def test_a_status_outside_the_vocabulary_is_dropped(self, with_key):
        client = model_returning(
            {
                "entity_type": "SHIPMENT",
                "id_paths": [],
                "status_paths": [],
                "time_paths": [],
                # PAID is an invoice status; a shipment cannot take it.
                "status_map": {"handed_to_courier": "PAID"},
            }
        )
        with patch("openai.OpenAI", return_value=client):
            result = discover_profile("Novacarrier", samples())

        assert "handed_to_courier" not in result.profile.status_map
        assert any("not canonical" in warning for warning in result.warnings)

    def test_a_null_mapping_is_left_unmapped(self, with_key):
        client = model_returning(
            {
                "entity_type": "SHIPMENT",
                "id_paths": [],
                "status_paths": [],
                "time_paths": [],
                "status_map": {"handed_to_courier": None},
            }
        )
        with patch("openai.OpenAI", return_value=client):
            result = discover_profile("Novacarrier", samples())

        assert "handed_to_courier" in result.unmapped_statuses

    def test_a_model_failure_falls_back_to_the_statistical_reading(self, with_key):
        client = MagicMock()
        client.chat.completions.create.side_effect = RuntimeError("rate limited")
        with patch("openai.OpenAI", return_value=client):
            result = discover_profile("Novacarrier", samples())

        # Discovery is an assist, not a dependency.
        assert result.method == STATISTICAL
        assert result.profile.status_map["in_transit"] == "IN_TRANSIT"
        assert any("model-assisted discovery failed" in w for w in result.warnings)

    def test_the_model_can_be_switched_off_explicitly(self, with_key):
        with patch("openai.OpenAI") as ctor:
            result = discover_profile("Novacarrier", samples(), use_model=False)
        ctor.assert_not_called()
        assert result.method == STATISTICAL


class TestTheDiscoveryEndpoint:
    def _post(self, client, body):
        return client.post(
            reverse("vendor-profile-discover"),
            data=json.dumps(body),
            content_type="application/json",
        )

    def test_it_proposes_without_saving_by_default(self, client):
        response = self._post(client, {"vendor": "Novacarrier", "samples": samples()})
        assert response.status_code == 200
        assert response.json()["persisted"] is False
        # A proposal read out of three payloads is a hypothesis, not a change.
        assert not VendorProfile.objects.exists()

    def test_it_saves_when_asked(self, client):
        response = self._post(
            client, {"vendor": "Novacarrier", "samples": samples(), "persist": True}
        )
        assert response.status_code == 200
        assert response.json()["persisted"] is True
        profile = VendorProfile.objects.get(vendor="Novacarrier")
        assert profile.source == ProfileSource.DISCOVERED.value
        assert profile.sample_count == 3

    def test_it_reports_the_words_a_person_still_has_to_decide(self, client):
        body = self._post(client, {"vendor": "Novacarrier", "samples": samples()}).json()
        assert "handed_to_courier" in body["unmapped_statuses"]

    def test_samples_are_required(self, client):
        assert self._post(client, {"vendor": "Novacarrier", "samples": []}).status_code == 400
