"""Tests for modem_readiness core and modem availability API."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from django.urls import reverse

from apps.sms.mmcli_client import MMCLIClient, MmcliError
from apps.sms.modem_readiness import (
    ReadinessIssue,
    ReadinessSnapshot,
    evaluate_sms_readiness,
    refresh_and_persist_readiness,
)
from apps.sms.models import OutboundSms

pytestmark = pytest.mark.django_db


def _ready_snapshot(**overrides) -> ReadinessSnapshot:
    base = {
        'ready': True,
        'phone_number': '+351961343706',
        'modem_index': 0,
        'modem_state': 'enabled',
        'checked_at': '2026-06-22T08:00:00+00:00',
        'capabilities': {'inbound_sms': True, 'outbound_sms': True},
        'issues': [],
        'components': {},
    }
    base.update(overrides)
    return ReadinessSnapshot(**base)


def _modem_availability(**overrides):
    from apps.sms.modem_readiness import ModemAvailability

    base = {
        'modem_index': 0,
        'available': True,
        'state': 'registered',
        'checked_at': '2026-06-22T08:00:00+00:00',
        'enumerated_indices': [0],
        'ping_ok': True,
        'phone_number': '+351961343706',
        'detail': 'Modem responsive (state=registered, ping ok).',
        'last_activity': {
            'at': None,
            'source': None,
            'inbound_sms_at': None,
            'outbound_sms_at': None,
            'device_last_seen_at': None,
            'readiness_checked_at': None,
        },
    }
    base.update(overrides)
    return ModemAvailability(**base)


@patch('apps.sms.modem_readiness.probe_modem_identity')
@patch('apps.sms.modem_readiness.modem_overview_needs_sim_unlock', return_value=False)
@patch('apps.sms.modem_readiness.get_modem_state', return_value='enabled')
@patch('apps.sms.modem_readiness.resolve_modem_mmcli_index', return_value=0)
@patch.object(MMCLIClient, 'modem_ping', return_value=(True, ''))
@patch.object(MMCLIClient, 'list_modem_indices', return_value=[0])
def test_evaluate_sms_readiness_ready(
    mock_list,
    mock_ping,
    mock_resolve,
    mock_state,
    mock_sim,
    mock_probe,
):
    mock_probe.return_value = {'phone_number': '+351961343706'}
    snap = evaluate_sms_readiness(0)
    assert snap.ready is True
    assert snap.phone_number == '+351961343706'
    assert snap.capabilities['outbound_sms'] is True
    assert snap.capabilities['inbound_sms'] is True
    assert snap.issues == []


@patch('apps.sms.modem_readiness.probe_modem_identity')
@patch('apps.sms.modem_readiness.modem_overview_needs_sim_unlock', return_value=False)
@patch('apps.sms.modem_readiness.get_modem_state', return_value='disabled')
@patch('apps.sms.modem_readiness.resolve_modem_mmcli_index', return_value=0)
@patch.object(MMCLIClient, 'modem_ping', return_value=(True, ''))
@patch.object(MMCLIClient, 'list_modem_indices', return_value=[0])
def test_evaluate_sms_readiness_modem_not_ready(
    mock_list,
    mock_ping,
    mock_resolve,
    mock_state,
    mock_sim,
    mock_probe,
):
    mock_probe.return_value = {'phone_number': '+351961343706'}
    snap = evaluate_sms_readiness(0)
    assert snap.ready is False
    assert any(i.code == 'modem_not_ready' for i in snap.issues)


@patch('apps.sms.modem_readiness.probe_modem_identity')
@patch('apps.sms.modem_readiness.modem_overview_needs_sim_unlock', return_value=True)
@patch('apps.sms.modem_readiness.get_modem_state', return_value='enabled')
@patch('apps.sms.modem_readiness.resolve_modem_mmcli_index', return_value=0)
@patch.object(MMCLIClient, 'modem_ping', return_value=(True, ''))
@patch.object(MMCLIClient, 'list_modem_indices', return_value=[0])
def test_evaluate_sms_readiness_sim_locked(
    mock_list,
    mock_ping,
    mock_resolve,
    mock_state,
    mock_sim,
    mock_probe,
):
    mock_probe.return_value = {'phone_number': '+351961343706'}
    snap = evaluate_sms_readiness(0)
    assert snap.ready is False
    assert any(i.code == 'sim_pin_locked' for i in snap.issues)


@patch('apps.sms.modem_readiness.evaluate_sms_readiness')
def test_refresh_and_persist_readiness_returns_snapshot(mock_evaluate):
    mock_evaluate.return_value = _ready_snapshot()
    snap = refresh_and_persist_readiness(0)
    assert snap.ready is True
    assert snap.phone_number == '+351961343706'


@patch('apps.sms.modem_readiness.get_modem_state', return_value='registered')
@patch.object(MMCLIClient, 'modem_ping', return_value=(True, ''))
@patch.object(MMCLIClient, 'list_modem_indices', return_value=[0])
def test_check_modem_availability_ok(mock_list, mock_ping, mock_state):
    from apps.sms.modem_readiness import check_modem_availability

    result = check_modem_availability(0)
    assert result.available is True
    assert result.modem_index == 0
    assert result.state == 'registered'
    assert result.enumerated_indices == [0]
    assert result.ping_ok is True
    assert result.detail
    assert 'last_activity' in result.to_dict()


@patch('apps.sms.modem_readiness.get_modem_state', return_value='disabled')
@patch.object(MMCLIClient, 'modem_ping', return_value=(True, ''))
@patch.object(MMCLIClient, 'list_modem_indices', return_value=[0])
def test_check_modem_availability_not_ready(mock_list, mock_ping, mock_state):
    from apps.sms.modem_readiness import check_modem_availability

    result = check_modem_availability(0)
    assert result.available is False
    assert result.state == 'disabled'
    assert 'disabled' in result.detail


@patch.object(MMCLIClient, 'list_modem_indices', return_value=[0, 1])
def test_check_modem_availability_missing(mock_list):
    from apps.sms.modem_readiness import check_modem_availability

    result = check_modem_availability(3)
    assert result.available is False
    assert result.state == 'missing'
    assert result.enumerated_indices == [0, 1]
    assert 'not reported' in result.detail


@patch('apps.sms.modem_readiness.probe_modem_identity', return_value={'phone_number': '+351961343706'})
@patch('apps.sms.modem_readiness.get_modem_state', return_value='registered')
@patch.object(MMCLIClient, 'modem_ping', return_value=(True, ''))
@patch.object(MMCLIClient, 'list_modem_indices', return_value=[0])
def test_check_modem_availability_last_activity_inbound(
    mock_list,
    mock_ping,
    mock_state,
    mock_probe,
):
    from apps.sms.models import InboundSms
    from apps.sms.modem_readiness import check_modem_availability

    InboundSms.objects.create(
        mm_path='/org/freedesktop/ModemManager1/SMS/99',
        modem_index=0,
        from_number='+351913000387',
        text='ping',
    )
    result = check_modem_availability(0)
    assert result.last_activity['source'] == 'inbound_sms'
    assert result.last_activity['inbound_sms_at'] is not None
    assert result.last_activity['at'] == result.last_activity['inbound_sms_at']


@patch('apps.sms.views_system.check_modem_availability')
def test_modem_availability_api_ok(mock_check, api_client):
    mock_check.return_value = _modem_availability()
    resp = api_client.get(reverse('sms-modem-availability', kwargs={'modem_index': 0}))
    assert resp.status_code == 200
    assert resp.data['available'] is True
    assert resp.data['enumerated_indices'] == [0]
    assert resp.data['last_activity']['source'] is None


@patch('apps.sms.views_system.check_modem_availability')
def test_modem_availability_api_unavailable(mock_check, api_client):
    mock_check.return_value = _modem_availability(
        modem_index=1,
        available=False,
        state='missing',
        enumerated_indices=[0],
        ping_ok=None,
        phone_number='',
        detail='Modem index 1 not reported by ModemManager (mmcli -L returned: 0).',
    )
    resp = api_client.get(reverse('sms-modem-availability', kwargs={'modem_index': 1}))
    assert resp.status_code == 503
    assert resp.data['available'] is False
    assert resp.data['enumerated_indices'] == [0]


@patch('apps.sms.views_system.check_modem_availability')
def test_modem_availability_api_anonymous(mock_check, api_client):
    mock_check.return_value = _modem_availability()
    resp = api_client.get(reverse('sms-modem-availability', kwargs={'modem_index': 0}))
    assert resp.status_code == 200
    assert resp.data['available'] is True
    assert 'last_activity' in resp.data


@patch('apps.sms.modem_readiness.refresh_readiness_safe')
@patch('apps.sms.services.prepare_modem_for_outbound_sms')
@patch.object(MMCLIClient, 'create_sms', side_effect=MmcliError('fail', stderr='modem busy'))
def test_dispatch_outbound_failure_refreshes_readiness(
    mock_create,
    mock_prepare,
    mock_refresh,
):
    from apps.sms.services import dispatch_outbound_mmcli

    outbound = OutboundSms.objects.create(
        modem_index=0,
        to_number='+351913000387',
        text='hi',
        state=OutboundSms.State.CREATED,
    )
    dispatch_outbound_mmcli(outbound, client=MMCLIClient())
    mock_refresh.assert_called_once()
    extra = mock_refresh.call_args.kwargs['extra_issues']
    assert any(i.code == 'outbound_failed_recently' for i in extra)
