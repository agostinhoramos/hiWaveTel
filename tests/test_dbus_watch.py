"""D-Bus watcher helpers that do not need a running system bus."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
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
    mock_queue = MagicMock()
    mock_queue.enqueue.return_value = True

    with patch('apps.sms.dbus_watch.get_sms_queue', return_value=mock_queue):
        with patch('apps.sms.dbus_watch.time.sleep', return_value=None):
            n = dbus_watch.sync_modem_sms_snapshot(0, boom)
    assert n == 1
    mock_queue.enqueue.assert_called_once_with(paths_first[0], 0)


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


def test_on_added_enqueues_sms_when_received_true():
    """D-Bus calls _on_added(path, True) for a freshly received SMS.

    ModemManager Messaging.Added signal: Added(o path, b received).
    received=True means the SMS just arrived over-the-air.
    This is the normal case and MUST be processed, not dropped.
    """
    mock_queue = MagicMock()
    mock_queue.enqueue.return_value = True

    with patch('apps.sms.dbus_watch.get_sms_queue', return_value=mock_queue):
        callback = dbus_watch._make_on_added_callback(modem_index=0)
        # dbus-next passes (path, received=True) for incoming SMS
        callback('/org/freedesktop/ModemManager1/SMS/123', True)

    mock_queue.enqueue.assert_called_once_with('/org/freedesktop/ModemManager1/SMS/123', 0)


def test_on_added_enqueues_sms_when_received_false():
    """received=False means SMS loaded from storage (not just received).

    Still must be processed (e.g. startup snapshot path via D-Bus).
    """
    mock_queue = MagicMock()
    mock_queue.enqueue.return_value = True

    with patch('apps.sms.dbus_watch.get_sms_queue', return_value=mock_queue):
        callback = dbus_watch._make_on_added_callback(modem_index=2)
        callback('/org/freedesktop/ModemManager1/SMS/77', False)

    mock_queue.enqueue.assert_called_once_with('/org/freedesktop/ModemManager1/SMS/77', 2)


def test_on_added_fallback_when_queue_full():
    """When enqueue returns False (queue full), path goes to DLQ."""
    mock_queue = MagicMock()
    mock_queue.enqueue.return_value = False

    with patch('apps.sms.dbus_watch.get_sms_queue', return_value=mock_queue):
        with patch('apps.sms.dbus_watch.enqueue_persist_failure') as mock_dlq:
            callback = dbus_watch._make_on_added_callback(modem_index=1)
            callback('/org/freedesktop/ModemManager1/SMS/456', True)

    mock_queue.enqueue.assert_called_once()
    mock_dlq.assert_called_once_with(
        '/org/freedesktop/ModemManager1/SMS/456',
        1,
        'persist queue full',
    )


def test_on_added_does_not_raise_on_enqueue_exception():
    """Exceptions inside enqueue must not propagate out of the signal handler."""
    mock_queue = MagicMock()
    mock_queue.enqueue.side_effect = RuntimeError('Queue exploded')

    with patch('apps.sms.dbus_watch.get_sms_queue', return_value=mock_queue):
        callback = dbus_watch._make_on_added_callback(modem_index=0)
        # Must not raise
        callback('/org/freedesktop/ModemManager1/SMS/999', True)

    mock_queue.enqueue.assert_called_once()


def test_try_enable_modem_disabled_state_succeeds():
    """Should detect disabled modem and run --enable successfully."""
    state_result = SimpleNamespace(
        returncode=0,
        stdout='state: disabled',
        stderr=''
    )
    enable_result = SimpleNamespace(
        returncode=0,
        stdout='successfully enabled',
        stderr=''
    )
    
    with patch('apps.sms.modem_ready.get_modem_state', return_value='disabled'):
        with patch('apps.sms.modem_ready.sim_pin_lock_active', return_value=False):
            with patch('apps.sms.modem_ready.subprocess.run', return_value=enable_result) as mock_run:
                dbus_watch._try_enable_modem(modem_index=0)

    mock_run.assert_called_once()
    assert mock_run.call_args[0][0] == ['mmcli', '-m', '0', '--enable']


def test_try_enable_modem_disabled_state_enable_fails():
    """Should log warning when --enable fails but not raise exception."""
    state_result = SimpleNamespace(
        returncode=0,
        stdout='state: disabled',
        stderr=''
    )
    enable_result = SimpleNamespace(
        returncode=1,
        stdout='',
        stderr='enable failed: error'
    )
    
    with patch('apps.sms.modem_ready.get_modem_state', return_value='disabled'):
        with patch('apps.sms.modem_ready.sim_pin_lock_active', return_value=False):
            with patch('apps.sms.modem_ready.subprocess.run', return_value=enable_result) as mock_run:
                dbus_watch._try_enable_modem(modem_index=0)

    mock_run.assert_called_once()


def test_try_enable_modem_not_disabled():
    """Should skip enable when modem is not disabled."""
    state_result = SimpleNamespace(
        returncode=0,
        stdout='state: enabled',
        stderr=''
    )
    
    with patch('subprocess.run', return_value=state_result) as mock_run:
        dbus_watch._try_enable_modem(modem_index=0)
    
    # Only state check, no enable call
    assert mock_run.call_count == 1
    assert mock_run.call_args[0][0] == ['mmcli', '-m', '0']


def test_try_enable_modem_exception_does_not_raise():
    """Should catch and log exceptions without raising."""
    with patch('subprocess.run', side_effect=Exception('mmcli command failed')):
        # Should not raise exception
        dbus_watch._try_enable_modem(modem_index=0)


def test_try_enable_modem_custom_mmcli_path():
    """Should respect MMCLI_PATH environment variable."""
    state_result = SimpleNamespace(
        returncode=0,
        stdout='state: enabled',
        stderr=''
    )
    
    with patch('subprocess.run', return_value=state_result) as mock_run:
        with patch.dict('os.environ', {'MMCLI_PATH': '/custom/path/mmcli'}):
            dbus_watch._try_enable_modem(modem_index=3)
    
    assert mock_run.call_count == 1
    assert mock_run.call_args[0][0] == ['/custom/path/mmcli', '-m', '3']
