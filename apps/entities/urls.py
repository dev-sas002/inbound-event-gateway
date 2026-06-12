from django.urls import path

from apps.entities.views import EntityStateView

urlpatterns = [
    path("entities/state/", EntityStateView.as_view(), name="entity-state"),
]
