"""Detect sustained modem/SMS failures and escalate recovery (soft → MM restart → container)."""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import time
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any

from django.conf import settings

from apps.sms.container_restart import schedule_container_restart
from apps.sms.mmcli_client import MMCLIClient, MmcliError, resolve_modem_mmcli_index
from apps.sms.modem_ready import (
    _READY_STATES,
    ensure_modem_ready_for_sms,
    get_modem_state,
    messaging_interface_ready,
    try_rescan_modems,
    try_reset_modem,
)
from apps.sms.modem_registry import sync_detected_modems

_LOGGER = logging.getLogger(__name__)


@dataclass
class ModemHealthReport:
    """Outcome of a single modem SMS health probe."""

    ok: bool
    configured_index: int
    effective_index: int | None = None
    enumerated_indices: list[int] = field(default_factory=list)
    modem_state: str = 'unknown'
    ping_ok: bool = False
    messaging_ok: bool = False
    index_mismatch: bool = False
    ghost_modem: bool = False
    issues: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            'ok': self.ok,
            'configured_index': self.configured_index,
            'effective_index': self.effective_index,
            'enumerated_indices': list(self.enumerated_indices),
            'modem_state': self.modem_state,
            'ping_ok': self.ping_ok,
            'messaging_ok': self.messaging_ok,
            'index_mismatch': self.index_mismatch,
            'ghost_modem': self.ghost_modem,
            'issues': list(self.issues),
        }


def probe_modem_sms_health(
    configured_index: int | None = None,
    *,
    client: MMCLIClient | None = None,
) -> ModemHealthReport:
    """Probe mmcli enumeration, ping, and SMS messaging readiness."""
    configured = int(
        configured_index if configured_index is not None else getattr(settings, 'MODEM_MMCLI_INDEX', 0),
    )
    timeout = float(getattr(settings, 'HIWAVE_MMCLI_HEALTH_TIMEOUT', 15.0))
    mm = client or MMCLIClient(timeout_sec=timeout)
    issues: list[str] = []

    try:
        indices = mm.list_modem_indices()
    except MmcliError as exc:
        return ModemHealthReport(
            ok=False,
            configured_index=configured,
            issues=[f'mmcli list failed: {exc}'],
        )

    if not indices:
        return ModemHealthReport(
            ok=False,
            configured_index=configured,
            enumerated_indices=[],
            issues=['ModemManager returned zero modems via mmcli -L.'],
        )

    try:
        effective = resolve_modem_mmcli_index(configured, client=mm)
    except MmcliError as exc:
        return ModemHealthReport(
            ok=False,
            configured_index=configured,
            enumerated_indices=list(indices),
            issues=[str(exc)[:512]],
        )

    index_mismatch = effective != configured
    if index_mismatch:
        issues.append(
            f'Configured modem {configured} resolved to effective index {effective} (present={indices}).',
        )

    state = get_modem_state(effective, mmcli_path=mm.mmcli_path)
    if state not in _READY_STATES:
        issues.append(f'Modem state is {state} (expected enabled or registered).')

    ping_ok, ping_detail = mm.modem_ping(effective)
    if not ping_ok:
        issues.append((ping_detail or 'mmcli modem ping failed.')[:512])

    messaging_ok = messaging_interface_ready(effective, mmcli_path=mm.mmcli_path)
    if not messaging_ok:
        issues.append(f'mmcli --messaging-list-sms failed for modem {effective}.')

    ghost_modem = state in _READY_STATES and ping_ok and not messaging_ok
    if ghost_modem:
        issues.append(
            f'Ghost modem suspected: index {effective} reports ready/ping OK but messaging fails.',
        )

    ok = bool(ping_ok and messaging_ok and state in _READY_STATES)
    return ModemHealthReport(
        ok=ok,
        configured_index=configured,
        effective_index=effective,
        enumerated_indices=list(indices),
        modem_state=state,
        ping_ok=ping_ok,
        messaging_ok=messaging_ok,
        index_mismatch=index_mismatch,
        ghost_modem=ghost_modem,
        issues=issues,
    )


def attempt_soft_recovery(configured_index: int | None = None) -> bool:
    """Tier 1: rescan, sync registry, enable/unlock/reset, wait for messaging."""
    configured = int(
        configured_index if configured_index is not None else getattr(settings, 'MODEM_MMCLI_INDEX', 0),
    )
    _LOGGER.warning(
        'MODEM WATCHDOG: tier=1 action=soft_recovery configured_index=%s',
        configured,
    )

    try_rescan_modems()
    try:
        sync_detected_modems()
    except Exception:
        _LOGGER.debug('sync_detected_modems during soft recovery failed', exc_info=True)

    mm = MMCLIClient()
    try:
        indices = mm.list_modem_indices()
    except MmcliError:
        indices = []

    targets = sorted(set(indices or [configured]))
    recovered_any = False
    for idx in targets:
        if get_modem_state(idx) == 'failed':
            try_reset_modem(idx)
        if ensure_modem_ready_for_sms(idx, require_messaging=True):
            recovered_any = True

    report = probe_modem_sms_health(configured, client=mm)
    result = report.ok or recovered_any
    _LOGGER.warning(
        'MODEM WATCHDOG: tier=1 action=soft_recovery result=%s report_ok=%s',
        result,
        report.ok,
    )
    return result


def restart_modem_manager() -> bool:
    """Tier 2: restart ModemManager inside the container (mirrors entrypoint.sh)."""
    _LOGGER.warning('MODEM WATCHDOG: tier=2 action=restart_modem_manager')

    try:
        subprocess.run(
            ['pkill', '-TERM', 'ModemManager'],
            capture_output=True,
            timeout=10,
            check=False,
        )
    except Exception as exc:
        _LOGGER.warning('pkill ModemManager failed: %s', exc)

    time.sleep(3)

    debug = os.environ.get('MODEM_MANAGER_DEBUG', '').strip().lower() in {'1', 'true', 'yes', 'on'}
    log_path = '/tmp/ModemManager.log'
    try:
        with open(log_path, 'ab', buffering=0) as log_file:
            if debug:
                proc = subprocess.Popen(  # noqa: S603
                    ['ModemManager', '--debug'],
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                )
            else:
                proc = subprocess.Popen(  # noqa: S603
                    ['ModemManager'],
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                )
    except Exception as exc:
        _LOGGER.error('MODEM WATCHDOG: tier=2 failed to start ModemManager: %s', exc)
        return False

    warmup = float(os.environ.get('MODEM_MM_WARMUP_SEC', '5'))
    if warmup > 0:
        time.sleep(warmup)

    try_rescan_modems()
    try:
        sync_detected_modems()
    except Exception:
        _LOGGER.debug('sync_detected_modems after MM restart failed', exc_info=True)

    mm = MMCLIClient()
    try:
        indices = mm.list_modem_indices()
    except MmcliError:
        indices = []

    all_ok = True
    configured = int(getattr(settings, 'MODEM_MMCLI_INDEX', 0))
    targets = sorted(set(indices or [configured]))
    for idx in targets:
        if not ensure_modem_ready_for_sms(idx, require_messaging=True):
            all_ok = False

    report = probe_modem_sms_health(configured, client=mm)
    result = report.ok
    _LOGGER.warning(
        'MODEM WATCHDOG: tier=2 action=restart_modem_manager result=%s mm_pid=%s all_ready=%s',
        result,
        proc.pid,
        all_ok,
    )
    if not result:
        with suppress(Exception):
            proc.send_signal(signal.SIGTERM)
    return result


class ModemWatchdogRunner:
    """Periodic probe with tiered escalation and cooldowns."""

    def __init__(self) -> None:
        self._consecutive_failures = 0
        self._last_soft_recovery = 0.0
        self._last_mm_restart = 0.0
        self._last_container_restart = 0.0
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    def _interval_sec(self) -> float:
        return max(5.0, float(getattr(settings, 'MODEM_WATCHDOG_INTERVAL_SEC', 60)))

    def _failure_threshold(self) -> int:
        return max(1, int(getattr(settings, 'MODEM_WATCHDOG_FAILURE_THRESHOLD', 3)))

    def _cooldown(self, name: str, default: float) -> float:
        return max(0.0, float(getattr(settings, name, default)))

    def _can_soft_recover(self) -> bool:
        cooldown = self._cooldown('MODEM_WATCHDOG_SOFT_COOLDOWN_SEC', 120.0)
        return (time.monotonic() - self._last_soft_recovery) >= cooldown

    def _can_restart_mm(self) -> bool:
        if not getattr(settings, 'MODEM_WATCHDOG_AUTO_MM_RESTART', True):
            return False
        cooldown = self._cooldown('MODEM_WATCHDOG_MM_RESTART_COOLDOWN_SEC', 600.0)
        return (time.monotonic() - self._last_mm_restart) >= cooldown

    def _can_restart_container(self) -> bool:
        if not getattr(settings, 'MODEM_WATCHDOG_AUTO_CONTAINER_RESTART', True):
            return False
        if not getattr(settings, 'HIWAVE_ALLOW_CONTAINER_RESTART_API', True):
            return False
        cooldown = self._cooldown('MODEM_WATCHDOG_CONTAINER_RESTART_COOLDOWN_SEC', 3600.0)
        return (time.monotonic() - self._last_container_restart) >= cooldown

    def run_once(self) -> ModemHealthReport:
        configured = int(getattr(settings, 'MODEM_MMCLI_INDEX', 0))
        report = probe_modem_sms_health(configured)

        if report.ok:
            if self._consecutive_failures:
                _LOGGER.info(
                    'MODEM WATCHDOG: probe ok after %s consecutive failure(s)',
                    self._consecutive_failures,
                )
            self._consecutive_failures = 0
            return report

        self._consecutive_failures += 1
        _LOGGER.warning(
            'MODEM WATCHDOG: probe failed consecutive=%s/%s issues=%s',
            self._consecutive_failures,
            self._failure_threshold(),
            report.issues,
        )

        if self._consecutive_failures < self._failure_threshold():
            return report

        if self._can_soft_recover():
            self._last_soft_recovery = time.monotonic()
            if attempt_soft_recovery(configured):
                self._consecutive_failures = 0
                return probe_modem_sms_health(configured)

        if self._can_restart_mm():
            self._last_mm_restart = time.monotonic()
            if restart_modem_manager():
                self._consecutive_failures = 0
                return probe_modem_sms_health(configured)

        if self._can_restart_container():
            self._last_container_restart = time.monotonic()
            _LOGGER.error(
                'MODEM WATCHDOG: tier=3 action=container_restart consecutive_failures=%s',
                self._consecutive_failures,
            )
            schedule_container_restart(requested_by='modem_watchdog')
            self._consecutive_failures = 0

        return report

    def run_forever(self) -> None:
        _LOGGER.info(
            'MODEM WATCHDOG: started interval_sec=%s failure_threshold=%s',
            self._interval_sec(),
            self._failure_threshold(),
        )
        while not self._stop:
            try:
                self.run_once()
            except Exception:
                _LOGGER.exception('MODEM WATCHDOG: probe cycle failed')
            time.sleep(self._interval_sec())
