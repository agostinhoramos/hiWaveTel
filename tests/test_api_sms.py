"""Outbound REST API behaviour (public, no authentication)."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from django.urls import reverse

from apps.sms.models import OutboundSms

pytestmark = pytest.mark.django_db


@patch('apps.sms.views.dispatch_outbound_mmcli')
def test_send_sms_success(mock_dispatch, api_client):
    def _dispatch(outbound, client=None):
        outbound.state = OutboundSms.State.SENT
        outbound.mm_path = '/org/freedesktop/ModemManager1/SMS/0'
        outbound.save(update_fields=('state', 'mm_path'))
        return outbound

    mock_dispatch.side_effect = _dispatch
    payload = {'to': '+351913000387', 'text': 'Test EC25', 'modem_index': 0}
    resp = api_client.post(reverse('sms-send'), payload, format='json')

    assert resp.status_code == 202, resp.content
    assert resp.data['state'] == OutboundSms.State.SENT
    assert resp.data['mm_path'] == '/org/freedesktop/ModemManager1/SMS/0'
    assert OutboundSms.objects.count() == 1
    mock_dispatch.assert_called_once()


@patch('apps.sms.views.dispatch_outbound_mmcli')
def test_send_sms_create_failure_records_failed_state(mock_dispatch, api_client):
    def _dispatch(outbound, client=None):
        outbound.state = OutboundSms.State.FAILED
        outbound.error_message = 'modem busy'
        outbound.save(update_fields=('state', 'error_message'))
        return outbound

    mock_dispatch.side_effect = _dispatch
    resp = api_client.post(
        reverse('sms-send'),
        {'to': '+351913000387', 'text': 'x'},
        format='json',
    )
    assert resp.status_code == 202
    assert resp.data['state'] == OutboundSms.State.FAILED


def test_send_sms_rejects_short_destination_number(api_client):
    resp = api_client.post(
        reverse('sms-send'),
        {'to': '+351913', 'text': 'hello'},
        format='json',
    )
    assert resp.status_code == 400


@patch('apps.sms.views.dispatch_outbound_mmcli')
def test_send_sms_ensure_failure_marks_failed(mock_dispatch, api_client):
    def _dispatch(outbound, client=None):
        outbound.state = OutboundSms.State.FAILED
        outbound.error_message = 'Modem missing'
        outbound.save(update_fields=('state', 'error_message'))
        return outbound

    mock_dispatch.side_effect = _dispatch
    resp = api_client.post(
        reverse('sms-send'),
        {'to': '+351913000387', 'text': 'x'},
        format='json',
    )
    assert resp.status_code == 202
    assert resp.data['state'] == OutboundSms.State.FAILED


def test_send_sms_no_auth_required(api_client):
    with patch('apps.sms.views.dispatch_outbound_mmcli'):
        resp = api_client.post(
            reverse('sms-send'),
            {'to': '+351913000387', 'text': 'hello'},
            format='json',
        )
    assert resp.status_code == 202
