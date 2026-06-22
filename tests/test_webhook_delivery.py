"""Tests for inbound SMS webhook delivery."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from django.test import override_settings

from apps.sms.models import InboundSms, InboundWebhook
from apps.sms.webhook_delivery import (
    build_inbound_webhook_payload,
    deliver_inbound_webhooks,
    get_active_webhook_urls,
)


@pytest.mark.django_db
def test_build_inbound_webhook_payload():
    inbound = InboundSms.objects.create(
        mm_path='/org/freedesktop/ModemManager1/SMS/wh1',
        modem_index=0,
        from_number='+351912345678',
        text='hello webhook',
        mm_state='received',
    )
    payload = build_inbound_webhook_payload(inbound)
    assert payload['id'] == inbound.pk
    assert payload['sender'] == '+351912345678'
    assert payload['body'] == 'hello webhook'
    assert payload['modem_index'] == 0
    assert payload['mm_state'] == 'received'
    assert payload['received_at']


@pytest.mark.django_db
def test_get_active_webhook_urls_filters_by_modem():
    InboundWebhook.objects.create(
        modem_index=0,
        name='m0',
        url='http://m0.test/hook',
        enabled=True,
    )
    InboundWebhook.objects.create(
        modem_index=1,
        name='m1',
        url='http://m1.test/hook',
        enabled=True,
    )
    InboundWebhook.objects.create(
        modem_index=0,
        name='disabled',
        url='http://off.test/hook',
        enabled=False,
    )
    assert get_active_webhook_urls(0) == ['http://m0.test/hook']
    assert get_active_webhook_urls(1) == ['http://m1.test/hook']


@pytest.mark.django_db
@override_settings(
    SMS_WEBHOOK_RETRY_MAX=3,
    SMS_WEBHOOK_RETRY_BASE_SEC=0.01,
)
def test_deliver_inbound_webhooks_success():
    InboundWebhook.objects.create(
        modem_index=0,
        name='hook',
        url='http://a.test/hook',
        enabled=True,
    )
    inbound = InboundSms.objects.create(
        mm_path='/org/freedesktop/ModemManager1/SMS/wh2',
        modem_index=0,
        from_number='+351900000001',
        text='payload',
        mm_state='received',
    )
    mock_resp = MagicMock()
    mock_resp.__enter__ = MagicMock(return_value=mock_resp)
    mock_resp.__exit__ = MagicMock(return_value=False)
    mock_resp.status = 200
    mock_resp.getcode = MagicMock(return_value=200)

    with patch('urllib.request.urlopen', return_value=mock_resp) as mock_open:
        assert deliver_inbound_webhooks(inbound) is True
    mock_open.assert_called_once()


@pytest.mark.django_db
@override_settings(
    SMS_WEBHOOK_RETRY_MAX=2,
    SMS_WEBHOOK_RETRY_BASE_SEC=0.01,
)
def test_deliver_inbound_webhooks_partial_failure():
    InboundWebhook.objects.create(
        modem_index=0,
        name='fail',
        url='http://fail.test/hook',
        enabled=True,
    )
    InboundWebhook.objects.create(
        modem_index=0,
        name='ok',
        url='http://ok.test/hook',
        enabled=True,
    )
    inbound = InboundSms.objects.create(
        mm_path='/org/freedesktop/ModemManager1/SMS/wh3',
        modem_index=0,
        from_number='+351900000002',
        text='retry',
        mm_state='received',
    )

    def fake_urlopen(req, timeout=0):
        url = req.full_url
        if 'fail.test' in url:
            raise OSError('connection refused')
        mock_resp = MagicMock()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.status = 204
        mock_resp.getcode = MagicMock(return_value=204)
        return mock_resp

    with patch('urllib.request.urlopen', side_effect=fake_urlopen):
        assert deliver_inbound_webhooks(inbound) is False


@pytest.mark.django_db
def test_deliver_inbound_webhooks_no_urls_is_noop():
    inbound = InboundSms.objects.create(
        mm_path='/org/freedesktop/ModemManager1/SMS/wh4',
        modem_index=0,
        from_number='+351900000003',
        text='noop',
        mm_state='received',
    )
    with patch('urllib.request.urlopen') as mock_open:
        assert deliver_inbound_webhooks(inbound) is True
    mock_open.assert_not_called()


@pytest.mark.django_db
def test_deliver_inbound_webhooks_does_not_use_other_modem_urls():
    InboundWebhook.objects.create(
        modem_index=1,
        name='other',
        url='http://other.test/hook',
        enabled=True,
    )
    inbound = InboundSms.objects.create(
        mm_path='/org/freedesktop/ModemManager1/SMS/wh5',
        modem_index=0,
        from_number='+351900000004',
        text='modem0',
        mm_state='received',
    )
    with patch('urllib.request.urlopen') as mock_open:
        assert deliver_inbound_webhooks(inbound) is True
    mock_open.assert_not_called()
