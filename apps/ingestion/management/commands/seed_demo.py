"""
Put enough real-shaped traffic through the pipeline that a fresh boot is worth
looking at.

An empty deployment of this service tells you nothing: the review queue is the
interesting screen and it only exists once something has been held. So the seed
sends payloads from four vendors with deliberately different shapes — including
one whose vocabulary the normaliser does not recognise, which is what puts an
event in front of a human.

Safe to run repeatedly: every payload carries a stable idempotency key, so a
second run is a no-op rather than a second copy of the same events.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

from apps.ingestion.models import ProcessingStatus, RawWebhook
from apps.ingestion.services import IngestionService

SAMPLES_DIR = Path(settings.BASE_DIR) / "samples"

#: Status words cycled through the templates, so the seeded data exercises
#: several canonical statuses instead of repeating one.
# Each list is mostly vocabulary the normaliser knows, plus one phrase it does
# not. That mix is the point: a fresh deployment shows both halves of the
# pipeline — events confident enough to become entity state, and events held
# in front of a person.
_STATUS_CYCLE = {
    "maersk_shipment.json": (
        "event_description",
        ["collected", "departed", "arrived", "Loaded onboard and sailed"],
    ),
    "one_shipment.json": (
        "status_text",
        ["in_transit", "out_for_delivery", "delivered", "Cargo released to consignee"],
    ),
    "globalfreightpay_invoice.json": (
        "message",
        ["issued", "paid", "refunded", "settled in full"],
    ),
    # Left alone on purpose: this vendor's payloads carry no status the
    # normaliser recognises, which is exactly the case the gate exists for.
    "marine_advisory_unclassified.json": ("severity", ["low", "medium", "high"]),
}

_ID_KEYS = ("event_id", "externalEventId", "id")
_TIME_KEYS = ("event_time", "occurred_at", "timestamp", "published_at")


class Command(BaseCommand):
    help = "Ingest sample vendor webhooks so a fresh deployment has something to show."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--per-vendor",
            type=int,
            default=6,
            help="How many events to send for each sample vendor.",
        )
        parser.add_argument(
            "--admin-username",
            default="demo",
            help="Username for the demo admin account created by --admin-password.",
        )
        parser.add_argument(
            "--admin-password",
            default="",
            help=(
                "Create a superuser with this password so the review console is "
                "reachable on a fresh boot. Omitted or empty, no account is created."
            ),
        )

    def handle(self, *args: Any, **options: Any) -> None:
        self._create_admin(options["admin_username"], options["admin_password"])

        per_vendor = max(1, options["per_vendor"])
        templates = sorted(SAMPLES_DIR.glob("*.json"))
        if not templates:
            self.stdout.write(self.style.WARNING(f"No samples in {SAMPLES_DIR}"))
            return

        created = 0
        duplicates = 0
        for template in templates:
            payload_template = json.loads(template.read_text(encoding="utf-8"))
            for index in range(per_vendor):
                payload = self._variant(template.name, payload_template, index)
                result = IngestionService.ingest(
                    payload=payload,
                    vendor=str(payload.get("vendor", "unknown")),
                    # Stable across runs: seeding twice must not double the data.
                    idempotency_key=f"seed:{template.stem}:{index}",
                )
                created += int(result.created)
                duplicates += int(not result.created)

        total = RawWebhook.objects.count()
        pending = RawWebhook.objects.filter(
            processing_status__in=[ProcessingStatus.RECEIVED, ProcessingStatus.PROCESSING]
        ).count()
        self.stdout.write(
            self.style.SUCCESS(
                f"Seeded {created} new webhook(s), skipped {duplicates} already present. "
                f"{total} stored, {pending} awaiting the worker."
            )
        )

    def _create_admin(self, username: str, password: str) -> None:
        """
        A login for the demo stack, and only when one is asked for.

        The review console is the screen worth looking at, and a reviewer who
        boots this with one command should be able to reach it. But a
        superuser conjured out of a default password is how demo credentials
        end up in a real deployment, so nothing is created unless a password
        is passed in explicitly — compose passes one, `manage.py seed_demo`
        on its own does not.
        """
        if not password:
            return

        user_model = get_user_model()
        user, created = user_model.objects.get_or_create(
            username=username,
            defaults={"is_staff": True, "is_superuser": True, "email": ""},
        )
        if not created:
            # Left alone on purpose: overwriting the password on every
            # container start would silently undo an operator's change.
            self.stdout.write(f"Admin user '{username}' already exists; left unchanged.")
            return

        user.is_staff = True
        user.is_superuser = True
        user.set_password(password)
        user.save()
        self.stdout.write(
            self.style.WARNING(
                f"Created demo superuser '{username}'. This is a demo credential: "
                "do not pass --admin-password in any deployment that matters."
            )
        )

    @staticmethod
    def _variant(name: str, template: dict[str, Any], index: int) -> dict[str, Any]:
        payload = dict(template)
        status_key, statuses = _STATUS_CYCLE.get(name, (None, []))
        if status_key and statuses:
            payload[status_key] = statuses[index % len(statuses)]

        suffix = f"-{index:02d}"
        for key in _ID_KEYS:
            if key in payload:
                payload[key] = f"{payload[key]}{suffix}"
        for key in ("shipment_reference", "container_no", "invoice_number"):
            if key in payload:
                payload[key] = f"{payload[key]}{suffix}"

        # Walk the timestamps backwards so entity state has an ordering to
        # respect rather than a pile of identical instants.
        moment = (datetime.now(UTC) - timedelta(hours=index * 3)).isoformat()
        for key in _TIME_KEYS:
            if key in payload:
                payload[key] = moment
        return payload
