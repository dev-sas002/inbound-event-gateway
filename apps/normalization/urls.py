from django.urls import path

from apps.normalization.views import (
    CalibrationView,
    LowConfidenceQueueView,
    ReviewDecisionView,
    ReviewQueueView,
    VendorDiscoveryView,
    VendorProfileDetailView,
    VendorProfileListView,
)

urlpatterns = [
    path(
        "normalization/low-confidence/",
        LowConfidenceQueueView.as_view(),
        name="low-confidence-events",
    ),
    path(
        "normalization/review-queue/",
        ReviewQueueView.as_view(),
        name="review-queue",
    ),
    path(
        "normalization/review-queue/<int:event_id>/",
        ReviewDecisionView.as_view(),
        name="review-decision",
    ),
    path(
        "normalization/calibration/",
        CalibrationView.as_view(),
        name="calibration-report",
    ),
    path(
        "normalization/vendor-profiles/",
        VendorProfileListView.as_view(),
        name="vendor-profiles",
    ),
    path(
        "normalization/vendor-profiles/discover/",
        VendorDiscoveryView.as_view(),
        name="vendor-profile-discover",
    ),
    path(
        "normalization/vendor-profiles/<str:vendor>/",
        VendorProfileDetailView.as_view(),
        name="vendor-profile-detail",
    ),
]
