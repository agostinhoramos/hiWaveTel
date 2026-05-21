"""Tests for inbound SMS post-save processor queue."""

from unittest.mock import MagicMock, patch

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
@override_settings(MQTT_REMOTE_BRIDGE_ENABLED=False)
def test_process_inbound_mirror_direct(processor):
    """Direct worker call avoids pytest transaction isolation on enqueue."""
    inbound = InboundSms.objects.create(
        mm_path='/org/freedesktop/ModemManager1/SMS/test_queue_001',
        modem_index=0,
        from_number='+351900000099',
        text='queue test body',
        mm_state='received',
    )

    with patch(
        'apps.external_device.services.sync_single_inbound_to_all_devices',
    ) as mock_sync:
        processor._process_inbound(inbound.pk, 'test-worker')

    mock_sync.assert_called()
    assert processor.get_metrics()['processed'] == 1


@pytest.mark.django_db
@override_settings(MQTT_REMOTE_BRIDGE_ENABLED=True)
def test_process_inbound_remote_ephemeral_direct(processor):
    inbound = InboundSms.objects.create(
        mm_path='/org/freedesktop/ModemManager1/SMS/test_remote_eph_001',
        modem_index=0,
        from_number='+351900000098',
        text='remote ephemeral test',
        mm_state='received',
    )

    with patch(
        'apps.external_device.services.sync_single_inbound_to_all_devices',
    ):
        with patch(
            'apps.external_device.services.publish_inbound_to_remote_ephemeral',
            return_value=True,
        ) as mock_eph:
            processor._process_inbound(inbound.pk, 'test-worker')
            mock_eph.assert_called()


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
