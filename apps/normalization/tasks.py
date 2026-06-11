from __future__ import annotations

import logging
from time import perf_counter

from celery import shared_task
from celery.exceptions import MaxRetriesExceededError, Retry
from django.db import transaction

from apps.ingestion.models import ProcessingStatus, RawWebhook
from apps.normalization.backends import get_normalizer
from apps.normalization.exceptions import NormalizationError, RetryableNormalizationError
from apps.normalization.repositories import NormalizedEventRepository
from apps.normalization.review import apply_confidence_gate

logger = logging.getLogger("apps.normalization")


@shared_task(bind=True, max_retries=5, autoretry_for=())
def process_raw_webhook(self, webhook_id: str) -> None:
    start = perf_counter()
    task_id = getattr(self.request, "id", None)

    webhook = RawWebhook.objects.filter(id=webhook_id).first()
    if webhook is None:
        logger.warning("webhook_not_found", extra={"webhook_id": webhook_id, "task_id": task_id})
        return

    if webhook.processing_status == ProcessingStatus.NORMALIZED:
        logger.info(
            "webhook_already_normalized",
            extra={"webhook_id": webhook_id, "task_id": task_id, "vendor": webhook.vendor},
        )
        return

    try:
        with transaction.atomic():
            locked = RawWebhook.objects.select_for_update().get(id=webhook_id)
            if locked.processing_status == ProcessingStatus.PROCESSING:
                logger.info(
                    "webhook_already_processing",
                    extra={"webhook_id": webhook_id, "vendor": locked.vendor, "task_id": task_id},
                )
                return
            locked.processing_status = ProcessingStatus.PROCESSING
            locked.save(update_fields=["processing_status"])

        service = get_normalizer()
        # The vendor is passed, not inferred: it selects the learned profile
        # for this sender, which is the difference between reading a payload
        # and guessing at it.
        response = service.normalize(webhook.raw_payload, vendor=webhook.vendor)

        normalized_event = NormalizedEventRepository.create_for_webhook(
            webhook=webhook,
            result=response.normalized,
            llm_model=response.llm_model,
            prompt_version=response.prompt_version,
        )
        # The confidence gate. LOW_CONFIDENCE_THRESHOLD was declared in
        # settings but never read by anything — the gate had been designed and
        # not built, so every normalisation updated entity state regardless of
        # how much the normaliser had to guess. The rule itself lives in
        # apps.normalization.review, because the review API and the admin
        # console have to agree with this code about what "trusted" means.
        apply_confidence_gate(normalized_event)

        duration_ms = round((perf_counter() - start) * 1000, 2)
        logger.info(
            "webhook_processed",
            extra={
                "webhook_id": webhook_id,
                "vendor": webhook.vendor,
                "entity_type": normalized_event.entity_type,
                "entity_id": normalized_event.entity_id,
                "processing_status": ProcessingStatus.NORMALIZED,
                "duration_ms": duration_ms,
                "task_id": task_id,
            },
        )
    except RetryableNormalizationError as exc:
        webhook.retry_count += 1
        webhook.processing_status = ProcessingStatus.FAILED
        webhook.error_message = str(exc)
        webhook.save(update_fields=["retry_count", "processing_status", "error_message"])
        # request.retries is None when the task function is called directly
        # rather than through Celery, and 2 ** None is a TypeError that would
        # mask the failure being handled.
        attempt = self.request.retries or 0
        retry_delay = min(2**attempt, 300)
        logger.warning(
            "webhook_retry_scheduled",
            extra={
                "webhook_id": webhook_id,
                "vendor": webhook.vendor,
                "retry_count": webhook.retry_count,
                "task_id": task_id,
            },
        )
        try:
            self.retry(exc=exc, countdown=retry_delay)
        except Retry:
            # The normal path: Celery is rescheduling this task, and the Retry
            # signal has to reach it.
            raise
        except (MaxRetriesExceededError, RetryableNormalizationError):
            # The ladder is finished. Celery only raises MaxRetriesExceeded
            # when retry() is called *without* an exc; given one it re-raises
            # that original exception instead, so catching MaxRetriesExceeded
            # alone left this branch unreachable and the webhook sat in FAILED
            # forever while the task reported an error on every delivery. Both
            # endings mean the same thing: stop retrying and dead-letter it.
            webhook.processing_status = ProcessingStatus.DEAD_LETTER
            webhook.error_message = f"Max retries exceeded: {exc}"
            webhook.save(update_fields=["processing_status", "error_message"])
            logger.error(
                "webhook_dead_letter",
                extra={"webhook_id": webhook_id, "vendor": webhook.vendor, "task_id": task_id},
            )
    except NormalizationError as exc:
        webhook.processing_status = ProcessingStatus.FAILED
        webhook.error_message = str(exc)
        webhook.save(update_fields=["processing_status", "error_message"])
        logger.error(
            "webhook_normalization_failed",
            extra={"webhook_id": webhook_id, "vendor": webhook.vendor, "task_id": task_id},
            exc_info=True,
        )
    except Exception as exc:
        webhook.processing_status = ProcessingStatus.FAILED
        webhook.error_message = f"Unexpected worker failure: {exc}"
        webhook.save(update_fields=["processing_status", "error_message"])
        logger.exception(
            "webhook_worker_unexpected_failure",
            extra={"webhook_id": webhook_id, "vendor": webhook.vendor, "task_id": task_id},
        )
        # Record the failure on the webhook, then let it propagate. Swallowing
        # it here reported the task as *succeeded* to Celery, so a programming
        # error looked like a perfectly healthy worker while every webhook
        # silently went to FAILED — the one failure mode nobody would notice.
        # There is no retry (autoretry_for is empty and this is not a
        # RetryableNormalizationError): an unexpected exception is a bug, and
        # retrying a bug five times only multiplies the damage.
        raise
