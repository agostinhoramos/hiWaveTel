"""Detect modems via mmcli and persist ModemDevice records."""

from __future__ import annotations

import logging
import re
from typing import Any

from django.utils import timezone

from apps.sms.mmcli_client import MMCLIClient, MmcliError
from apps.sms.modem_identity import probe_modem_identity
from apps.sms.modem_readiness import check_modem_availability
from apps.sms.models import ModemDevice

_LOGGER = logging.getLogger(__name__)
_MODEM_PATH_RE = re.compile(r'(/org/freedesktop/ModemManager1/Modem/(\d+))')


class ModemNotEnumeratedError(Exception):
    """Raised when a modem index is not reported by ModemManager."""


def _parse_modem_paths(haystack: str) -> dict[int, str]:
    paths: dict[int, str] = {}
    for match in _MODEM_PATH_RE.finditer(haystack or ''):
        paths[int(match.group(2))] = match.group(1)
    return paths


def list_enumerated_modem_indices(*, client: MMCLIClient | None = None) -> list[int]:
    mm = client or MMCLIClient()
    return mm.list_modem_indices()


def assert_modem_enumerated(modem_index: int, *, client: MMCLIClient | None = None) -> None:
    """Raise ModemNotEnumeratedError when the index is absent from mmcli -L."""
    mm = client or MMCLIClient()
    try:
        indices = mm.list_modem_indices()
    except MmcliError as exc:
        raise ModemNotEnumeratedError(
            f'ModemManager unreachable: {exc}',
        ) from exc
    if modem_index not in indices:
        label = ', '.join(str(i) for i in indices) if indices else 'none'
        raise ModemNotEnumeratedError(
            f'Modem index {modem_index} not enumerated (mmcli -L: {label}).',
        )


def sync_detected_modems(*, client: MMCLIClient | None = None) -> list[ModemDevice]:
    """Upsert ModemDevice rows from mmcli -L and mark absent modems."""
    mm = client or MMCLIClient()
    try:
        cp = mm._run([mm.mmcli_path, '-L'])  # noqa: SLF001
        mm._ensure_ok(cp, 'mmcli -L')  # noqa: SLF001
    except MmcliError as exc:
        _LOGGER.warning('sync_detected_modems mmcli -L failed: %s', exc)
        return list(ModemDevice.objects.order_by('modem_index'))

    haystack = f'{cp.stdout or ""}\n{cp.stderr or ""}'
    paths_by_index = _parse_modem_paths(haystack)
    seen: set[int] = set()

    for index in sorted(paths_by_index):
        seen.add(index)
        dbus_path = paths_by_index[index]
        obj, created = ModemDevice.objects.get_or_create(
            modem_index=index,
            defaults={'dbus_path': dbus_path, 'is_present': True},
        )
        if not created:
            obj.dbus_path = dbus_path
            obj.is_present = True
            obj.save(update_fields=['dbus_path', 'is_present', 'last_detected_at'])
        if created:
            _LOGGER.info('Modem detected and registered modem_index=%s path=%s', index, dbus_path)

    if seen:
        ModemDevice.objects.exclude(modem_index__in=seen).update(is_present=False)
    else:
        ModemDevice.objects.update(is_present=False)

    return list(ModemDevice.objects.order_by('modem_index'))


def _modem_device_or_none(modem_index: int) -> ModemDevice | None:
    try:
        return ModemDevice.objects.get(modem_index=modem_index)
    except ModemDevice.DoesNotExist:
        return None


def build_modem_summary(
    device: ModemDevice,
    *,
    client: MMCLIClient | None = None,
) -> dict[str, Any]:
    """Persisted modem row plus lightweight live state."""
    mm = client or MMCLIClient()
    availability = check_modem_availability(device.modem_index, client=mm)
    identity = probe_modem_identity(device.modem_index, client=mm)
    return {
        'modem_index': device.modem_index,
        'enabled': device.enabled,
        'is_present': device.is_present,
        'dbus_path': device.dbus_path,
        'phone_number': identity.get('phone_number') or '',
        'manufacturer': identity.get('manufacturer') or '',
        'model': identity.get('model') or '',
        'state': availability.state,
        'available': availability.available,
        'first_detected_at': device.first_detected_at.isoformat(),
        'last_detected_at': device.last_detected_at.isoformat(),
    }


def get_modem_detail(
    modem_index: int,
    *,
    client: MMCLIClient | None = None,
) -> dict[str, Any] | None:
    """Full modem detail for API; None when never detected (no DB row)."""
    device = _modem_device_or_none(modem_index)
    if device is None:
        return None

    mm = client or MMCLIClient()
    availability = check_modem_availability(modem_index, client=mm)
    identity = probe_modem_identity(modem_index, client=mm)
    avail_dict = availability.to_dict()

    return {
        'modem_index': device.modem_index,
        'enabled': device.enabled,
        'is_present': device.is_present,
        'dbus_path': device.dbus_path,
        'phone_number': identity.get('phone_number') or '',
        'manufacturer': identity.get('manufacturer') or '',
        'model': identity.get('model') or '',
        'imei': identity.get('imei') or '',
        'firmware': identity.get('firmware') or '',
        'sim_path': identity.get('sim_path') or '',
        'state': availability.state,
        'available': availability.available,
        'checked_at': avail_dict.get('checked_at') or timezone.now().isoformat(),
        'enumerated_indices': avail_dict.get('enumerated_indices') or [],
        'ping_ok': avail_dict.get('ping_ok'),
        'detail': avail_dict.get('detail') or '',
        'last_activity': avail_dict.get('last_activity') or {},
        'first_detected_at': device.first_detected_at.isoformat(),
        'last_detected_at': device.last_detected_at.isoformat(),
    }
