"""Print the per-vendor confidence gate calibration report."""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand

from apps.normalization.calibration import OVERLAPPING, SEPARATED, calibration_report


class Command(BaseCommand):
    help = "Report how the confidence gate is behaving for each vendor."

    def handle(self, *args: Any, **options: Any) -> None:
        report = calibration_report()
        self.stdout.write(
            self.style.MIGRATE_HEADING(
                f"Confidence gate calibration (threshold {report.current_threshold:.2f})"
            )
        )
        if not report.vendors:
            self.stdout.write("No normalized events yet.")
            return

        header = f"{'vendor':<24}{'events':>8}{'held':>7}{'hold%':>8}{'appr':>6}{'rej':>5}  verdict"
        self.stdout.write(header)
        self.stdout.write("-" * len(header))
        for vendor in report.vendors:
            self.stdout.write(
                f"{vendor.vendor[:23]:<24}{vendor.events:>8}{vendor.held:>7}"
                f"{vendor.hold_rate * 100:>7.1f}%{vendor.approved:>6}{vendor.rejected:>5}"
                f"  {vendor.verdict}"
            )

        self.stdout.write("")
        for vendor in report.vendors:
            style = self.style.SUCCESS if vendor.verdict == SEPARATED else self.style.WARNING
            if vendor.verdict == OVERLAPPING:
                style = self.style.ERROR
            self.stdout.write(style(f"{vendor.vendor}: {vendor.note}"))
            if vendor.suggested_threshold is not None:
                self.stdout.write(
                    f"  suggested LOW_CONFIDENCE_THRESHOLD={vendor.suggested_threshold}"
                )
