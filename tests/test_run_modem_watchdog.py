"""Management command tests for run_modem_watchdog."""

from __future__ import annotations

import signal
from io import StringIO
from unittest.mock import patch

import pytest
from django.core.management import call_command

from apps.sms.management.commands import run_modem_watchdog as cmd_mod
from apps.sms.modem_watchdog import ModemHealthReport, ModemWatchdogRunner


@pytest.mark.django_db
def test_run_modem_watchdog_once_ok():
    ok_report = ModemHealthReport(ok=True, configured_index=0, effective_index=0)
    out = StringIO()
    with patch.object(ModemWatchdogRunner, 'run_once', return_value=ok_report):
        call_command('run_modem_watchdog', '--once', stdout=out)
    assert 'Modem health OK' in out.getvalue()


@pytest.mark.django_db
def test_run_modem_watchdog_once_failed():
    bad_report = ModemHealthReport(ok=False, configured_index=0, issues=['down'])
    out = StringIO()
    with patch.object(ModemWatchdogRunner, 'run_once', return_value=bad_report):
        call_command('run_modem_watchdog', '--once', stdout=out)
    assert 'Modem health FAILED' in out.getvalue()


@pytest.mark.django_db
def test_run_modem_watchdog_sigterm_stops_runner(monkeypatch):
    runner = ModemWatchdogRunner()
    captured: dict[str, object] = {}

    def fake_run_forever() -> None:
        captured['handler'] = signal.getsignal(signal.SIGTERM)
        signal.raise_signal(signal.SIGTERM)

    monkeypatch.setattr(cmd_mod, 'ModemWatchdogRunner', lambda: runner)
    with patch.object(runner, 'run_forever', side_effect=fake_run_forever):
        cmd_mod.Command().handle(once=False)
    assert runner._stop is True
