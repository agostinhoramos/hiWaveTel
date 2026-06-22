"""Tests for modem REST API."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from django.urls import reverse

from apps.sms.models import ModemDevice


@pytest.mark.django_db
def test_modem_list_empty(api_client):
    resp = api_client.get(reverse('sms-modem-list'))
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.django_db
def test_modem_list_returns_summaries(api_client):
    ModemDevice.objects.create(modem_index=0, dbus_path='/org/freedesktop/ModemManager1/Modem/0')
    summary = {
        'modem_index': 0,
        'enabled': True,
        'is_present': True,
        'dbus_path': '/org/freedesktop/ModemManager1/Modem/0',
        'phone_number': '+351900000001',
        'manufacturer': 'vendor',
        'model': 'model-x',
        'state': 'registered',
        'available': True,
        'first_detected_at': '2026-01-01T00:00:00+00:00',
        'last_detected_at': '2026-01-01T00:00:00+00:00',
    }
    with patch('apps.sms.views_modems.build_modem_summary', return_value=summary):
        resp = api_client.get(reverse('sms-modem-list'))
    assert resp.status_code == 200
    assert resp.json() == [summary]


@pytest.mark.django_db
def test_modem_detail_404_when_never_detected(api_client):
    resp = api_client.get(reverse('sms-modem-detail', kwargs={'modem_index': 0}))
    assert resp.status_code == 404


@pytest.mark.django_db
def test_modem_detail_and_put_enabled(api_client):
    ModemDevice.objects.create(modem_index=0, enabled=True)
    detail = {
        'modem_index': 0,
        'enabled': True,
        'is_present': True,
        'dbus_path': '/org/freedesktop/ModemManager1/Modem/0',
        'phone_number': '+351900000001',
        'manufacturer': 'vendor',
        'model': 'model-x',
        'imei': '123',
        'firmware': '1.0',
        'sim_path': '/org/freedesktop/ModemManager1/SIM/0',
        'state': 'registered',
        'available': True,
        'checked_at': '2026-01-01T00:00:00+00:00',
        'enumerated_indices': [0],
        'ping_ok': True,
        'detail': 'ok',
        'last_activity': {'at': None, 'source': None},
        'first_detected_at': '2026-01-01T00:00:00+00:00',
        'last_detected_at': '2026-01-01T00:00:00+00:00',
    }
    disabled_detail = {**detail, 'enabled': False}

    with patch('apps.sms.views_modems.get_modem_detail', side_effect=[detail, disabled_detail]):
        get_resp = api_client.get(reverse('sms-modem-detail', kwargs={'modem_index': 0}))
        assert get_resp.status_code == 200
        assert get_resp.json()['enabled'] is True

        put_resp = api_client.put(
            reverse('sms-modem-detail', kwargs={'modem_index': 0}),
            {'enabled': False},
            format='json',
        )
    assert put_resp.status_code == 200
    assert put_resp.json()['enabled'] is False
    assert ModemDevice.objects.get(modem_index=0).enabled is False


@pytest.mark.django_db
def test_modem_sync(api_client):
    device = ModemDevice(modem_index=0, dbus_path='/org/freedesktop/ModemManager1/Modem/0')
    summary = {
        'modem_index': 0,
        'enabled': True,
        'is_present': True,
        'dbus_path': device.dbus_path,
        'phone_number': '',
        'manufacturer': '',
        'model': '',
        'state': 'enabled',
        'available': True,
        'first_detected_at': '2026-01-01T00:00:00+00:00',
        'last_detected_at': '2026-01-01T00:00:00+00:00',
    }
    with patch('apps.sms.views_modems.sync_detected_modems', return_value=[device]):
        with patch('apps.sms.views_modems.build_modem_summary', return_value=summary):
            resp = api_client.post(reverse('sms-modem-sync'))
    assert resp.status_code == 200
    assert resp.json() == [summary]
