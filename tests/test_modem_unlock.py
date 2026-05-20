"""Tests for SIM PIN unlock helpers."""

from __future__ import annotations

from subprocess import CompletedProcess
from unittest.mock import patch

from apps.sms.modem_ready import modem_overview_needs_sim_unlock, try_unlock_sim_pin


def test_modem_overview_needs_sim_unlock_detects_locked():
    overview = CompletedProcess(
        ['mmcli', '-m', '0'],
        0,
        stdout='Status   |             state: locked\n           |              lock: sim-pin\n',
        stderr='',
    )
    with patch('apps.sms.modem_ready.subprocess.run', return_value=overview):
        assert modem_overview_needs_sim_unlock(0) is True


def test_modem_overview_needs_sim_unlock_false_when_enabled():
    overview = CompletedProcess(
        ['mmcli', '-m', '0'],
        0,
        stdout='Status   |             state: registered\n',
        stderr='',
    )
    with patch('apps.sms.modem_ready.subprocess.run', return_value=overview):
        assert modem_overview_needs_sim_unlock(0) is False


def test_try_unlock_sim_pin_uses_modem_fallback():
    overview = CompletedProcess(
        ['mmcli', '-m', '0'],
        0,
        stdout='SIM      |  primary sim path: /org/freedesktop/ModemManager1/SIM/0\n',
        stderr='',
    )
    sim_fail = CompletedProcess(['mmcli', '-i', 'x', '--pin', '1234'], 1, stdout='', stderr='fail')
    modem_ok = CompletedProcess(['mmcli', '-m', '0', '--pin', '1234'], 0, stdout='ok', stderr='')

    with patch('apps.sms.modem_ready.subprocess.run', side_effect=[overview, sim_fail, modem_ok]):
        assert try_unlock_sim_pin(0, pin='1234') is True
