"""Management command wiring (mocked asyncio / lightweight listener coroutine)."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from django.core.management import call_command

from apps.sms.management.commands import run_sms_watcher as cmd_mod


def test_run_sms_watcher_keyboard_interrupt(capsys):
    """Exit cleanly after ``KeyboardInterrupt`` from ``asyncio.run``."""

    def abort_run(coroutine_main):
        coroutine_main.close()
        raise KeyboardInterrupt

    with patch.object(cmd_mod.asyncio, 'run', side_effect=abort_run):
        cmd_mod.Command().handle(modem_index=0, reconnect_after=5.0, skip_initial_sync=False)

    out = capsys.readouterr().out.lower()
    assert 'interrupt' in out


@pytest.mark.parametrize(('skip_via_cli_flag', 'expected_snapshot'), [(True, False), (False, True)])
def test_run_command_maps_skip_sync_to_listener_snapshot(monkeypatch, skip_via_cli_flag, expected_snapshot):
    """``asyncio.run`` completes instantly using a patched listener coroutine."""

    captured: dict[str, object] = {}

    async def fake_listener(modem_index: int, reconnect_after: float, *, initial_snapshot: bool = False) -> None:
        captured['modem_index'] = modem_index
        captured['reconnect_after'] = reconnect_after
        captured['initial_snapshot'] = initial_snapshot

    monkeypatch.setattr(cmd_mod, 'run_modem_added_listener', fake_listener)

    kwargs = {'skip_initial_sync': True} if skip_via_cli_flag else {}
    call_command('run_sms_watcher', modem_index=8, reconnect_after=3.125, **kwargs)

    assert captured['modem_index'] == 8
    assert captured['reconnect_after'] == 3.125
    assert captured['initial_snapshot'] is expected_snapshot
