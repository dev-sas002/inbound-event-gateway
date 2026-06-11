"""Propose a vendor profile from sample payload files."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError

from apps.ingestion.models import RawWebhook
from apps.normalization.discovery import DiscoveryError, discover_profile
from apps.normalization.repositories import VendorProfileRepository


class Command(BaseCommand):
    help = "Infer a vendor profile from sample webhook payloads."

    def add_arguments(self, parser) -> None:
        parser.add_argument("vendor", help="Vendor name, as it appears on inbound webhooks.")
        parser.add_argument(
            "paths",
            nargs="*",
            help="JSON files, or directories of them, holding real payloads from this vendor.",
        )
        parser.add_argument(
            "--from-stored",
            type=int,
            default=0,
            metavar="N",
            help=(
                "Learn from the last N payloads this vendor actually sent, instead of "
                "from files. Usually the right answer: the service already has them."
            ),
        )
        parser.add_argument(
            "--persist",
            action="store_true",
            help="Save the proposal as this vendor's profile.",
        )
        parser.add_argument(
            "--no-model",
            action="store_true",
            help="Skip the model-assisted pass even if an OpenAI key is configured.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        payloads = self._load(options["paths"])
        if options["from_stored"]:
            payloads.extend(self._stored(options["vendor"], options["from_stored"]))
        if not payloads:
            raise CommandError(
                "No payloads to learn from. Give JSON files, or --from-stored N to use "
                "what this vendor has already sent."
            )

        try:
            result = discover_profile(
                options["vendor"],
                payloads,
                use_model=False if options["no_model"] else None,
            )
        except DiscoveryError as exc:
            raise CommandError(str(exc)) from exc

        profile = result.profile
        self.stdout.write(
            self.style.MIGRATE_HEADING(
                f"{profile.vendor}  ({result.method}, {result.sample_count} sample(s))"
            )
        )
        self.stdout.write(f"  entity_type   {profile.entity_type or '(undecided)'}")
        self.stdout.write(f"  id_paths      {', '.join(profile.id_paths) or '(none)'}")
        self.stdout.write(f"  status_paths  {', '.join(profile.status_paths) or '(none)'}")
        self.stdout.write(f"  time_paths    {', '.join(profile.time_paths) or '(none)'}")
        self.stdout.write("  status_map")
        for token, canonical in sorted(profile.status_map.items()):
            self.stdout.write(f"    {token:<28} -> {canonical}")
        if not profile.status_map:
            self.stdout.write("    (none)")

        for token in result.unmapped_statuses:
            # Called out rather than buried: these are the words a person has
            # to decide, and they are the whole reason to read this output.
            self.stdout.write(self.style.WARNING(f"  unmapped status: {token}"))
        for warning in result.warnings:
            self.stdout.write(self.style.WARNING(f"  warning: {warning}"))
        if profile.notes:
            self.stdout.write(f"  notes         {profile.notes}")

        if options["persist"]:
            VendorProfileRepository.save(profile)
            self.stdout.write(self.style.SUCCESS(f"\nSaved profile for {profile.vendor}."))
        else:
            self.stdout.write(
                "\nProposal only. Re-run with --persist to save it, or edit it in the admin."
            )

    @staticmethod
    def _stored(vendor: str, limit: int) -> list[Any]:
        """
        The payloads this vendor has already sent.

        Every raw body is kept, so the best sample set for learning a vendor's
        shape is the traffic that is already in the table — no asking anybody
        for example files.
        """
        return list(
            RawWebhook.objects.filter(vendor__iexact=vendor)
            .order_by("-received_at")
            .values_list("raw_payload", flat=True)[:limit]
        )

    @staticmethod
    def _load(paths: list[str]) -> list[Any]:
        payloads: list[Any] = []
        for raw in paths:
            path = Path(raw)
            if path.is_dir():
                files = sorted(path.glob("*.json"))
            elif path.exists():
                files = [path]
            else:
                raise CommandError(f"No such file or directory: {raw}")
            for file in files:
                try:
                    loaded = json.loads(file.read_text(encoding="utf-8"))
                except json.JSONDecodeError as exc:
                    raise CommandError(f"{file} is not valid JSON: {exc}") from exc
                # A file may hold one payload or a list of them; both are
                # shapes people actually have lying around.
                payloads.extend(loaded if isinstance(loaded, list) else [loaded])
        return payloads
