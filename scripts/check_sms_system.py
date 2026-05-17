#!/usr/bin/env python
"""
Diagnostic script: verifies the full SMS reception chain.

Run inside the container:
    python /app/scripts/check_sms_system.py

Checks performed:
  1. Django + DB connectivity
  2. ModemManager / mmcli availability
  3. Modem enumeration (mmcli -L)
  4. D-Bus system bus accessibility
  5. Modem.Messaging D-Bus interface presence
  6. InboundSms table row count
  7. InboxMessage table row count
  8. Active ExternalDevice count
  9. Signal chain: simulate _make_on_added_callback(received=True) → enqueue

Exit code 0 = all checks passed; non-zero = at least one failure.
"""
from __future__ import annotations

import os
import sys
import subprocess
import textwrap

# ── Bootstrap Django ──────────────────────────────────────────────────────────
os.environ.setdefault('DJANGO_SETTINGS_MODULE', os.environ.get('DJANGO_SETTINGS_MODULE', 'config.settings'))
import django  # noqa: E402
django.setup()

from django.db import connection  # noqa: E402

# ── Helpers ───────────────────────────────────────────────────────────────────
PASS = '\033[92m[PASS]\033[0m'
FAIL = '\033[91m[FAIL]\033[0m'
INFO = '\033[94m[INFO]\033[0m'
WARN = '\033[93m[WARN]\033[0m'

failures: list[str] = []


def ok(label: str, detail: str = '') -> None:
    suffix = f'  {detail}' if detail else ''
    print(f'{PASS} {label}{suffix}')


def fail(label: str, detail: str = '') -> None:
    suffix = f'  {detail}' if detail else ''
    print(f'{FAIL} {label}{suffix}')
    failures.append(label)


def info(label: str, detail: str = '') -> None:
    suffix = f'  {detail}' if detail else ''
    print(f'{INFO} {label}{suffix}')


def warn(label: str, detail: str = '') -> None:
    suffix = f'  {detail}' if detail else ''
    print(f'{WARN} {label}{suffix}')


# ── 1. Django DB ──────────────────────────────────────────────────────────────
print('\n── 1. Django / database ────────────────────────────────────────')
try:
    with connection.cursor() as cur:
        cur.execute('SELECT 1')
    ok('Django DB connection')
except Exception as exc:
    fail('Django DB connection', str(exc))

# ── 2. mmcli binary ──────────────────────────────────────────────────────────
print('\n── 2. mmcli binary ─────────────────────────────────────────────')
mmcli_path = os.environ.get('MMCLI_PATH', 'mmcli')
try:
    cp = subprocess.run([mmcli_path, '--version'], capture_output=True, text=True, timeout=5)
    if cp.returncode == 0:
        ok('mmcli binary found', cp.stdout.strip().split('\n')[0])
    else:
        fail('mmcli binary', cp.stderr.strip()[:200])
except FileNotFoundError:
    fail('mmcli binary not found', f'PATH={os.environ.get("PATH", "")}')
except Exception as exc:
    fail('mmcli binary', str(exc))

# ── 3. Modem enumeration ─────────────────────────────────────────────────────
print('\n── 3. Modem enumeration (mmcli -L) ─────────────────────────────')
try:
    cp = subprocess.run([mmcli_path, '-L'], capture_output=True, text=True, timeout=10)
    output = (cp.stdout + cp.stderr).strip()
    if cp.returncode == 0:
        if 'ModemManager' in output or 'Modem' in output or '/org/' in output:
            ok('Modem(s) found', output[:200])
        else:
            warn('mmcli -L succeeded but no modems listed', output[:200])
    else:
        fail('mmcli -L', output[:200])
except Exception as exc:
    fail('mmcli -L', str(exc))

# ── 4. D-Bus system bus ───────────────────────────────────────────────────────
print('\n── 4. D-Bus system bus ─────────────────────────────────────────')
try:
    import asyncio
    from dbus_next.aio import MessageBus
    from dbus_next import BusType

    async def _probe_dbus():
        bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        bus.disconnect()
        return True

    asyncio.run(_probe_dbus())
    ok('D-Bus system bus accessible')
except Exception as exc:
    fail('D-Bus system bus', str(exc))

# ── 5. Modem.Messaging D-Bus interface ───────────────────────────────────────
print('\n── 5. Modem.Messaging D-Bus interface ──────────────────────────')
modem_index = int(os.environ.get('MODEM_MMCLI_INDEX', '0'))
_MM_BUS_NAME = 'org.freedesktop.ModemManager1'
_MM_MODEM_INTF = 'org.freedesktop.ModemManager1.Modem.Messaging'
modem_path = f'/org/freedesktop/ModemManager1/Modem/{modem_index}'

try:
    async def _probe_messaging():
        from dbus_next.aio import MessageBus
        from dbus_next import BusType
        bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        try:
            introspection = await bus.introspect(_MM_BUS_NAME, modem_path)
            proxy = bus.get_proxy_object(_MM_BUS_NAME, modem_path, introspection)
            iface = proxy.get_interface(_MM_MODEM_INTF)
            return True
        finally:
            bus.disconnect()

    asyncio.run(_probe_messaging())
    ok(f'Modem.Messaging interface on modem {modem_index}')
except Exception as exc:
    fail(f'Modem.Messaging interface on modem {modem_index}', str(exc))

# ── 6. InboundSms count ───────────────────────────────────────────────────────
print('\n── 6. InboundSms rows ──────────────────────────────────────────')
try:
    from apps.sms.models import InboundSms
    count = InboundSms.objects.count()
    if count > 0:
        ok(f'InboundSms table', f'{count} row(s)')
        latest = InboundSms.objects.order_by('-created_at').first()
        info(f'  Latest: from={latest.from_number!r} modem={latest.modem_index} path={latest.mm_path}')
    else:
        warn('InboundSms table is empty — no SMS has ever been persisted')
except Exception as exc:
    fail('InboundSms query', str(exc))

# ── 7. InboxMessage count ─────────────────────────────────────────────────────
print('\n── 7. InboxMessage rows ────────────────────────────────────────')
try:
    from apps.external_device.models import InboxMessage
    count = InboxMessage.objects.count()
    if count > 0:
        ok(f'InboxMessage table', f'{count} row(s)')
    else:
        warn('InboxMessage table is empty')
except Exception as exc:
    fail('InboxMessage query', str(exc))

# ── 8. Active devices ─────────────────────────────────────────────────────────
print('\n── 8. Active ExternalDevices ───────────────────────────────────')
try:
    from apps.external_device.models import ExternalDevice
    devices = list(ExternalDevice.objects.filter(status=ExternalDevice.Status.ACTIVE))
    if devices:
        ok(f'{len(devices)} active device(s)')
        for d in devices:
            info(f'  device_id={d.device_id!r} metadata={d.metadata}')
    else:
        warn('No active ExternalDevices — inbox mirroring will not run')
except Exception as exc:
    fail('ExternalDevice query', str(exc))

# ── 9. Signal chain simulation ────────────────────────────────────────────────
print('\n── 9. Signal chain: _make_on_added_callback(received=True) ─────')
try:
    from unittest.mock import MagicMock, patch
    from apps.sms.dbus_watch import _make_on_added_callback

    mock_queue = MagicMock()
    mock_queue.enqueue.return_value = True

    with patch('apps.sms.dbus_watch.get_sms_queue', return_value=mock_queue):
        cb = _make_on_added_callback(0)
        cb('/org/freedesktop/ModemManager1/SMS/TEST', True)   # received=True

    assert mock_queue.enqueue.called, 'enqueue was not called!'
    call_args = mock_queue.enqueue.call_args
    assert call_args[0][0] == '/org/freedesktop/ModemManager1/SMS/TEST'
    ok('_on_added(path, received=True) correctly calls enqueue', str(call_args))
except AssertionError as exc:
    fail('Signal chain simulation', str(exc))
except Exception as exc:
    fail('Signal chain simulation', str(exc))

# ── 10. mmcli SMS list ────────────────────────────────────────────────────────
print('\n── 10. mmcli --messaging-list-sms ──────────────────────────────')
try:
    cp = subprocess.run(
        [mmcli_path, '-m', str(modem_index), '--messaging-list-sms'],
        capture_output=True, text=True, timeout=15,
    )
    output = (cp.stdout + cp.stderr).strip()
    if cp.returncode == 0:
        ok('mmcli --messaging-list-sms', output[:300] or '(empty list)')
    else:
        warn('mmcli --messaging-list-sms failed (modem may have no stored SMS)', output[:200])
except Exception as exc:
    warn('mmcli --messaging-list-sms', str(exc))

# ── Summary ───────────────────────────────────────────────────────────────────
print('\n' + '─' * 60)
if failures:
    print(f'{FAIL} {len(failures)} check(s) FAILED: {", ".join(failures)}')
    sys.exit(1)
else:
    print(f'{PASS} All checks passed.')
    sys.exit(0)
