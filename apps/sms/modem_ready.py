"""Enable ModemManager modems and wait until SMS operations are allowed."""

from __future__ import annotations

import logging
import os
import re
import subprocess
import time

from apps.sms.modem_env import get_modem_pin_code

_LOGGER = logging.getLogger(__name__)

_STATE_LINE_RE = re.compile(r'state:\s*([a-z_-]+)', re.IGNORECASE)
_READY_STATES = frozenset({'enabled', 'registered'})
_ENABLE_STATES = frozenset({'disabled', 'unknown'})
_SIM_PATH_RE = re.compile(r'/org/freedesktop/ModemManager1/SIM/\d+')
_SIM_PIN_ACTIVE_RE = re.compile(
    r'sim lock status:\s*sim-pin\b|unlock required:\s*sim-pin\b',
    re.IGNORECASE,
)
_MODEM_STATUS_SIM_PIN_LOCK_RE = re.compile(r'\block:\s*sim-pin\b', re.IGNORECASE)


def _modem_overview_reports_sim_pin_lock(haystack: str) -> bool:
    return bool(_MODEM_STATUS_SIM_PIN_LOCK_RE.search(haystack or ''))


def _extract_sim_path_from_modem_overview(haystack: str) -> str:
    m = _SIM_PATH_RE.search(haystack)
    return m.group(0) if m else ''


def _sim_path_needs_pin_unlock(sim_path: str, *, mmcli_path: str) -> bool:
    try:
        cp = subprocess.run(
            [mmcli_path, '-i', sim_path],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception:
        return False
    haystack = f'{cp.stdout or ""}\n{cp.stderr or ""}'
    return bool(_SIM_PIN_ACTIVE_RE.search(haystack))


def _pin_unlock_not_needed(text: str) -> bool:
    lowered = (text or '').lower()
    return 'not sim-pin locked' in lowered or 'device is not sim-pin locked' in lowered


def sim_pin_lock_active(modem_index: int, *, mmcli_path: str | None = None) -> bool:
    """True only when ModemManager reports an active SIM-PIN lock."""
    path = mmcli_path or _mmcli_path()
    state = get_modem_state(modem_index, mmcli_path=path)
    if state in _READY_STATES:
        return False
    try:
        cp = subprocess.run(
            [path, '-m', str(modem_index)],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception as exc:
        _LOGGER.debug('sim_pin_lock_active(%s) modem overview error: %s', modem_index, exc)
        return state == 'locked'
    haystack = f'{cp.stdout or ""}\n{cp.stderr or ""}'
    if "couldn't find modem" in haystack.lower():
        return False
    overview_pin_lock = _modem_overview_reports_sim_pin_lock(haystack)
    sim_path = _extract_sim_path_from_modem_overview(haystack)
    sim_pin_lock = _sim_path_needs_pin_unlock(sim_path, mmcli_path=path) if sim_path else False
    return overview_pin_lock or sim_pin_lock or (state == 'locked' and not sim_path)


def modem_overview_needs_sim_unlock(modem_index: int, *, mmcli_path: str | None = None) -> bool:
    """Backward-compatible alias for strict SIM-PIN lock detection."""
    return sim_pin_lock_active(modem_index, mmcli_path=mmcli_path)


def _mmcli_path() -> str:
    return os.environ.get('MMCLI_PATH', 'mmcli')


def parse_modem_state(stdout: str, stderr: str = '') -> str:
    """Return lowercase modem state from ``mmcli -m N`` output (e.g. ``enabled``, ``disabled``)."""
    haystack = f'{stdout or ""}\n{stderr or ""}'
    for line in haystack.splitlines():
        m = _STATE_LINE_RE.search(line)
        if m:
            return m.group(1).lower()
    return 'unknown'


def _mmcli_command_ok(cp: subprocess.CompletedProcess[str]) -> bool:
    text = f'{cp.stdout or ""}\n{cp.stderr or ""}'.lower()
    if cp.returncode != 0:
        return False
    if 'error:' in text:
        return False
    return True


def get_modem_state(modem_index: int, *, mmcli_path: str | None = None) -> str:
    path = mmcli_path or _mmcli_path()
    try:
        cp = subprocess.run(
            [path, '-m', str(modem_index)],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception as exc:
        _LOGGER.warning('get_modem_state(%s) error: %s', modem_index, exc)
        return 'missing'
    haystack = f'{cp.stdout or ""}\n{cp.stderr or ""}'
    if "couldn't find modem" in haystack.lower():
        return 'missing'
    return parse_modem_state(cp.stdout or '', cp.stderr or '')


_MODEM_PATH_RE = re.compile(r'/org/freedesktop/ModemManager1/Modem/(\d+)')
_last_pin_missing_warn: dict[int, float] = {}


def _warn_pin_missing(modem_index: int) -> None:
    interval = float(os.environ.get('PIN_MISSING_WARN_INTERVAL_SEC', '60'))
    now = time.monotonic()
    last = _last_pin_missing_warn.get(modem_index, 0.0)
    if now - last < interval:
        return
    _last_pin_missing_warn[modem_index] = now
    _LOGGER.warning(
        'Modem %s locked but %s is not set',
        modem_index,
        f'MODEM_{modem_index}_DEVICE_PIN_CODE',
    )


def try_unlock_sim_pin(modem_index: int, *, pin: str | None = None, mmcli_path: str | None = None) -> bool:
    """Run ``mmcli --pin`` on SIM and/or modem when the overview reports SIM-PIN lock."""
    code = pin if pin is not None else get_modem_pin_code(modem_index)
    if not code:
        _warn_pin_missing(modem_index)
        return False
    path = mmcli_path or _mmcli_path()
    unlock_timeout = float(os.environ.get('SIM_UNLOCK_TIMEOUT_SEC', '60'))
    max_attempts = max(1, int(os.environ.get('SIM_UNLOCK_RETRIES', '3')))

    for attempt in range(1, max_attempts + 1):
        pin_lock_active = sim_pin_lock_active(modem_index, mmcli_path=path)
        state = get_modem_state(modem_index, mmcli_path=path)
        if not pin_lock_active:
            if state in _READY_STATES or state not in ('locked',):
                return True
            _LOGGER.info(
                'Modem %s still locked (state=%s) without SIM-PIN flag; attempting PIN unlock',
                modem_index,
                state,
            )

        sim_path = ''
        try:
            cp = subprocess.run(
                [path, '-m', str(modem_index)],
                capture_output=True,
                text=True,
                timeout=10,
            )
            sim_path = _extract_sim_path_from_modem_overview(f'{cp.stdout or ""}\n{cp.stderr or ""}')
        except Exception:
            pass

        for label, argv in (
            [('sim', [path, '-i', sim_path, '--pin', code])] if sim_path else []
        ) + [('modem', [path, '-m', str(modem_index), '--pin', code])]:
            if not argv:
                continue
            try:
                unlock = subprocess.run(
                    argv,
                    capture_output=True,
                    text=True,
                    timeout=unlock_timeout,
                )
                unlock_text = (unlock.stderr or unlock.stdout or '').strip()
                if _pin_unlock_not_needed(unlock_text):
                    _LOGGER.info(
                        'SIM PIN unlock not required modem_index=%s (%s already unlocked)',
                        modem_index,
                        label,
                    )
                    return True
                if _mmcli_command_ok(unlock) and not sim_pin_lock_active(
                    modem_index,
                    mmcli_path=path,
                ):
                    _LOGGER.info(
                        'SIM PIN unlock succeeded modem_index=%s via %s (attempt %s/%s)',
                        modem_index,
                        label,
                        attempt,
                        max_attempts,
                    )
                    reprobe = float(os.environ.get('MODEM_REPROBE_WAIT_SEC', '15'))
                    if reprobe > 0:
                        time.sleep(min(reprobe, 10.0))
                    return True
                _LOGGER.warning(
                    'SIM PIN unlock failed modem_index=%s via %s attempt=%s/%s: %s',
                    modem_index,
                    label,
                    attempt,
                    max_attempts,
                    unlock_text[:200],
                )
            except Exception as exc:
                _LOGGER.warning(
                    'try_unlock_sim_pin(%s) via %s attempt=%s error: %s',
                    modem_index,
                    label,
                    attempt,
                    exc,
                )

        if attempt < max_attempts:
            time.sleep(float(os.environ.get('SIM_UNLOCK_RETRY_SEC', '5')))

    return False


def try_enable_modem(modem_index: int, *, mmcli_path: str | None = None) -> None:
    """Run ``mmcli --enable`` when the modem is ``disabled`` or still ``unknown``."""
    path = mmcli_path or _mmcli_path()
    state = get_modem_state(modem_index, mmcli_path=path)
    if state in _READY_STATES:
        _LOGGER.debug('Modem %s already ready (state=%s); skipping enable', modem_index, state)
        return
    if state == 'missing':
        return
    if state == 'locked' or sim_pin_lock_active(modem_index, mmcli_path=path):
        _LOGGER.debug('Modem %s locked; enable skipped until PIN unlock', modem_index)
        return
    if state not in _ENABLE_STATES:
        _LOGGER.debug('Modem %s state=%s; skipping enable', modem_index, state)
        return
    try:
        _LOGGER.info('Modem %s is %s — attempting mmcli --enable ...', modem_index, state)
        en_cp = subprocess.run(
            [path, '-m', str(modem_index), '--enable'],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if en_cp.returncode == 0:
            _LOGGER.info('mmcli --enable modem %s succeeded', modem_index)
        else:
            _LOGGER.warning(
                'mmcli --enable modem %s failed (rc=%s): %s',
                modem_index,
                en_cp.returncode,
                (en_cp.stderr or en_cp.stdout or '').strip()[:200],
            )
    except Exception as exc:
        _LOGGER.warning('try_enable_modem(%s) error: %s', modem_index, exc)


def try_reset_modem(modem_index: int, *, mmcli_path: str | None = None) -> bool:
    """Run ``mmcli --reset`` when the modem reports ``failed`` (e.g. unknown-capabilities)."""
    path = mmcli_path or _mmcli_path()
    state = get_modem_state(modem_index, mmcli_path=path)
    if state != 'failed':
        return False
    try:
        _LOGGER.warning('Modem %s is failed — attempting mmcli --reset ...', modem_index)
        cp = subprocess.run(
            [path, '-m', str(modem_index), '--reset'],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if cp.returncode == 0:
            _LOGGER.info('mmcli --reset modem %s succeeded', modem_index)
            return True
        _LOGGER.warning(
            'mmcli --reset modem %s failed (rc=%s): %s',
            modem_index,
            cp.returncode,
            (cp.stderr or cp.stdout or '').strip()[:200],
        )
    except Exception as exc:
        _LOGGER.warning('try_reset_modem(%s) error: %s', modem_index, exc)
    return False


def try_rescan_modems(*, mmcli_path: str | None = None) -> None:
    """Request ModemManager device rescan (``mmcli -S``)."""
    path = mmcli_path or _mmcli_path()
    try:
        cp = subprocess.run([path, '-S'], capture_output=True, text=True, timeout=30)
        if cp.returncode == 0:
            _LOGGER.debug('mmcli -S rescan succeeded')
        else:
            _LOGGER.debug(
                'mmcli -S rescan rc=%s: %s',
                cp.returncode,
                (cp.stderr or cp.stdout or '').strip()[:120],
            )
    except Exception as exc:
        _LOGGER.debug('try_rescan_modems error: %s', exc)


def list_modem_indices(*, mmcli_path: str | None = None) -> list[int]:
    path = mmcli_path or _mmcli_path()
    try:
        cp = subprocess.run([path, '-L'], capture_output=True, text=True, timeout=15)
    except Exception as exc:
        _LOGGER.debug('list_modem_indices error: %s', exc)
        return []
    if cp.returncode != 0:
        return []
    return sorted({int(m.group(1)) for m in _MODEM_PATH_RE.finditer(cp.stdout or '')})


def messaging_interface_ready(modem_index: int, *, mmcli_path: str | None = None) -> bool:
    """True when ``mmcli --messaging-list-sms`` succeeds (Messaging stack is up)."""
    path = mmcli_path or _mmcli_path()
    try:
        cp = subprocess.run(
            [path, '-m', str(modem_index), '--messaging-list-sms'],
            capture_output=True,
            text=True,
            timeout=20,
        )
        return cp.returncode == 0
    except Exception as exc:
        _LOGGER.debug('messaging_interface_ready(%s) error: %s', modem_index, exc)
        return False


def _recover_modem_state(modem_index: int, *, mmcli_path: str | None = None) -> None:
    state = get_modem_state(modem_index, mmcli_path=mmcli_path)
    if state == 'failed':
        if try_reset_modem(modem_index, mmcli_path=mmcli_path):
            reprobe = float(os.environ.get('MODEM_REPROBE_WAIT_SEC', '15'))
            if reprobe > 0:
                time.sleep(reprobe)
            try_rescan_modems(mmcli_path=mmcli_path)
        return
    if state == 'locked' or sim_pin_lock_active(modem_index, mmcli_path=mmcli_path):
        try_unlock_sim_pin(modem_index, mmcli_path=mmcli_path)
        return
    if state in _ENABLE_STATES:
        try_enable_modem(modem_index, mmcli_path=mmcli_path)


def _resolve_working_modem_index(configured_index: int, *, mmcli_path: str | None = None) -> int | None:
    """Return a present mmcli index (configured or primary fallback), or None if none listed."""
    from apps.sms.mmcli_client import MMCLIClient, MmcliError, resolve_modem_mmcli_index

    path = mmcli_path or _mmcli_path()
    try:
        return resolve_modem_mmcli_index(configured_index, client=MMCLIClient(mmcli_path=path))
    except MmcliError:
        return None


def wait_modem_ready_for_sms(
    modem_index: int,
    *,
    timeout_sec: float | None = None,
    mmcli_path: str | None = None,
    require_messaging: bool = False,
) -> bool:
    """Poll until modem is ``enabled`` or ``registered``, recovering when needed.

    Returns True when ready, False on timeout (same semantics as docker entrypoint).
    """
    configured_index = modem_index
    wait_sec = timeout_sec if timeout_sec is not None else float(
        os.environ.get('MODEM_ENABLE_WAIT_SEC', '20'),
    )
    path = mmcli_path or _mmcli_path()
    deadline = time.monotonic() + max(0.0, wait_sec)
    last_rescan = 0.0
    rescan_interval = float(os.environ.get('MODEM_RESCAN_INTERVAL_SEC', '15'))

    while time.monotonic() < deadline:
        active_index = _resolve_working_modem_index(configured_index, mmcli_path=path)
        if active_index is None:
            if time.monotonic() - last_rescan >= rescan_interval:
                try_rescan_modems(mmcli_path=path)
                last_rescan = time.monotonic()
            time.sleep(1.0)
            continue

        modem_index = active_index
        state = get_modem_state(modem_index, mmcli_path=path)
        if state in _READY_STATES:
            if require_messaging and not messaging_interface_ready(modem_index, mmcli_path=path):
                _recover_modem_state(modem_index, mmcli_path=path)
                time.sleep(1.0)
                continue
            _LOGGER.info('Modem %s ready for SMS (state=%s)', modem_index, state)
            return True

        _recover_modem_state(modem_index, mmcli_path=path)
        time.sleep(1.0)

    active_index = _resolve_working_modem_index(configured_index, mmcli_path=path)
    if active_index is None:
        final_state = 'missing'
    else:
        final_state = get_modem_state(active_index, mmcli_path=path)
    _LOGGER.warning(
        'Modem %s not ready for SMS after %.0fs (last state=%s)',
        configured_index,
        wait_sec,
        final_state,
    )
    return False


def ensure_modem_ready_for_sms(
    configured_index: int,
    *,
    mmcli_path: str | None = None,
    require_messaging: bool = True,
) -> bool:
    """Unlock SIM, enable/reset modem, wait until SMS/Messaging is usable."""
    return wait_modem_ready_for_sms(
        configured_index,
        mmcli_path=mmcli_path,
        require_messaging=require_messaging,
    )


def prepare_modem_for_outbound_sms(modem_index: int, *, mmcli_path: str | None = None) -> None:
    """Best-effort SIM unlock, enable/reset, and wait before ``mmcli --messaging-create-sms``."""
    ensure_modem_ready_for_sms(modem_index, mmcli_path=mmcli_path, require_messaging=True)
