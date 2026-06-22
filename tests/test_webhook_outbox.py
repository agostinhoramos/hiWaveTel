"""Tests for durable webhook delivery outbox."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from django.db import OperationalError
from django.test import override_settings

from apps.sms.models import InboundSms, OutboundSms, WebhookDeliveryJob
from apps.sms.webhook_outbox import (
    claim_next_job,
    process_webhook_job,
    schedule_inbound_webhook,
    schedule_outbound_webhook,
)


@pytest.mark.django_db
def test_schedule_inbound_webhook_creates_job():
    inbound = InboundSms.objects.create(
        mm_path='/org/freedesktop/ModemManager1/SMS/outbox1',
        modem_index=0,
        from_number='+351912345678',
        text='hello',
        mm_state='received',
    )
    job = schedule_inbound_webhook(inbound)
    assert job.pk
    assert job.kind == WebhookDeliveryJob.Kind.INBOUND
    assert job.status == WebhookDeliveryJob.Status.PENDING
    assert job.inbound_sms_id == inbound.pk


@pytest.mark.django_db
def test_schedule_inbound_webhook_idempotent():
    inbound = InboundSms.objects.create(
        mm_path='/org/freedesktop/ModemManager1/SMS/outbox2',
        modem_index=0,
        from_number='+351912345678',
        text='hello',
        mm_state='received',
    )
    first = schedule_inbound_webhook(inbound)
    second = schedule_inbound_webhook(inbound)
    assert first.pk == second.pk
    assert WebhookDeliveryJob.objects.count() == 1


@pytest.mark.django_db
def test_claim_next_job_marks_processing():
    inbound = InboundSms.objects.create(
        mm_path='/org/freedesktop/ModemManager1/SMS/outbox3',
        modem_index=0,
        from_number='+351912345678',
        text='claim me',
        mm_state='received',
    )
    schedule_inbound_webhook(inbound)
    job = claim_next_job()
    assert job is not None
    assert job.status == WebhookDeliveryJob.Status.PROCESSING
    assert claim_next_job() is None


@pytest.mark.django_db
@override_settings()
def test_process_webhook_job_delivers_inbound():
    inbound = InboundSms.objects.create(
        mm_path='/org/freedesktop/ModemManager1/SMS/outbox4',
        modem_index=0,
        from_number='+351912345678',
        text='deliver',
        mm_state='received',
    )
    job = schedule_inbound_webhook(inbound)
    job.status = WebhookDeliveryJob.Status.PROCESSING
    job.save(update_fields=['status'])

    with patch('apps.sms.webhook_delivery.deliver_inbound_webhooks', return_value=True) as mock:
        assert process_webhook_job(job) is True
    mock.assert_called_once()
    job.refresh_from_db()
    assert job.status == WebhookDeliveryJob.Status.DELIVERED
    assert job.delivered_at is not None


@pytest.mark.django_db
def test_schedule_outbound_webhook():
    outbound = OutboundSms.objects.create(
        modem_index=0,
        to_number='+351913000387',
        text='sent',
        state=OutboundSms.State.SENT,
    )
    job = schedule_outbound_webhook(outbound)
    assert job.kind == WebhookDeliveryJob.Kind.OUTBOUND
    assert job.outbound_sms_id == outbound.pk


@pytest.mark.django_db
def test_claim_next_job_retries_on_sqlite_locked():
    inbound = InboundSms.objects.create(
        mm_path='/org/freedesktop/ModemManager1/SMS/outbox5',
        modem_index=0,
        from_number='+351912345678',
        text='retry claim',
        mm_state='received',
    )
    schedule_inbound_webhook(inbound)

    from apps.sms import webhook_outbox as outbox_mod

    calls = {'n': 0}
    real_once = outbox_mod._claim_next_job_once

    def flaky_claim():
        calls['n'] += 1
        if calls['n'] == 1:
            raise OperationalError('database is locked')
        return real_once()

    with patch.object(outbox_mod, '_claim_next_job_once', side_effect=flaky_claim):
        with patch.object(outbox_mod.time, 'sleep', return_value=None):
            job = claim_next_job()

    assert job is not None
    assert job.status == WebhookDeliveryJob.Status.PROCESSING
    assert calls['n'] == 2


@pytest.mark.django_db
def test_post_save_signal_queues_webhook_job():
    with patch('django.db.transaction.on_commit', side_effect=lambda fn: fn()):
        inbound = InboundSms.objects.create(
            mm_path='/org/freedesktop/ModemManager1/SMS/signal1',
            modem_index=0,
            from_number='+351912345678',
            text='signal test',
            mm_state='received',
        )
    assert WebhookDeliveryJob.objects.filter(
        inbound_sms=inbound,
        status=WebhookDeliveryJob.Status.PENDING,
    ).exists()
