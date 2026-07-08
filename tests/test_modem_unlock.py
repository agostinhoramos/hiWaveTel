"""Verify SIM unlock only succeeds when lock clears."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from apps.sms.modem_ready import (
    _mmcli_command_ok,
    _pin_unlock_not_needed,
    sim_pin_lock_active,
    try_unlock_sim_pin,
)


def test_mmcli_command_ok_rejects_error_text_with_zero_rc():
    cp = SimpleNamespace(returncode=0, stdout='error: no SIM was specified', stderr='')
    assert _mmcli_command_ok(cp) is False


def test_pin_unlock_not_needed_detects_wrong_state():
    err = "GDBus.Error:org.freedesktop.ModemManager1.Error.Core.WrongState: Cannot send PIN: device is not SIM-PIN locked"
    assert _pin_unlock_not_needed(err) is True


def test_sim_pin_lock_active_uses_sim_status_not_generic_lock_lines():
    modem_overview = SimpleNamespace(
        returncode=0,
        stdout='state: disabled\n  SIM: /org/freedesktop/ModemManager1/SIM/0\n  locks: sim-pin',
        stderr='',
    )
    sim_unlocked = SimpleNamespace(
        returncode=0,
        stdout='SIM lock status: unknown\n',
        stderr='',
    )
    with patch('apps.sms.modem_ready.subprocess.run', side_effect=[modem_overview, sim_unlocked]):
        assert sim_pin_lock_active(0) is False


def test_sim_pin_lock_active_detects_modem_overview_lock_sim_pin():
    modem_overview = SimpleNamespace(
        returncode=0,
        stdout=(
            'state: locked\n'
            '  lock: sim-pin\n'
            '  SIM: /org/freedesktop/ModemManager1/SIM/0\n'
        ),
        stderr='',
    )
    sim_unlocked = SimpleNamespace(
        returncode=0,
        stdout='Properties | active: yes\n',
        stderr='',
    )
    with patch('apps.sms.modem_ready.subprocess.run', side_effect=[modem_overview, sim_unlocked]):
        assert sim_pin_lock_active(0) is True


def test_try_unlock_sim_pin_treats_not_pin_locked_as_success():
    not_needed = SimpleNamespace(
        returncode=1,
        stdout='',
        stderr="error: couldn't send PIN code to the SIM: device is not SIM-PIN locked",
    )
    with patch('apps.sms.modem_ready.get_modem_state', return_value='enabled'):
        with patch('apps.sms.modem_ready.sim_pin_lock_active', return_value=True):
            with patch('apps.sms.modem_ready.subprocess.run', side_effect=[not_needed, not_needed]):
                with patch('apps.sms.modem_ready.time.sleep'):
                    assert try_unlock_sim_pin(0, pin='1234') is True


def test_try_unlock_sim_pin_keeps_trying_when_modem_still_locked():
    not_needed = SimpleNamespace(
        returncode=1,
        stdout='',
        stderr="error: couldn't send PIN code to the SIM: device is not SIM-PIN locked",
    )
    unlocked = SimpleNamespace(returncode=0, stdout='state: enabled', stderr='')

    with patch('apps.sms.modem_ready.get_modem_state', side_effect=['locked', 'enabled']):
        with patch('apps.sms.modem_ready.sim_pin_lock_active', side_effect=[False, False]):
            with patch('apps.sms.modem_ready.subprocess.run', side_effect=[not_needed, not_needed, unlocked]):
                with patch('apps.sms.modem_ready.time.sleep'):
                    assert try_unlock_sim_pin(0, pin='1234') is True


def test_try_unlock_sim_pin_requires_lock_cleared():
    locked = SimpleNamespace(returncode=0, stdout='state: locked', stderr='')
    unlock_ok = SimpleNamespace(returncode=0, stdout='successfully sent PIN', stderr='')
    unlocked = SimpleNamespace(returncode=0, stdout='state: enabled', stderr='')

    with patch('apps.sms.modem_ready.subprocess.run', side_effect=[locked, unlock_ok, unlocked]):
        with patch('apps.sms.modem_ready.sim_pin_lock_active', side_effect=[True, False]):
            with patch('apps.sms.modem_ready.time.sleep'):
                assert try_unlock_sim_pin(0, pin='1234') is True
