"""Outbound/inbound REST API behaviour (authenticated + anonymous)."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.sms.mmcli_client import MMCLIClient, MmcliError
from apps.sms.models import InboundSms, OutboundSms

pytestmark = pytest.mark.django_db


def test_sms_requires_auth_when_anonymous(api_client):
    assert api_client.get(reverse('sms-inbound-list')).status_code == 401
    assert api_client.get(reverse('sms-outbound-list')).status_code == 401


@patch.object(MMCLIClient, 'send_sms', autospec=True)
@patch.object(MMCLIClient, 'create_sms', autospec=True)
@patch.object(MMCLIClient, 'ensure_modem_index', autospec=True)
def test_outbound_create_send_success(mock_ensure, mock_create, mock_send, auth_client):
    mock_create.return_value = '/org/freedesktop/ModemManager1/SMS/0'
    payload = {'to': '+351913000387', 'text': 'Test EC25', 'modem_index': 0}
    resp = auth_client.post(reverse('sms-outbound-list'), payload, format='json')

    assert resp.status_code == 202, resp.content
    assert resp.data['state'] == OutboundSms.State.SENT
    assert resp.data['mm_path'] == '/org/freedesktop/ModemManager1/SMS/0'
    assert OutboundSms.objects.count() == 1
    mock_create.assert_called_once()
    mock_send.assert_called_once()
    mock_ensure.assert_called_once()


@patch.object(MMCLIClient, 'send_sms', autospec=True)
@patch.object(MMCLIClient, 'create_sms', autospec=True)
@patch.object(MMCLIClient, 'ensure_modem_index', autospec=True)
def test_outbound_create_fails_records_failed_state(mock_ensure, mock_create, mock_send, auth_client):
    mock_create.side_effect = MmcliError('modem busy', stderr='EBUSY', exit_code=1)
    resp = auth_client.post(
        reverse('sms-outbound-list'),
        {'to': '+4412345678910', 'text': 'x'},
        format='json',
    )
    assert resp.status_code == 202
    assert resp.data['state'] == OutboundSms.State.FAILED
    mock_send.assert_not_called()


def test_outbound_create_rejects_short_destination_number(auth_client):
    resp = auth_client.post(
        reverse('sms-outbound-list'),
        {'to': '+351913', 'text': 'hello'},
        format='json',
    )
    assert resp.status_code == 400


def test_inbound_list_filter_from(auth_client, two_inbounds):
    a = two_inbounds
    url = reverse('sms-inbound-list')
    resp = auth_client.get(url, {'from': '913'})
    assert resp.status_code == 200
    ids = {row['id'] for row in resp.data['results']}
    assert ids == {a.id}


def test_inbound_since_invalid(auth_client):
    resp = auth_client.get(reverse('sms-inbound-list'), {'since': 'not-valid'})
    assert resp.status_code == 400


def test_inbound_since_valid(auth_client):
    iso = timezone.now().isoformat()
    resp = auth_client.get(reverse('sms-inbound-list'), {'since': iso})
    assert resp.status_code == 200
    ids = {row['id'] for row in resp.data['results']}
    assert ids == set()


def test_inbound_from_param_length_guard(auth_client):
    resp = auth_client.get(reverse('sms-inbound-list'), {'from': 'x' * 300})
    assert resp.status_code == 400


def test_inbound_pagination_next_link_when_over_page_size(auth_client, two_inbounds):
    bulk = [
        InboundSms(
            mm_path=f'/org/freedesktop/ModemManager1/SMS/bulk/{i}',
            modem_index=0,
            from_number='+4412345678910',
            text='x',
        )
        for i in range(9000, 9051)
    ]
    InboundSms.objects.bulk_create(bulk)
    resp = auth_client.get(reverse('sms-inbound-list'))
    assert resp.status_code == 200
    assert resp.data.get('next') is not None
    assert len(resp.data['results']) == 50


def test_retrieve_inbound_detail(auth_client, two_inbounds):
    a = two_inbounds
    resp = auth_client.get(reverse('sms-inbound-detail', args=[a.pk]))
    assert resp.status_code == 200
    assert resp.data['from_number'] == '+351913000387'


@patch.object(MMCLIClient, 'send_sms', autospec=True)
@patch.object(MMCLIClient, 'create_sms', autospec=True)
@patch.object(MMCLIClient, 'ensure_modem_index', autospec=True)
def test_outbound_ensure_failure_marks_failed(mock_ensure, mock_create, mock_send, auth_client):
    mock_ensure.side_effect = MmcliError(
        'Modem missing',
        stderr='modem index missing',
        exit_code=-2,
    )
    resp = auth_client.post(
        reverse('sms-outbound-list'),
        {'to': '+4412345678910', 'text': 'x'},
        format='json',
    )
    assert resp.status_code == 202
    assert resp.data['state'] == OutboundSms.State.FAILED
    mock_create.assert_not_called()
    mock_send.assert_not_called()
