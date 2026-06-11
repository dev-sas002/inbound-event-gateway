"""
The seed command runs on every container start, so it has to be safe to run on
every container start.
"""

from __future__ import annotations

from io import StringIO
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command

from apps.ingestion.models import RawWebhook

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def enqueue():
    with patch("apps.ingestion.services.process_raw_webhook.apply_async") as mock:
        yield mock


def seed(**options) -> str:
    out = StringIO()
    call_command("seed_demo", stdout=out, **options)
    return out.getvalue()


class TestSeeding:
    def test_it_ingests_the_sample_vendors(self):
        seed(per_vendor=2)
        vendors = set(RawWebhook.objects.values_list("vendor", flat=True))
        assert len(vendors) >= 4
        assert RawWebhook.objects.count() == 8

    def test_running_it_twice_does_not_double_the_data(self):
        seed(per_vendor=2)
        before = RawWebhook.objects.count()
        output = seed(per_vendor=2)
        # Stable idempotency keys, so a restart is a no-op rather than a
        # second copy of the demo.
        assert RawWebhook.objects.count() == before
        assert "skipped 8" in output

    def test_each_variant_is_a_distinct_event(self):
        seed(per_vendor=3)
        ids = RawWebhook.objects.values_list("idempotency_key", flat=True)
        assert len(set(ids)) == len(ids)


class TestTheDemoAdmin:
    """
    The review console is the screen worth showing, so a one-command boot has
    to be able to reach it — without leaving a default superuser behind in
    anything that is not a demo.
    """

    def test_no_password_creates_no_account(self):
        seed(per_vendor=1)
        assert not get_user_model().objects.exists()

    def test_a_password_creates_a_superuser_that_can_log_in(self, client):
        seed(per_vendor=1, admin_password="s3cret-demo")
        user = get_user_model().objects.get(username="demo")
        assert user.is_staff and user.is_superuser
        # The password is set, not stored raw.
        assert user.password != "s3cret-demo"
        assert client.login(username="demo", password="s3cret-demo")

    def test_the_username_is_configurable(self):
        seed(per_vendor=1, admin_username="ops", admin_password="s3cret-demo")
        assert get_user_model().objects.filter(username="ops").exists()

    def test_an_existing_account_is_not_overwritten(self):
        user_model = get_user_model()
        user_model.objects.create_user(username="demo", password="chosen-by-an-operator")
        output = seed(per_vendor=1, admin_password="the-container-default")
        user = user_model.objects.get(username="demo")
        # A restart must not silently undo a password somebody changed.
        assert user.check_password("chosen-by-an-operator")
        assert "left unchanged" in output

    def test_seeding_twice_does_not_duplicate_the_account(self):
        seed(per_vendor=1, admin_password="s3cret-demo")
        seed(per_vendor=1, admin_password="s3cret-demo")
        assert get_user_model().objects.filter(username="demo").count() == 1
