"""GET /api/health/ ModemManager probes."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from django.test import override_settings
from django.urls import reverse

from apps.sms.mmcli_client import MMCLIClient, MmcliError

pytestmark = pytest.mark.django_db


def test_health_modem_ok(api_client):
    with patch.object(MMCLIClient, 'modem_ping') as mp, patch.object(MMCLIClient, 'list_modem_indices') as ml:
        ml.return_value = [0]
        mp.return_value = (True, '')
        resp = api_client.get(reverse('api-health-mm'))
    assert resp.status_code == 200
    assert resp.json().get('ok') is True


def test_health_modem_index_mismatch(api_client):
    with override_settings(MODEM_MMCLI_INDEX=99), patch.object(MMCLIClient, 'modem_ping'), patch.object(
        MMCLIClient,
        'list_modem_indices',
        return_value=[0],
    ):
        resp = api_client.get(reverse('api-health-mm'))
    assert resp.status_code == 503


def test_health_no_modems(api_client):
    with patch.object(MMCLIClient, 'list_modem_indices', return_value=[]):
        resp = api_client.get(reverse('api-health-mm'))
    assert resp.status_code == 503
    body = resp.json()
    assert body.get('ok') is False
    assert 'zero modems' in (body.get('mmcli_notes') or '').lower()


def test_health_ping_failure(api_client):
    with patch.object(MMCLIClient, 'list_modem_indices', return_value=[0]), patch.object(
        MMCLIClient,
        'modem_ping',
        return_value=(False, 'modem offline'),
    ), override_settings(MODEM_MMCLI_INDEX=0):
        resp = api_client.get(reverse('api-health-mm'))
    assert resp.status_code == 503


def test_health_mmcli_error(api_client):
    with patch.object(MMCLIClient, 'list_modem_indices', side_effect=MmcliError('mmcli exploded', stderr='boom')):
        resp = api_client.get(reverse('api-health-mm'))
    assert resp.status_code == 503


def test_health_os_error(api_client):
    with patch.object(MMCLIClient, 'list_modem_indices', side_effect=OSError('ENOENT')):
        resp = api_client.get(reverse('api-health-mm'))
    assert resp.status_code == 503


def test_health_unexpected_error_sanitized(api_client):
    with patch(
        'apps.sms.views_health.MMCLIClient',
        side_effect=RuntimeError('do not expose this exact string'),
    ):
        resp = api_client.get(reverse('api-health-mm'))
    assert resp.status_code == 503
    assert 'do not expose this exact string' not in resp.json().get('mmcli_notes', '')
