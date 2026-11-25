"""Tests for modem watchdog probe and escalation."""

from __future__ import annotations

from unittest.mock import MagicMock, mock_open, patch

import pytest

from apps.sms.mmcli_client import MmcliError
from apps.sms.modem_watchdog import (
    ModemHealthReport,
    ModemWatchdogRunner,
    attempt_soft_recovery,
    probe_modem_sms_health,
    restart_modem_manager,
)


@pytest.mark.django_db
def test_probe_detects_ghost_modem_jul22_scenario():
    """Configured modem missing; fallback registered but messaging fails."""
    client = MagicMock()
    client.mmcli_path = 'mmcli'
    with patch.object(client, 'list_modem_indices', return_value=[28]):
        with patch('apps.sms.modem_watchdog.resolve_modem_mmcli_index', return_value=28):
            with patch('apps.sms.modem_watchdog.get_modem_state', return_value='registered'):
                with patch.object(client, 'modem_ping', return_value=(True, '')):
                    with patch(
                        'apps.sms.modem_watchdog.messaging_interface_ready',
                        return_value=False,
                    ):
                        report = probe_modem_sms_health(0, client=client)

    assert report.ok is False
    assert report.configured_index == 0
    assert report.effective_index == 28
    assert report.ghost_modem is True
    assert report.index_mismatch is True
    assert any('messaging-list-sms' in issue for issue in report.issues)


@pytest.mark.django_db
def test_probe_ok_when_messaging_and_ping_ok():
    client = MagicMock()
    client.mmcli_path = 'mmcli'
    with patch.object(client, 'list_modem_indices', return_value=[0]):
        with patch('apps.sms.modem_watchdog.resolve_modem_mmcli_index', return_value=0):
            with patch('apps.sms.modem_watchdog.get_modem_state', return_value='registered'):
                with patch.object(client, 'modem_ping', return_value=(True, '')):
                    with patch(
                        'apps.sms.modem_watchdog.messaging_interface_ready',
                        return_value=True,
                    ):
                        report = probe_modem_sms_health(0, client=client)

    assert report.ok is True
    assert report.messaging_ok is True


@pytest.mark.django_db
def test_probe_no_modems():
    client = MagicMock()
    with patch.object(client, 'list_modem_indices', return_value=[]):
        report = probe_modem_sms_health(0, client=client)
    assert report.ok is False
    assert 'zero modems' in report.issues[0].lower()


@pytest.mark.django_db
def test_attempt_soft_recovery_delegates():
    with patch('apps.sms.modem_watchdog.try_rescan_modems') as rescan, patch(
        'apps.sms.modem_watchdog.sync_detected_modems',
    ), patch('apps.sms.modem_watchdog.MMCLIClient') as mm_cls, patch(
        'apps.sms.modem_watchdog.ensure_modem_ready_for_sms',
        return_value=True,
    ), patch('apps.sms.modem_watchdog.probe_modem_sms_health') as probe:
        mm_cls.return_value.list_modem_indices.return_value = [0]
        probe.return_value = ModemHealthReport(ok=True, configured_index=0, effective_index=0)
        assert attempt_soft_recovery(0) is True
        rescan.assert_called_once()


@pytest.mark.django_db
def test_restart_modem_manager_success():
    proc = MagicMock()
    proc.pid = 12345
    with patch('apps.sms.modem_watchdog.subprocess.run'), patch(
        'apps.sms.modem_watchdog.subprocess.Popen',
        return_value=proc,
    ), patch('apps.sms.modem_watchdog.time.sleep'), patch(
        'apps.sms.modem_watchdog.try_rescan_modems',
    ), patch('apps.sms.modem_watchdog.sync_detected_modems'), patch(
        'apps.sms.modem_watchdog.MMCLIClient',
    ) as mm_cls, patch(
        'apps.sms.modem_watchdog.ensure_modem_ready_for_sms',
        return_value=True,
    ), patch('apps.sms.modem_watchdog.probe_modem_sms_health') as probe:
        mm_cls.return_value.list_modem_indices.return_value = [0]
        probe.return_value = ModemHealthReport(ok=True, configured_index=0, effective_index=0)
        assert restart_modem_manager() is True


@pytest.mark.django_db
def test_watchdog_runner_resets_failures_on_ok():
    runner = ModemWatchdogRunner()
    runner._consecutive_failures = 2
    ok_report = ModemHealthReport(ok=True, configured_index=0, effective_index=0)
    with patch('apps.sms.modem_watchdog.probe_modem_sms_health', return_value=ok_report):
        report = runner.run_once()
    assert report.ok is True
    assert runner._consecutive_failures == 0


@pytest.mark.django_db
def test_watchdog_runner_soft_recovery_after_threshold():
    runner = ModemWatchdogRunner()
    bad = ModemHealthReport(ok=False, configured_index=0, issues=['messaging failed'])
    good = ModemHealthReport(ok=True, configured_index=0, effective_index=0)
    with patch('apps.sms.modem_watchdog.probe_modem_sms_health', side_effect=[bad, bad, bad, good]), patch(
        'apps.sms.modem_watchdog.attempt_soft_recovery',
        return_value=True,
    ) as soft:
        runner.run_once()
        runner.run_once()
        runner.run_once()
        soft.assert_called_once()


@pytest.mark.django_db
def test_watchdog_runner_container_restart_when_tiers_fail(settings):
    settings.MODEM_WATCHDOG_FAILURE_THRESHOLD = 1
    settings.MODEM_WATCHDOG_AUTO_MM_RESTART = True
    settings.MODEM_WATCHDOG_AUTO_CONTAINER_RESTART = True
    settings.HIWAVE_ALLOW_CONTAINER_RESTART_API = True

    runner = ModemWatchdogRunner()
    bad = ModemHealthReport(ok=False, configured_index=0, issues=['down'])
    with patch('apps.sms.modem_watchdog.probe_modem_sms_health', return_value=bad), patch(
        'apps.sms.modem_watchdog.attempt_soft_recovery',
        return_value=False,
    ), patch(
        'apps.sms.modem_watchdog.restart_modem_manager',
        return_value=False,
    ), patch(
        'apps.sms.modem_watchdog.schedule_container_restart',
    ) as restart:
        runner.run_once()
        restart.assert_called_once_with(requested_by='modem_watchdog')


@pytest.mark.django_db
def test_probe_mmcli_list_error():
    client = MagicMock()
    client.list_modem_indices.side_effect = MmcliError('list failed', stderr='boom', exit_code=1)
    report = probe_modem_sms_health(0, client=client)
    assert report.ok is False
    assert 'mmcli list failed' in report.issues[0]


@pytest.mark.django_db
def test_probe_resolve_error():
    client = MagicMock()
    client.list_modem_indices.return_value = [0]
    with patch(
        'apps.sms.modem_watchdog.resolve_modem_mmcli_index',
        side_effect=MmcliError('missing index', stderr='x', exit_code=1),
    ):
        report = probe_modem_sms_health(0, client=client)
    assert report.ok is False
    assert report.enumerated_indices == [0]
    assert 'missing index' in report.issues[0]


@pytest.mark.django_db
def test_probe_ping_failure():
    client = MagicMock()
    client.mmcli_path = 'mmcli'
    with patch.object(client, 'list_modem_indices', return_value=[0]):
        with patch('apps.sms.modem_watchdog.resolve_modem_mmcli_index', return_value=0):
            with patch('apps.sms.modem_watchdog.get_modem_state', return_value='registered'):
                with patch.object(client, 'modem_ping', return_value=(False, 'offline')):
                    with patch(
                        'apps.sms.modem_watchdog.messaging_interface_ready',
                        return_value=True,
                    ):
                        report = probe_modem_sms_health(0, client=client)
    assert report.ok is False
    assert report.ping_ok is False
    assert report.messaging_ok is True


@pytest.mark.django_db
def test_probe_to_dict_roundtrip():
    report = ModemHealthReport(
        ok=False,
        configured_index=0,
        effective_index=28,
        enumerated_indices=[28],
        modem_state='registered',
        ping_ok=True,
        messaging_ok=False,
        index_mismatch=True,
        ghost_modem=True,
        issues=['ghost'],
    )
    payload = report.to_dict()
    assert payload['configured_index'] == 0
    assert payload['effective_index'] == 28
    assert payload['ghost_modem'] is True
    assert payload['issues'] == ['ghost']


@pytest.mark.django_db
def test_soft_recovery_resets_failed_modem():
    with patch('apps.sms.modem_watchdog.try_rescan_modems'), patch(
        'apps.sms.modem_watchdog.sync_detected_modems',
    ), patch('apps.sms.modem_watchdog.MMCLIClient') as mm_cls, patch(
        'apps.sms.modem_watchdog.get_modem_state',
        return_value='failed',
    ), patch('apps.sms.modem_watchdog.try_reset_modem') as reset, patch(
        'apps.sms.modem_watchdog.ensure_modem_ready_for_sms',
        return_value=False,
    ), patch('apps.sms.modem_watchdog.probe_modem_sms_health') as probe:
        mm_cls.return_value.list_modem_indices.return_value = [0]
        probe.return_value = ModemHealthReport(ok=False, configured_index=0)
        attempt_soft_recovery(0)
    reset.assert_called_once_with(0)


@pytest.mark.django_db
def test_soft_recovery_false_when_probe_and_ensure_fail():
    with patch('apps.sms.modem_watchdog.try_rescan_modems'), patch(
        'apps.sms.modem_watchdog.sync_detected_modems',
    ), patch('apps.sms.modem_watchdog.MMCLIClient') as mm_cls, patch(
        'apps.sms.modem_watchdog.get_modem_state',
        return_value='disabled',
    ), patch(
        'apps.sms.modem_watchdog.ensure_modem_ready_for_sms',
        return_value=False,
    ), patch('apps.sms.modem_watchdog.probe_modem_sms_health') as probe:
        mm_cls.return_value.list_modem_indices.return_value = [0]
        probe.return_value = ModemHealthReport(ok=False, configured_index=0)
        assert attempt_soft_recovery(0) is False


@pytest.mark.django_db
def test_restart_modem_manager_popen_failure():
    with patch('apps.sms.modem_watchdog.subprocess.run'), patch(
        'apps.sms.modem_watchdog.time.sleep',
    ), patch('builtins.open', mock_open()), patch(
        'apps.sms.modem_watchdog.subprocess.Popen',
        side_effect=OSError('cannot start'),
    ):
        assert restart_modem_manager() is False


@pytest.mark.django_db
def test_restart_modem_manager_debug_flag():
    proc = MagicMock()
    proc.pid = 99
    with patch.dict('os.environ', {'MODEM_MANAGER_DEBUG': 'true'}), patch(
        'apps.sms.modem_watchdog.subprocess.run',
    ), patch('apps.sms.modem_watchdog.subprocess.Popen', return_value=proc) as popen, patch(
        'apps.sms.modem_watchdog.time.sleep',
    ), patch('apps.sms.modem_watchdog.try_rescan_modems'), patch(
        'apps.sms.modem_watchdog.sync_detected_modems',
    ), patch('apps.sms.modem_watchdog.MMCLIClient') as mm_cls, patch(
        'apps.sms.modem_watchdog.ensure_modem_ready_for_sms',
        return_value=True,
    ), patch('apps.sms.modem_watchdog.probe_modem_sms_health') as probe, patch(
        'builtins.open',
        mock_open(),
    ):
        mm_cls.return_value.list_modem_indices.return_value = [0]
        probe.return_value = ModemHealthReport(ok=True, configured_index=0, effective_index=0)
        assert restart_modem_manager() is True
    assert popen.call_args[0][0] == ['ModemManager', '--debug']


@pytest.mark.django_db
def test_runner_tier2_mm_restart_after_soft_fails(settings):
    settings.MODEM_WATCHDOG_FAILURE_THRESHOLD = 1
    settings.MODEM_WATCHDOG_AUTO_MM_RESTART = True

    runner = ModemWatchdogRunner()
    bad = ModemHealthReport(ok=False, configured_index=0, issues=['down'])
    good = ModemHealthReport(ok=True, configured_index=0, effective_index=0)
    with patch('apps.sms.modem_watchdog.probe_modem_sms_health', side_effect=[bad, good]), patch(
        'apps.sms.modem_watchdog.attempt_soft_recovery',
        return_value=False,
    ), patch(
        'apps.sms.modem_watchdog.restart_modem_manager',
        return_value=True,
    ) as mm_restart:
        report = runner.run_once()
    mm_restart.assert_called_once()
    assert report.ok is True
    assert runner._consecutive_failures == 0


@pytest.mark.django_db
def test_runner_soft_cooldown_blocks_repeat(settings):
    settings.MODEM_WATCHDOG_FAILURE_THRESHOLD = 1
    settings.MODEM_WATCHDOG_SOFT_COOLDOWN_SEC = 3600

    runner = ModemWatchdogRunner()
    bad = ModemHealthReport(ok=False, configured_index=0, issues=['down'])
    with patch('apps.sms.modem_watchdog.probe_modem_sms_health', return_value=bad), patch(
        'apps.sms.modem_watchdog.attempt_soft_recovery',
        return_value=False,
    ) as soft, patch(
        'apps.sms.modem_watchdog.restart_modem_manager',
        return_value=False,
    ), patch(
        'apps.sms.modem_watchdog.schedule_container_restart',
    ):
        runner.run_once()
        runner.run_once()
    assert soft.call_count == 1


@pytest.mark.django_db
def test_runner_skips_mm_when_auto_disabled(settings):
    settings.MODEM_WATCHDOG_FAILURE_THRESHOLD = 1
    settings.MODEM_WATCHDOG_AUTO_MM_RESTART = False
    settings.MODEM_WATCHDOG_AUTO_CONTAINER_RESTART = False

    runner = ModemWatchdogRunner()
    bad = ModemHealthReport(ok=False, configured_index=0, issues=['down'])
    with patch('apps.sms.modem_watchdog.probe_modem_sms_health', return_value=bad), patch(
        'apps.sms.modem_watchdog.attempt_soft_recovery',
        return_value=False,
    ), patch(
        'apps.sms.modem_watchdog.restart_modem_manager',
    ) as mm_restart, patch(
        'apps.sms.modem_watchdog.schedule_container_restart',
    ) as container:
        runner.run_once()
    mm_restart.assert_not_called()
    container.assert_not_called()


@pytest.mark.django_db
def test_runner_skips_container_when_auto_disabled(settings):
    settings.MODEM_WATCHDOG_FAILURE_THRESHOLD = 1
    settings.MODEM_WATCHDOG_AUTO_MM_RESTART = True
    settings.MODEM_WATCHDOG_AUTO_CONTAINER_RESTART = False

    runner = ModemWatchdogRunner()
    bad = ModemHealthReport(ok=False, configured_index=0, issues=['down'])
    with patch('apps.sms.modem_watchdog.probe_modem_sms_health', return_value=bad), patch(
        'apps.sms.modem_watchdog.attempt_soft_recovery',
        return_value=False,
    ), patch(
        'apps.sms.modem_watchdog.restart_modem_manager',
        return_value=False,
    ), patch(
        'apps.sms.modem_watchdog.schedule_container_restart',
    ) as container:
        runner.run_once()
    container.assert_not_called()
