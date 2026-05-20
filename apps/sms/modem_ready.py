"""Enable ModemManager modems and wait until SMS operations are allowed."""

from __future__ import annotations

import logging
import os
import re
import subprocess
import time

_LOGGER = logging.getLogger(__name__)

_STATE_LINE_RE = re.compile(r'state:\s*([a-z_-]+)', re.IGNORECASE)
_READY_STATES = frozenset({'enabled', 'registered'})
_MODEM_LOCKED_RE = re.compile(
    r'(?:^|\s)state:\s*locked|lock:\s*sim-pin|enabled locks:.*\bsim\b',
    re.IGNORECASE,
)


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
        return 'unknown'
    return parse_modem_state(cp.stdout or '', cp.stderr or '')


def modem_overview_needs_sim_unlock(modem_index: int, *, mmcli_path: str | None = None) -> bool:
    path = mmcli_path or _mmcli_path()
    try:
        cp = subprocess.run(
            [path, '-m', str(modem_index)],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception as exc:
        _LOGGER.warning('modem_overview_needs_sim_unlock(%s) error: %s', modem_index, exc)
        return False
    haystack = f'{cp.stdout or ""}\n{cp.stderr or ""}'
    return bool(_MODEM_LOCKED_RE.search(haystack))


def try_unlock_sim_pin(modem_index: int, *, pin: str | None = None, mmcli_path: str | None = None) -> bool:
    """Run ``mmcli --pin`` on SIM and/or modem when the overview reports SIM-PIN lock."""
    code = (pin if pin is not None else os.environ.get('DEVICE_PIN_CODE', '')).strip()
    if not code:
        _LOGGER.warning('Modem %s locked but DEVICE_PIN_CODE is not set', modem_index)
        return False
    path = mmcli_path or _mmcli_path()
    sim_path = ''
    try:
        cp = subprocess.run(
            [path, '-m', str(modem_index)],
            capture_output=True,
            text=True,
            timeout=10,
        )
        m = re.search(r'/org/freedesktop/ModemManager1/SIM/\d+', (cp.stdout or '') + (cp.stderr or ''))
        if m:
            sim_path = m.group(0)
    except Exception:
        pass
    for argv in (
        [path, '-i', sim_path, '--pin', code] if sim_path else None,
        [path, '-m', str(modem_index), '--pin', code],
    ):
        if not argv:
            continue
        try:
            unlock = subprocess.run(argv, capture_output=True, text=True, timeout=30)
            if unlock.returncode == 0:
                _LOGGER.info('SIM PIN unlock succeeded modem_index=%s', modem_index)
                return True
            _LOGGER.warning(
                'SIM PIN unlock failed modem_index=%s rc=%s: %s',
                modem_index,
                unlock.returncode,
                (unlock.stderr or unlock.stdout or '').strip()[:200],
            )
        except Exception as exc:
            _LOGGER.warning('try_unlock_sim_pin(%s) error: %s', modem_index, exc)
    return False


def try_enable_modem(modem_index: int, *, mmcli_path: str | None = None) -> None:
    """Run ``mmcli --enable`` when the modem reports ``disabled`` state."""
    path = mmcli_path or _mmcli_path()
    try:
        cp = subprocess.run(
            [path, '-m', str(modem_index)],
            capture_output=True,
            text=True,
            timeout=10,
        )
        haystack = f'{cp.stdout or ""}\n{cp.stderr or ""}'
        if 'state: disabled' not in haystack.lower():
            _LOGGER.debug('Modem %s not disabled; skipping enable', modem_index)
            return
        _LOGGER.info('Modem %s is disabled — attempting mmcli --enable ...', modem_index)
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


def wait_modem_ready_for_sms(
    modem_index: int,
    *,
    timeout_sec: float | None = None,
    mmcli_path: str | None = None,
) -> bool:
    """Poll until modem is ``enabled`` or ``registered``, enabling if ``disabled``.

    Returns True when ready, False on timeout (same semantics as docker entrypoint).
    """
    wait_sec = timeout_sec if timeout_sec is not None else float(
        os.environ.get('MODEM_ENABLE_WAIT_SEC', '20'),
    )
    path = mmcli_path or _mmcli_path()
    deadline = time.monotonic() + max(0.0, wait_sec)
    while time.monotonic() < deadline:
        state = get_modem_state(modem_index, mmcli_path=path)
        if state in _READY_STATES:
            _LOGGER.debug('Modem %s ready (state=%s)', modem_index, state)
            return True
        if state == 'locked' or modem_overview_needs_sim_unlock(modem_index, mmcli_path=path):
            try_unlock_sim_pin(modem_index, mmcli_path=path)
        if state == 'disabled':
            try_enable_modem(modem_index, mmcli_path=path)
        time.sleep(1.0)
    _LOGGER.warning(
        'Modem %s not ready for SMS after %.0fs (last state=%s)',
        modem_index,
        wait_sec,
        get_modem_state(modem_index, mmcli_path=path),
    )
    return False


def prepare_modem_for_outbound_sms(modem_index: int, *, mmcli_path: str | None = None) -> None:
    """Best-effort SIM unlock, enable, and wait before ``mmcli --messaging-create-sms``."""
    if modem_overview_needs_sim_unlock(modem_index, mmcli_path=mmcli_path):
        try_unlock_sim_pin(modem_index, mmcli_path=mmcli_path)
    try_enable_modem(modem_index, mmcli_path=mmcli_path)
    wait_modem_ready_for_sms(modem_index, mmcli_path=mmcli_path)
