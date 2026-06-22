"""Tests for webhook REST API."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from django.urls import reverse

from apps.sms.models import InboundWebhook


@pytest.mark.django_db
def test_webhook_list(api_client):
    InboundWebhook.objects.create(
        modem_index=0,
        name='app',
        url='http://example.test/hook',
        enabled=True,
    )
    resp = api_client.get(reverse('sms-webhook-list'))
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]['modem_index'] == 0
    assert data[0]['url'] == 'http://example.test/hook'


@pytest.mark.django_db
def test_create_webhook_for_enumerated_modem(api_client):
    with patch('apps.sms.views_webhooks.assert_modem_enumerated'):
        resp = api_client.post(
            reverse('sms-modem-webhook-create', kwargs={'modem_index': 0}),
            {'name': 'app', 'url': 'http://hook.test/inbound'},
            format='json',
        )
    assert resp.status_code == 201
    assert resp.json()['modem_index'] == 0
    assert InboundWebhook.objects.filter(modem_index=0).count() == 1


@pytest.mark.django_db
def test_create_webhook_404_when_modem_not_enumerated(api_client):
    from apps.sms.modem_registry import ModemNotEnumeratedError

    with patch(
        'apps.sms.views_webhooks.assert_modem_enumerated',
        side_effect=ModemNotEnumeratedError('Modem index 2 not enumerated'),
    ):
        resp = api_client.post(
            reverse('sms-modem-webhook-create', kwargs={'modem_index': 2}),
            {'name': 'app', 'url': 'http://hook.test/inbound'},
            format='json',
        )
    assert resp.status_code == 404
