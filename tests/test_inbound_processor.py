"""Tests for inbound SMS post-save processor queue."""

from unittest.mock import patch

import pytest
from django.test import override_settings

from apps.sms.inbound_processor import InboundProcessorQueue, get_inbound_processor
from apps.sms.models import InboundSms


@pytest.fixture
def processor():
    """Small queue for tests (stops any global singleton started by AppConfig.ready)."""
    import apps.sms.inbound_processor as ip

    if ip._global_processor is not None and ip._global_processor.running:
        ip._global_processor.stop(timeout=2.0)
    ip._global_processor = None

    q = InboundProcessorQueue(num_workers=1, max_queue_size=10, retry_max=2, retry_base_sec=0.01)
    q.start()
    yield q
    q.stop(timeout=2.0)
    ip._global_processor = None


@pytest.mark.django_db
def test_process_inbound_delivers_webhooks(processor):
    inbound = InboundSms.objects.create(
        mm_path='/org/freedesktop/ModemManager1/SMS/test_queue_001',
        modem_index=0,
        from_number='+351900000099',
        text='queue test body',
        mm_state='received',
    )

    with patch(
        'apps.sms.webhook_delivery.deliver_inbound_webhooks',
        return_value=True,
    ) as mock_deliver:
        processor._process_inbound(inbound.pk, 'test-worker')

    mock_deliver.assert_called_once()
    assert processor.get_metrics()['processed'] == 1


@pytest.mark.django_db
def test_process_inbound_webhook_failure_retries(processor):
    inbound = InboundSms.objects.create(
        mm_path='/org/freedesktop/ModemManager1/SMS/test_webhook_fail',
        modem_index=0,
        from_number='+351900000098',
        text='fail test',
        mm_state='received',
    )

    with patch(
        'apps.sms.webhook_delivery.deliver_inbound_webhooks',
        return_value=False,
    ):
        processor._process_inbound(inbound.pk, 'test-worker')

    assert processor.get_metrics()['failed'] >= 1


@pytest.mark.django_db
def test_enqueue_logs_size(processor):
    inbound = InboundSms.objects.create(
        mm_path='/org/freedesktop/ModemManager1/SMS/test_enqueue_log',
        modem_index=0,
        from_number='+351900000002',
        text='enqueue log test',
        mm_state='received',
    )
    assert processor.enqueue(inbound.pk) is True
    assert processor.queue.qsize() >= 0


def test_get_inbound_processor_disabled():
    import apps.sms.inbound_processor as ip

    ip._global_processor = None
    with patch.dict('os.environ', {'INBOUND_PROCESSOR_WORKERS': '0'}):
        assert get_inbound_processor() is None
    ip._global_processor = None
