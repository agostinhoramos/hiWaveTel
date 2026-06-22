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


@pytest.mark.django_db
def test_update_webhook(api_client):
    webhook = InboundWebhook.objects.create(
        modem_index=0,
        name='old',
        url='http://old.test/hook',
        enabled=True,
    )
    resp = api_client.put(
        reverse('sms-modem-webhook-detail', kwargs={'modem_index': 0, 'webhook_id': webhook.pk}),
        {'name': 'new', 'url': 'http://new.test/hook', 'enabled': False},
        format='json',
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data['name'] == 'new'
    assert data['url'] == 'http://new.test/hook'
    assert data['enabled'] is False

    webhook.refresh_from_db()
    assert webhook.name == 'new'
    assert webhook.enabled is False


@pytest.mark.django_db
def test_patch_webhook_partial(api_client):
    webhook = InboundWebhook.objects.create(
        modem_index=0,
        name='app',
        url='http://hook.test/inbound',
        enabled=True,
    )
    resp = api_client.patch(
        reverse('sms-modem-webhook-detail', kwargs={'modem_index': 0, 'webhook_id': webhook.pk}),
        {'enabled': False},
        format='json',
    )
    assert resp.status_code == 200
    assert resp.json()['enabled'] is False
    webhook.refresh_from_db()
    assert webhook.enabled is False
    assert webhook.url == 'http://hook.test/inbound'


@pytest.mark.django_db
def test_update_webhook_wrong_modem_404(api_client):
    webhook = InboundWebhook.objects.create(
        modem_index=0,
        name='app',
        url='http://hook.test/inbound',
        enabled=True,
    )
    resp = api_client.put(
        reverse('sms-modem-webhook-detail', kwargs={'modem_index': 1, 'webhook_id': webhook.pk}),
        {'name': 'x', 'url': 'http://x.test/h', 'enabled': True},
        format='json',
    )
    assert resp.status_code == 404


@pytest.mark.django_db
def test_delete_webhook(api_client):
    webhook = InboundWebhook.objects.create(
        modem_index=0,
        name='app',
        url='http://hook.test/inbound',
        enabled=True,
    )
    webhook_id = webhook.pk
    resp = api_client.delete(
        reverse('sms-modem-webhook-detail', kwargs={'modem_index': 0, 'webhook_id': webhook_id}),
    )
    assert resp.status_code == 204
    assert not InboundWebhook.objects.filter(pk=webhook_id).exists()


@pytest.mark.django_db
def test_delete_webhook_wrong_modem_404(api_client):
    webhook = InboundWebhook.objects.create(
        modem_index=0,
        name='app',
        url='http://hook.test/inbound',
        enabled=True,
    )
    resp = api_client.delete(
        reverse('sms-modem-webhook-detail', kwargs={'modem_index': 1, 'webhook_id': webhook.pk}),
    )
    assert resp.status_code == 404
    assert InboundWebhook.objects.filter(pk=webhook.pk).exists()
