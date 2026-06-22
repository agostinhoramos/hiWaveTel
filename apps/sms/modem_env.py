"""Per-modem configuration from environment (MODEM_N_* variables)."""

from __future__ import annotations

import os
import re

from apps.sms.modem_identity import normalize_phone_e164

_ENV_KEY_RE = re.compile(r'^MODEM_(\d+)_(.+)$')


def modem_env_key(modem_index: int, suffix: str) -> str:
    """Build env var name, e.g. ``MODEM_0_DEVICE_PIN_CODE``."""
    return f'MODEM_{modem_index}_{suffix}'


def _strip_env_value(raw: str) -> str:
    value = (raw or '').strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    return value.strip()


def get_modem_env(modem_index: int, suffix: str, default: str = '') -> str:
    """Read ``MODEM_{index}_{suffix}`` from the environment."""
    return _strip_env_value(os.environ.get(modem_env_key(modem_index, suffix), default))


def get_modem_pin_code(modem_index: int) -> str:
    """SIM PIN for the given mmcli modem index."""
    return get_modem_env(modem_index, 'DEVICE_PIN_CODE')


def get_modem_phone_number(modem_index: int) -> str:
    """Optional MSISDN fallback when mmcli does not report own number."""
    return normalize_phone_e164(get_modem_env(modem_index, 'DEVICE_PHONE_NUMBER'))


def list_modem_indices_from_env() -> list[int]:
    """Return modem indices that have any ``MODEM_N_*`` env var set."""
    indices: set[int] = set()
    for key in os.environ:
        match = _ENV_KEY_RE.match(key)
        if match:
            indices.add(int(match.group(1)))
    return sorted(indices)
