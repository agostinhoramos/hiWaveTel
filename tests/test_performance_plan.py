"""Tests for performance edge gateway plan (outbound queue, MQTT offload, mmcli lock)."""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock, patch

import pytest
from django.test import override_settings

from apps.external_device.models import ExternalDevice, SmsDispatchOutbox, SmsRequest
from apps.external_device.mqtt_handler_queue import classify_mqtt_topic
from apps.external_device.services import process_sms_request
from apps.sms.mmcli_lock import mmcli_serial
from apps.sms.models import OutboundSms
from apps.sms.outbound_processor import OutboundProcessorQueue


@pytest.fixture
def active_device(db):
    """Create an active device with API key."""
    from apps.external_device.authentication import hash_api_key
    from apps.external_device.services import generate_token

    raw_api_key = generate_token(48)
    api_key_hash = hash_api_key(raw_api_key)
    device = ExternalDevice.objects.create(
        device_id='+351913000002',
        name='Active Device',
        device_type='modem',
        api_key_hash=api_key_hash,
        status=ExternalDevice.Status.ACTIVE,
    )
    device.raw_api_key = raw_api_key
    return device


@pytest.mark.django_db
@override_settings(OUTBOUND_ASYNC_ENABLED=True, MQTT_PUBLISH_SEND_REQUEST=False)
@patch('apps.external_device.services._dispatch_sms_request_sync')
def test_process_sms_request_async_queues(mock_dispatch, active_device):
    """Async path returns QUEUED without calling mmcli dispatch inline."""
    sms_request = process_sms_request(
        device=active_device,
        recipients=['+351912345678'],
        message='Async test',
        priority='high',
    )
    assert sms_request.status == SmsRequest.Status.QUEUED
    mock_dispatch.assert_not_called()
    assert SmsDispatchOutbox.objects.filter(reference=sms_request.request_id).exists()


@pytest.mark.django_db
@override_settings(OUTBOUND_ASYNC_ENABLED=True)
def test_outbound_processor_drains_sms_request(active_device):
    """Worker processes queued SmsRequest via sync dispatch helper."""
    with patch('apps.sms.outbound_processor._dispatch_sms_request_sync') as mock_sync:
        proc = OutboundProcessorQueue(num_workers=1, max_queue_size=100, poll_interval_sec=0.05)
        proc.start()
        try:
            sms_request = SmsRequest.objects.create(
                request_id='sms_testqueue1',
                device=active_device,
                recipients=['+351900000001'],
                message='hello',
                priority='normal',
                status=SmsRequest.Status.QUEUED,
            )
            assert proc.enqueue('sms_request', sms_request.request_id, priority='normal')
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline and mock_sync.call_count == 0:
                time.sleep(0.05)
            assert mock_sync.call_count >= 1
        finally:
            proc.stop()


def test_classify_mqtt_topic_sms_inbox():
    key, rank = classify_mqtt_topic('hidishelink_dev/devices/351912345678/sms/inbox')
    assert key == 'sms_inbox'
    assert rank == 0


def test_mmcli_lock_serializes():
    order: list[int] = []
    barrier = threading.Barrier(2)

    def worker(n: int) -> None:
        barrier.wait()
        with mmcli_serial():
            order.append(n)
            time.sleep(0.02)

    t1 = threading.Thread(target=worker, args=(1,))
    t2 = threading.Thread(target=worker, args=(2,))
    t1.start()
    t2.start()
    t1.join(timeout=5)
    t2.join(timeout=5)
    assert len(order) == 2
    assert order[0] != order[1]


@pytest.mark.django_db
def test_sanitized_device_id_on_save(active_device):
    active_device.save()
    active_device.refresh_from_db()
    assert active_device.sanitized_device_id == active_device.device_id.replace('+', '')


@pytest.mark.django_db
@override_settings(OUTBOUND_ASYNC_ENABLED=False)
@patch('apps.external_device.services.dispatch_outbound_mmcli')
def test_process_sms_request_sync_when_async_disabled(mock_dispatch, active_device):
    def _ok(outbound):
        outbound.state = OutboundSms.State.SENT
        outbound.save()
        return outbound

    mock_dispatch.side_effect = _ok
    sms_request = process_sms_request(
        device=active_device,
        recipients=['+351912345678'],
        message='sync',
    )
    assert sms_request.status == SmsRequest.Status.COMPLETED
    mock_dispatch.assert_called_once()


def test_mqtt_handler_queue_offloads_without_orm(monkeypatch):
    """_on_message enqueues without calling persist_inbox_from_mqtt when queue enabled."""
    from apps.external_device.mqtt_client import GatewayMqttClient

    client = GatewayMqttClient(mqtt_config={})
    enqueued: list[str] = []

    class FakeQueue:
        def enqueue(self, handler_key, topic, payload, *, client_ref=None, rank=2):
            enqueued.append(handler_key)
            return True

    monkeypatch.setattr(
        'apps.external_device.mqtt_handler_queue.get_mqtt_handler_queue',
        lambda: FakeQueue(),
    )
    monkeypatch.setattr(
        'apps.external_device.mqtt_client.persist_inbox_from_mqtt',
        MagicMock(side_effect=AssertionError('ORM should not run in callback')),
    )

    msg = MagicMock()
    msg.topic = f'{client.topic_prefix}/351912345678/sms/inbox'
    msg.payload = b'{"message_id":"m1","sender":"+351900","body":"hi"}'
    client._on_message(client.client, None, msg)
    assert enqueued == ['sms_inbox']
