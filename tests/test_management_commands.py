"""Management command wiring (mocked asyncio / lightweight listener coroutine)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from django.core.management import call_command

from apps.external_device.management.commands import run_mqtt_gateway as mqtt_cmd_mod
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


@pytest.mark.django_db
@patch('apps.external_device.management.commands.run_mqtt_gateway.GatewayMqttClient')
def test_run_mqtt_gateway_connects_and_loops(mock_mqtt_client_class):
    """Should create client, connect, and start loop_forever."""
    mock_client = MagicMock()
    mock_mqtt_client_class.return_value = mock_client
    
    # Mock loop_forever to return immediately to avoid blocking
    mock_client.loop_forever.return_value = None
    
    call_command('run_mqtt_gateway')

    mock_mqtt_client_class.assert_called_once_with(mqtt_config=None)
    mock_client.connect.assert_called_once()
    mock_client.loop_forever.assert_called_once()


@pytest.mark.django_db
@patch('apps.external_device.management.commands.run_mqtt_gateway.GatewayMqttClient')
def test_run_mqtt_gateway_keyboard_interrupt(mock_mqtt_client_class, capsys):
    """Should disconnect gracefully on KeyboardInterrupt."""
    mock_client = MagicMock()
    mock_mqtt_client_class.return_value = mock_client
    
    # Simulate KeyboardInterrupt during loop_forever
    mock_client.loop_forever.side_effect = KeyboardInterrupt
    
    mqtt_cmd_mod.Command().handle()
    
    mock_client.connect.assert_called_once()
    mock_client.loop_forever.assert_called_once()
    mock_client.disconnect.assert_called_once()
    
    out = capsys.readouterr().out.lower()
    assert 'shutting down' in out
