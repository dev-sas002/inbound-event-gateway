"""
Vendor profiles: the seam, and what it buys.

A profile only matters if it changes the reading — the same payload that scores
low without one has to clear the gate with one, and a profile that has been
edited has to take effect. Both are tested here, because "the registry exists"
is not a feature.
"""

from __future__ import annotations

import json

import pytest
from django.urls import reverse

from apps.normalization.backends.rules_backend import PROFILE_CONFIDENCE, RuleBasedNormalizer
from apps.normalization.models import EntityType, ProfileSource, VendorProfile
from apps.normalization.repositories import VendorProfileRepository
from apps.normalization.vendors import (
    _CODE_PROFILES,
    VendorProfileSpec,
    profile_for,
    register_profile,
)

pytestmark = pytest.mark.django_db

#: A vendor that names nothing the way the generic key list expects.
AWKWARD_PAYLOAD = {
    "vendor": "Novacarrier",
    "consignment": {"ref": "NC-77120"},
    "lifecycle": {"phase": "handed_to_courier"},
    "stamp": "2026-04-21T22:47:00+00:00",
}

NOVACARRIER = VendorProfileSpec(
    vendor="Novacarrier",
    entity_type=EntityType.SHIPMENT.value,
    id_paths=("consignment.ref",),
    status_paths=("lifecycle.phase",),
    time_paths=("stamp",),
    status_map={"handed_to_courier": "OUT_FOR_DELIVERY"},
)


@pytest.fixture
def normalizer():
    return RuleBasedNormalizer()


@pytest.fixture
def code_profiles_are_clean():
    """Code-registered profiles are process-global; do not leak one."""
    before = dict(_CODE_PROFILES)
    yield
    _CODE_PROFILES.clear()
    _CODE_PROFILES.update(before)


class TestWithoutAProfile:
    def test_an_unknown_vocabulary_scores_low_and_is_held(self, normalizer):
        response = normalizer.normalize(AWKWARD_PAYLOAD, vendor="Novacarrier")
        # "handed_to_courier" is not in the generic table, so the normaliser
        # abstains rather than picking a status nobody verified.
        assert response.normalized.confidence_score < 0.7
        assert response.normalized.normalized_payload["vendor_profile"] is None


class TestWithAProfile:
    def test_a_profile_turns_guesses_into_lookups(self, normalizer):
        VendorProfileRepository.save(NOVACARRIER)

        response = normalizer.normalize(AWKWARD_PAYLOAD, vendor="Novacarrier")

        assert response.normalized.entity_id == "NC-77120"
        assert response.normalized.canonical_status == "OUT_FOR_DELIVERY"
        assert response.normalized.confidence_score == pytest.approx(PROFILE_CONFIDENCE)
        assert response.normalized.normalized_payload["vendor_profile"] == "Novacarrier"

    def test_the_vendor_name_is_matched_case_insensitively(self, normalizer):
        VendorProfileRepository.save(NOVACARRIER)
        response = normalizer.normalize(AWKWARD_PAYLOAD, vendor="NOVACARRIER")
        assert response.normalized.canonical_status == "OUT_FOR_DELIVERY"

    def test_a_profile_only_applies_to_its_own_vendor(self, normalizer):
        VendorProfileRepository.save(NOVACARRIER)
        response = normalizer.normalize(AWKWARD_PAYLOAD, vendor="SomeoneElse")
        assert response.normalized.confidence_score < 0.7

    def test_an_unmapped_word_still_falls_through_to_the_gate(self, normalizer):
        # A profile that says nothing about this word must not invent one.
        VendorProfileRepository.save(NOVACARRIER)
        payload = {**AWKWARD_PAYLOAD, "lifecycle": {"phase": "held_at_border"}}
        response = normalizer.normalize(payload, vendor="Novacarrier")
        assert response.normalized.confidence_score < 0.7

    def test_a_null_mapping_is_not_applied(self, normalizer):
        # Discovery records a word it could not interpret as null. Treating
        # that as a status would write None into a non-null column.
        VendorProfileRepository.save(
            VendorProfileSpec(
                vendor="Novacarrier",
                entity_type=EntityType.SHIPMENT.value,
                id_paths=("consignment.ref",),
                status_paths=("lifecycle.phase",),
                status_map={},
            )
        )
        VendorProfile.objects.filter(vendor="Novacarrier").update(
            status_map={"handed_to_courier": None}
        )
        from apps.normalization.vendors import invalidate

        invalidate("Novacarrier")
        response = normalizer.normalize(AWKWARD_PAYLOAD, vendor="Novacarrier")
        assert response.normalized.confidence_score < 0.7


class TestResolution:
    def test_an_edit_takes_effect_rather_than_serving_a_stale_cache(self):
        VendorProfileRepository.save(NOVACARRIER)
        assert profile_for("Novacarrier").status_map == {"handed_to_courier": "OUT_FOR_DELIVERY"}

        VendorProfileRepository.save(
            VendorProfileSpec(
                vendor="Novacarrier",
                entity_type=EntityType.SHIPMENT.value,
                status_map={"handed_to_courier": "DELIVERED"},
            )
        )
        assert profile_for("Novacarrier").status_map == {"handed_to_courier": "DELIVERED"}

    def test_a_deleted_profile_stops_being_used(self):
        VendorProfileRepository.save(NOVACARRIER)
        assert profile_for("Novacarrier") is not None
        VendorProfileRepository.delete("Novacarrier")
        assert profile_for("Novacarrier") is None

    def test_a_code_profile_wins_over_a_stored_one(self, code_profiles_are_clean):
        VendorProfileRepository.save(NOVACARRIER)
        register_profile(
            VendorProfileSpec(
                vendor="Novacarrier",
                entity_type=EntityType.INVOICE.value,
                status_map={"handed_to_courier": "PAID"},
            )
        )
        # A profile the team has deliberately pinned in the repository must
        # not be silently replaced by a discovery run.
        assert profile_for("Novacarrier").entity_type == EntityType.INVOICE.value

    def test_an_empty_vendor_has_no_profile(self):
        assert profile_for("") is None


class TestTheProfileApi:
    def test_a_profile_can_be_written_read_and_removed(self, client):
        url = reverse("vendor-profile-detail", args=["Novacarrier"])
        body = {
            "entity_type": "SHIPMENT",
            "id_paths": ["consignment.ref"],
            "status_paths": ["lifecycle.phase"],
            "status_map": {"handed_to_courier": "OUT_FOR_DELIVERY"},
        }
        written = client.put(url, data=json.dumps(body), content_type="application/json")
        assert written.status_code == 200

        read = client.get(url)
        assert read.status_code == 200
        assert read.json()["status_map"] == {"handed_to_courier": "OUT_FOR_DELIVERY"}
        assert read.json()["source"] == "MANUAL"

        assert client.delete(url).status_code == 204
        assert client.get(url).status_code == 404

    def test_a_status_outside_the_entity_vocabulary_is_refused(self, client):
        url = reverse("vendor-profile-detail", args=["Novacarrier"])
        body = {"entity_type": "SHIPMENT", "status_map": {"handed_to_courier": "PAID"}}
        response = client.put(url, data=json.dumps(body), content_type="application/json")
        # PAID is an invoice status. Accepting it here would mean every one of
        # this vendor's webhooks failed validation at normalisation time.
        assert response.status_code == 400
        assert "status_map" in response.json()

    def test_the_listing_shows_stored_profiles(self, client):
        VendorProfileRepository.save(NOVACARRIER)
        body = client.get(reverse("vendor-profiles")).json()
        assert body["count"] == 1
        assert body["results"][0]["vendor"] == "Novacarrier"
        assert body["results"][0]["source"] == ProfileSource.MANUAL.value

    def test_deleting_a_profile_that_is_not_there_is_a_404(self, client):
        assert client.delete(reverse("vendor-profile-detail", args=["nobody"])).status_code == 404
