"""Probe modem/SIM identity via mmcli."""

from __future__ import annotations

import re
from typing import Any

from apps.sms.mmcli_client import MMCLIClient, MmcliError, _merge_mmcli_sources, _parse_keyvalue

_PHONE_KEY_CANDIDATES = (
    'ownnumbers',
    'ownnumber',
    'msisdn',
    'phonenumber',
    '3gppmsisdn',
    'simmsisdn',
)
_MANUFACTURER_KEYS = ('genericmanufacturer', 'manufacturer')
_MODEL_KEYS = ('genericmodel', 'model', 'hardwaremodel')
_IMEI_KEYS = ('genericequipmentidentifier', 'equipmentidentifier', 'equipmentid', 'imei', '3gppimei')
_FIRMWARE_KEYS = ('genericrevision', 'revision', 'firmwareversion', 'softwareversion')
_SIM_PATH_KEYS = ('modemsim', 'sim', 'simpath', 'primarysimpath')


def normalize_phone_e164(raw: str) -> str:
    """Normalize a phone string to E.164-like form (+digits)."""
    value = (raw or '').strip().strip('"').strip("'")
    if not value or value in {'--', 'unknown'}:
        return ''
    value = re.sub(r'[\s\-()]', '', value)
    if value.startswith('+'):
        digits = '+' + re.sub(r'\D', '', value[1:])
        return digits if len(digits) > 1 else ''
    digits_only = re.sub(r'\D', '', value)
    if not digits_only:
        return ''
    return f'+{digits_only}'


def _first_value(details: dict[str, str], keys: tuple[str, ...]) -> str:
    for key in keys:
        val = (details.get(key) or '').strip()
        if val and val not in {'--', 'unknown'}:
            return val
    return ''


def _extract_own_number(details: dict[str, str]) -> str:
    raw = _first_value(details, _PHONE_KEY_CANDIDATES)
    if not raw:
        return ''
    for part in re.split(r'[,;]', raw):
        normalized = normalize_phone_e164(part)
        if normalized:
            return normalized
    return ''


def _extract_sim_path(details: dict[str, str]) -> str:
    for key in _SIM_PATH_KEYS:
        val = (details.get(key) or '').strip()
        if val.startswith('/org/freedesktop/ModemManager1/SIM/'):
            return val
    return ''


def probe_modem_identity(
    modem_index: int,
    *,
    client: MMCLIClient | None = None,
    phone_override: str | None = None,
) -> dict[str, Any]:
    """Return modem identity fields from mmcli; phone may fall back to env override."""
    mm = client or MMCLIClient()
    try:
        modem = mm.show_modem(modem_index)
    except MmcliError:
        modem = {}

    sim_path = _extract_sim_path(modem)
    sim: dict[str, str] = {}
    if sim_path:
        cp = mm._run([mm.mmcli_path, '-i', sim_path, '--output-keyvalue'])  # noqa: SLF001
        if cp.returncode == 0:
            sim = _parse_keyvalue(cp.stdout or '')

    merged = _merge_mmcli_sources(modem, sim)
    phone = _extract_own_number(merged)
    if not phone:
        override = phone_override
        if override is None:
            from apps.sms.modem_env import get_modem_phone_number

            override = get_modem_phone_number(modem_index)
        phone = normalize_phone_e164(override) if override else ''

    return {
        'modem_index': modem_index,
        'phone_number': phone,
        'manufacturer': _first_value(merged, _MANUFACTURER_KEYS),
        'model': _first_value(merged, _MODEL_KEYS),
        'imei': _first_value(merged, _IMEI_KEYS),
        'firmware': _first_value(merged, _FIRMWARE_KEYS),
        'sim_path': sim_path,
    }
