"""Tests for modem enable / wait helpers."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from apps.sms import dbus_watch
from apps.sms.modem_ready import (
    get_modem_state,
    parse_modem_state,
    prepare_modem_for_outbound_sms,
    try_enable_modem,
    wait_modem_ready_for_sms,
)


def test_parse_modem_state_from_mmcli_output():
    assert parse_modem_state('modem.generic.state:\tenabled\n') == 'enabled'
    assert parse_modem_state('', 'error: state: disabled') == 'disabled'


def test_try_enable_modem_disabled_state_succeeds():
    state_result = SimpleNamespace(returncode=0, stdout='state: disabled', stderr='')
    enable_result = SimpleNamespace(returncode=0, stdout='successfully enabled', stderr='')

    with patch('apps.sms.modem_ready.get_modem_state', return_value='disabled'):
        with patch('apps.sms.modem_ready.sim_pin_lock_active', return_value=False):
            with patch('apps.sms.modem_ready.subprocess.run', return_value=enable_result) as mock_run:
                try_enable_modem(modem_index=0)

    mock_run.assert_called_once()
    assert mock_run.call_args[0][0] == ['mmcli', '-m', '0', '--enable']


def test_try_enable_modem_unknown_state_attempts_enable():
    enable_result = SimpleNamespace(returncode=0, stdout='successfully enabled', stderr='')

    with patch('apps.sms.modem_ready.get_modem_state', return_value='unknown'):
        with patch('apps.sms.modem_ready.sim_pin_lock_active', return_value=False):
            with patch('apps.sms.modem_ready.subprocess.run', return_value=enable_result) as mock_run:
                try_enable_modem(modem_index=0)

    mock_run.assert_called_once()


def test_try_enable_modem_not_disabled_skips_enable():
    with patch('apps.sms.modem_ready.get_modem_state', return_value='enabled'):
        with patch('apps.sms.modem_ready.subprocess.run') as mock_run:
            try_enable_modem(modem_index=0)

    mock_run.assert_not_called()


def test_wait_modem_ready_returns_true_when_enabled():
    with patch('apps.sms.modem_ready._resolve_working_modem_index', return_value=0):
        with patch('apps.sms.modem_ready.get_modem_state', return_value='enabled'):
            assert wait_modem_ready_for_sms(0, timeout_sec=1.0) is True


def test_dbus_watch_reexports_try_enable_modem():
    """dbus_watch._try_enable_modem remains available for the SMS watcher."""
    assert dbus_watch._try_enable_modem is try_enable_modem


@pytest.mark.django_db
def test_dispatch_calls_prepare_modem_when_no_injected_client():
    from unittest.mock import MagicMock

    from apps.sms.models import OutboundSms
    from apps.sms.services import dispatch_outbound_mmcli

    outbound = OutboundSms.objects.create(
        modem_index=0,
        to_number='+351900000001',
        text='hi',
        state=OutboundSms.State.CREATED,
    )
    dummy = MagicMock()
    dummy.mmcli_path = 'mmcli'
    dummy.ensure_modem_index = MagicMock()
    dummy.create_sms = MagicMock(return_value='/org/freedesktop/ModemManager1/SMS/1')
    dummy.send_sms = MagicMock()

    with patch('apps.sms.services.MMCLIClient', return_value=dummy):
        with patch('apps.sms.services.resolve_modem_mmcli_index', return_value=0):
            with patch('apps.sms.services.prepare_modem_for_outbound_sms') as mock_prep:
                dispatch_outbound_mmcli(outbound)

    mock_prep.assert_called_once_with(0, mmcli_path='mmcli')
    assert outbound.state == OutboundSms.State.SENT


def test_prepare_modem_enable_then_wait():
    with patch('apps.sms.modem_ready._resolve_working_modem_index', return_value=0):
        with patch('apps.sms.modem_ready.get_modem_state', return_value='enabled'):
            with patch('apps.sms.modem_ready.messaging_interface_ready', return_value=True):
                prepare_modem_for_outbound_sms(0)
