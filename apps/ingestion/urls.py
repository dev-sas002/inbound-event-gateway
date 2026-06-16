from django.urls import path

from apps.ingestion.views import HealthView, WebhookIngestionView, WebhookReplayView

urlpatterns = [
    path("health/", HealthView.as_view(), name="health"),
    path("webhooks/", WebhookIngestionView.as_view(), name="webhook-ingest"),
    path("webhooks/<uuid:webhook_id>/replay/", WebhookReplayView.as_view(), name="webhook-replay"),
]
