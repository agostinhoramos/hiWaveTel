"""Durable DB-backed webhook delivery queue."""

from __future__ import annotations

import logging
import os
import random
import time
from collections.abc import Callable
from datetime import timedelta
from typing import TypeVar

from django.conf import settings
from django.db import OperationalError, close_old_connections, transaction
from django.utils import timezone

from apps.sms.models import InboundSms, OutboundSms, WebhookDeliveryJob
from apps.sms.services import _looks_sqlite_concurrency_error

_LOGGER = logging.getLogger(__name__)

_T = TypeVar('_T')


def _max_attempts() -> int:
    return int(os.environ.get('WEBHOOK_JOB_MAX_ATTEMPTS', '10'))


def _with_sqlite_retry(fn: Callable[[], _T], *, label: str) -> _T:
    """Retry SQLite busy/locked errors (watcher + webhook workers share one DB)."""
    retries = int(getattr(settings, 'SQLITE_LOCKED_RETRY_COUNT', 15))
    backoff_sec = float(getattr(settings, 'SQLITE_LOCKED_RETRY_BACKOFF_SEC', 0.02))
    last_exc: OperationalError | None = None

    for attempt in range(retries):
        try:
            return fn()
        except OperationalError as exc:
            last_exc = exc
            if not _looks_sqlite_concurrency_error(exc) or attempt >= retries - 1:
                raise
            delay = backoff_sec * (2**attempt) + random.random() * 0.02
            _LOGGER.warning(
                'SQLite busy %s (attempt %s/%s); retry %.3fs',
                label,
                attempt + 1,
                retries,
                delay,
            )
            close_old_connections()
            time.sleep(delay)

    if last_exc is not None:
        raise last_exc
    raise RuntimeError(f'SQLite retry exhausted for {label}')


def schedule_inbound_webhook(inbound: InboundSms) -> WebhookDeliveryJob:
    """Queue inbound SMS for webhook delivery (idempotent)."""

    def _schedule() -> WebhookDeliveryJob:
        job, created = WebhookDeliveryJob.objects.get_or_create(
            inbound_sms=inbound,
            defaults={
                'kind': WebhookDeliveryJob.Kind.INBOUND,
                'status': WebhookDeliveryJob.Status.PENDING,
            },
        )
        if not created and job.status != WebhookDeliveryJob.Status.DELIVERED:
            job.status = WebhookDeliveryJob.Status.PENDING
            job.last_error = ''
            job.save(update_fields=['status', 'last_error', 'updated_at'])
        if created:
            _LOGGER.info('Webhook job queued inbound pk=%s job=%s', inbound.pk, job.pk)
        return job

    return _with_sqlite_retry(_schedule, label=f'schedule inbound webhook pk={inbound.pk}')


def schedule_outbound_webhook(outbound: OutboundSms) -> WebhookDeliveryJob:
    """Queue outbound SMS for webhook delivery (idempotent)."""

    def _schedule() -> WebhookDeliveryJob:
        job, created = WebhookDeliveryJob.objects.get_or_create(
            outbound_sms=outbound,
            defaults={
                'kind': WebhookDeliveryJob.Kind.OUTBOUND,
                'status': WebhookDeliveryJob.Status.PENDING,
            },
        )
        if not created and job.status != WebhookDeliveryJob.Status.DELIVERED:
            job.status = WebhookDeliveryJob.Status.PENDING
            job.last_error = ''
            job.save(update_fields=['status', 'last_error', 'updated_at'])
        if created:
            _LOGGER.info('Webhook job queued outbound pk=%s job=%s', outbound.pk, job.pk)
        return job

    return _with_sqlite_retry(_schedule, label=f'schedule outbound webhook pk={outbound.pk}')


def reset_stale_processing_jobs(*, max_age_sec: int | None = None) -> int:
    """Return stuck processing jobs to pending (worker crash recovery)."""
    age = max_age_sec if max_age_sec is not None else int(
        os.environ.get('WEBHOOK_JOB_STALE_SEC', '300'),
    )
    cutoff = timezone.now() - timedelta(seconds=age)

    def _reset() -> int:
        return WebhookDeliveryJob.objects.filter(
            status=WebhookDeliveryJob.Status.PROCESSING,
            updated_at__lt=cutoff,
        ).update(
            status=WebhookDeliveryJob.Status.PENDING,
            last_error='Reset stale processing job',
        )

    updated = _with_sqlite_retry(_reset, label='reset stale webhook jobs')
    if updated:
        _LOGGER.warning('Reset %s stale webhook processing job(s)', updated)
    return updated


def _claim_next_job_once() -> WebhookDeliveryJob | None:
    with transaction.atomic():
        job = (
            WebhookDeliveryJob.objects.select_for_update()
            .filter(status=WebhookDeliveryJob.Status.PENDING)
            .order_by('created_at', 'pk')
            .first()
        )
        if job is None:
            return None
        job.status = WebhookDeliveryJob.Status.PROCESSING
        job.save(update_fields=['status', 'updated_at'])
        return job


def claim_next_job() -> WebhookDeliveryJob | None:
    """Atomically claim the oldest pending webhook job."""
    return _with_sqlite_retry(_claim_next_job_once, label='claim webhook job')


def process_webhook_job(job: WebhookDeliveryJob) -> bool:
    """Deliver one webhook job; update status and return success."""
    from apps.sms.webhook_delivery import deliver_inbound_webhooks, deliver_outbound_webhooks

    def _bump_attempts() -> None:
        job.attempts += 1
        job.save(update_fields=['attempts', 'updated_at'])

    _with_sqlite_retry(_bump_attempts, label=f'webhook job {job.pk} bump attempts')

    try:
        if job.kind == WebhookDeliveryJob.Kind.INBOUND:
            if job.inbound_sms_id is None:
                raise ValueError('Inbound webhook job missing inbound_sms')
            ok = deliver_inbound_webhooks(job.inbound_sms)
        elif job.kind == WebhookDeliveryJob.Kind.OUTBOUND:
            if job.outbound_sms_id is None:
                raise ValueError('Outbound webhook job missing outbound_sms')
            ok = deliver_outbound_webhooks(job.outbound_sms)
        else:
            raise ValueError(f'Unknown webhook job kind: {job.kind}')
    except Exception as exc:
        _LOGGER.exception('Webhook job %s failed: %s', job.pk, exc)
        ok = False
        job.last_error = str(exc)[:2000]
    else:
        if not ok:
            job.last_error = 'One or more webhook URLs failed'

    def _finalize() -> None:
        if ok:
            job.status = WebhookDeliveryJob.Status.DELIVERED
            job.delivered_at = timezone.now()
            job.last_error = ''
            job.save(update_fields=['status', 'delivered_at', 'last_error', 'updated_at'])
            return
        if job.attempts >= _max_attempts():
            job.status = WebhookDeliveryJob.Status.FAILED
            job.save(update_fields=['status', 'last_error', 'updated_at'])
            return
        job.status = WebhookDeliveryJob.Status.PENDING
        job.save(update_fields=['status', 'last_error', 'updated_at'])

    _with_sqlite_retry(_finalize, label=f'webhook job {job.pk} finalize')

    if ok:
        _LOGGER.info('Webhook job %s delivered (%s)', job.pk, job.kind)
        return True
    if job.attempts >= _max_attempts():
        _LOGGER.error(
            'Webhook job %s failed permanently after %s attempts',
            job.pk,
            job.attempts,
        )
        return False

    _LOGGER.warning(
        'Webhook job %s will retry (attempt %s/%s)',
        job.pk,
        job.attempts,
        _max_attempts(),
    )
    return False


def pending_job_count() -> int:
    return WebhookDeliveryJob.objects.filter(
        status=WebhookDeliveryJob.Status.PENDING,
    ).count()
