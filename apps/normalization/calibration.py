"""
Is the confidence threshold set in the right place?

The gate holds anything the normaliser was not sure about, and a person then
approves or rejects it. Those decisions are the only ground truth this system
ever gets, and they answer a question nobody can answer from the threshold
alone: is the gate holding work that people keep waving through?

The report is deliberately one-directional about what it can prove. Every
reviewed event is, by construction, one the gate already held — so the data can
show that a threshold is too *high* (approvals piling up just under it) but it
can never show that it is too low, because events above the line were never put
in front of anybody. The report says so rather than implying otherwise.

The interesting third outcome is neither: when approvals and rejections overlap
in confidence, the score is not separating good readings from bad ones for that
vendor, and no threshold will fix it. That vendor needs a profile — see
`apps.normalization.discovery`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from django.conf import settings
from django.db.models import Avg, Count, Max, Min, Q

from apps.normalization.models import NormalizedEvent, ReviewDecision

#: Fewer decisions than this and any conclusion is noise, so the report
#: declines to draw one rather than recommending a threshold off two samples.
MIN_DECISIONS_FOR_ADVICE = 5

SEPARATED = "separated"
OVERLAPPING = "overlapping"
ALL_REJECTED = "all_rejected"
INSUFFICIENT = "insufficient_decisions"


@dataclass(frozen=True)
class VendorCalibration:
    """One vendor's row of the report."""

    vendor: str
    events: int
    held: int
    reviewed: int
    approved: int
    rejected: int
    mean_confidence: float | None
    mean_confidence_approved: float | None
    mean_confidence_rejected: float | None
    lowest_approved: float | None
    highest_rejected: float | None
    verdict: str
    suggested_threshold: float | None
    note: str

    @property
    def hold_rate(self) -> float:
        return round(self.held / self.events, 3) if self.events else 0.0

    @property
    def approval_rate(self) -> float | None:
        return round(self.approved / self.reviewed, 3) if self.reviewed else None

    def as_dict(self) -> dict[str, object]:
        return {
            "vendor": self.vendor,
            "events": self.events,
            "held": self.held,
            "hold_rate": self.hold_rate,
            "reviewed": self.reviewed,
            "approved": self.approved,
            "rejected": self.rejected,
            "approval_rate": self.approval_rate,
            "mean_confidence": self.mean_confidence,
            "mean_confidence_approved": self.mean_confidence_approved,
            "mean_confidence_rejected": self.mean_confidence_rejected,
            "lowest_approved": self.lowest_approved,
            "highest_rejected": self.highest_rejected,
            "verdict": self.verdict,
            "suggested_threshold": self.suggested_threshold,
            "note": self.note,
        }


@dataclass(frozen=True)
class CalibrationReport:
    current_threshold: float
    vendors: tuple[VendorCalibration, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "current_threshold": self.current_threshold,
            "count": len(self.vendors),
            "results": [vendor.as_dict() for vendor in self.vendors],
        }


def _round(value: float | None) -> float | None:
    return round(float(value), 3) if value is not None else None


def _assess(
    *,
    threshold: float,
    reviewed: int,
    approved: int,
    rejected: int,
    lowest_approved: float | None,
    highest_rejected: float | None,
) -> tuple[str, float | None, str]:
    """The verdict, a suggested threshold when one is defensible, and why."""
    if reviewed < MIN_DECISIONS_FOR_ADVICE:
        return (
            INSUFFICIENT,
            None,
            f"only {reviewed} decision(s) on file; {MIN_DECISIONS_FOR_ADVICE} needed "
            "before the threshold is worth moving",
        )

    if approved == 0:
        return (
            ALL_REJECTED,
            None,
            "every held event was rejected, so the gate is earning its keep here; "
            "the data cannot say whether it should hold more",
        )

    if rejected == 0 or (
        lowest_approved is not None
        and highest_rejected is not None
        and lowest_approved > highest_rejected
    ):
        suggested = _round(lowest_approved)
        # Only advice if it actually moves: a suggestion equal to the current
        # threshold is not a finding.
        if suggested is not None and suggested < threshold:
            return (
                SEPARATED,
                suggested,
                f"confidence separates the {approved} approval(s) from the "
                f"{rejected} rejection(s); a threshold of {suggested} would have "
                "auto-applied every approved event and still held every rejected one",
            )
        return (
            SEPARATED,
            None,
            "confidence separates approvals from rejections and the current "
            "threshold already sits at the right place",
        )

    return (
        OVERLAPPING,
        None,
        "approved and rejected events overlap in confidence, so no threshold "
        "separates them; this vendor needs a profile rather than a different number",
    )


def calibration_report(*, since: datetime | None = None) -> CalibrationReport:
    """
    Per-vendor gate behaviour, in one aggregate query.

    Grouping happens in the database rather than in Python: the interesting
    deployments have millions of normalised events, and pulling them back to
    count them would be the slowest thing this service does.
    """
    threshold = float(settings.LOW_CONFIDENCE_THRESHOLD)
    approved_q = Q(review_decision=ReviewDecision.APPROVE)
    rejected_q = Q(review_decision=ReviewDecision.REJECT)

    queryset = NormalizedEvent.objects.all()
    if since is not None:
        queryset = queryset.filter(created_at__gte=since)

    rows = (
        queryset.values("webhook__vendor")
        .annotate(
            events=Count("id"),
            held=Count("id", filter=Q(requires_review=True)),
            approved=Count("id", filter=approved_q),
            rejected=Count("id", filter=rejected_q),
            mean_confidence=Avg("confidence_score"),
            mean_confidence_approved=Avg("confidence_score", filter=approved_q),
            mean_confidence_rejected=Avg("confidence_score", filter=rejected_q),
            lowest_approved=Min("confidence_score", filter=approved_q),
            highest_rejected=Max("confidence_score", filter=rejected_q),
        )
        .order_by("webhook__vendor")
    )

    vendors: list[VendorCalibration] = []
    for row in rows:
        approved = row["approved"]
        rejected = row["rejected"]
        reviewed = approved + rejected
        lowest_approved = _round(row["lowest_approved"])
        highest_rejected = _round(row["highest_rejected"])
        verdict, suggested, note = _assess(
            threshold=threshold,
            reviewed=reviewed,
            approved=approved,
            rejected=rejected,
            lowest_approved=lowest_approved,
            highest_rejected=highest_rejected,
        )
        vendors.append(
            VendorCalibration(
                vendor=row["webhook__vendor"],
                events=row["events"],
                held=row["held"],
                reviewed=reviewed,
                approved=approved,
                rejected=rejected,
                mean_confidence=_round(row["mean_confidence"]),
                mean_confidence_approved=_round(row["mean_confidence_approved"]),
                mean_confidence_rejected=_round(row["mean_confidence_rejected"]),
                lowest_approved=lowest_approved,
                highest_rejected=highest_rejected,
                verdict=verdict,
                suggested_threshold=suggested,
                note=note,
            )
        )

    return CalibrationReport(current_threshold=threshold, vendors=tuple(vendors))
