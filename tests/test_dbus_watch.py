"""D-Bus watcher helpers that do not need a running system bus."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from apps.sms import dbus_watch
from apps.sms.mmcli_client import MMCLIClient, MmcliError


def test_modem_object_path():
    assert dbus_watch.modem_object_path(0) == '/org/freedesktop/ModemManager1/Modem/0'
    assert dbus_watch.modem_object_path(7) == '/org/freedesktop/ModemManager1/Modem/7'


def test_startup_snapshot_retries_then_persists():
    paths_first = ['/org/freedesktop/ModemManager1/SMS/1']
    boom = MMCLIClient()
    boom.list_sms_paths = MagicMock(side_effect=[MmcliError('boom', exit_code=1), paths_first])

    with patch.object(dbus_watch, 'persist_inbound_sms') as mock_persist:
        with patch('apps.sms.dbus_watch.time.sleep', return_value=None):
            n = dbus_watch.sync_modem_sms_snapshot(0, boom)
    assert n == 1
    assert mock_persist.call_count >= 1


def test_startup_snapshot_returns_zero_when_list_always_fails():
    bad = MMCLIClient()
    bad.list_sms_paths = MagicMock(side_effect=MmcliError('boom', exit_code=1))

    with patch('apps.sms.dbus_watch.time.sleep', return_value=None):
        n = dbus_watch.sync_modem_sms_snapshot(0, bad)
    assert n == 0


def test_persist_async_runs(monkeypatch):
    calls: list[tuple[str, int]] = []

    def fake_persist(mm_path: str, modem_index: int, client=None) -> None:
        calls.append((mm_path, modem_index))

    monkeypatch.setattr(dbus_watch, 'persist_inbound_sms', fake_persist)

    asyncio.run(dbus_watch._persist_async('/p', 9))
    assert calls == [('/p', 9)]
