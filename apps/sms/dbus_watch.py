"""Asynchronous ingestion of ModemManager inbound SMS notifications over D-Bus."""

from __future__ import annotations

import asyncio
import functools
import logging
import os
import time

from dbus_next import BusType
from dbus_next.aio import MessageBus
from dbus_next.errors import DBusError, InterfaceNotFoundError
import dbus_next.message_bus as _dbus_mb

from django.db import DatabaseError, IntegrityError

from .mmcli_client import MMCLIClient, MmcliError, resolve_modem_mmcli_index
from .modem_ready import try_enable_modem as _try_enable_modem
from .queue_processor import get_sms_queue
from .services import persist_inbound_sms

_LOGGER = logging.getLogger(__name__)

_MM_BUS_NAME = 'org.freedesktop.ModemManager1'
_MM_MODEM_INTF = 'org.freedesktop.ModemManager1.Modem.Messaging'

# Configuration for modem interface readiness retry
_MODEM_INTF_WAIT_RETRIES = int(os.environ.get('MODEM_INTF_WAIT_RETRIES', '12'))
_MODEM_INTF_WAIT_SEC = float(os.environ.get('MODEM_INTF_WAIT_SEC', '3.0'))

# Monkey-patch dbus-next to handle None msg in add_match_notify (known bug in dbus-next)
_original_add_match_notify = None

def _safe_add_match_notify(self_bus, msg, err):  # noqa: ANN001
    """Guard against msg=None in dbus-next add_match_notify callback."""
    if msg is None:
        return
    return _original_add_match_notify(self_bus, msg, err)

# Apply patch only once at module import
_orig = getattr(_dbus_mb.BaseMessageBus, '_add_match_notify', None)
if _orig is not None:
    _original_add_match_notify = _orig
    _dbus_mb.BaseMessageBus._add_match_notify = _safe_add_match_notify


def modem_object_path(modem_index: int) -> str:
    return f'/org/freedesktop/ModemManager1/Modem/{modem_index}'


def sync_modem_sms_snapshot(modem_index: int, client: MMCLIClient | None = None) -> int:
    """Persist any inbound SMS paths currently known to ModemManager."""

    mm = client or MMCLIClient()

    tries = max(1, int(os.environ.get('MMCLI_SNAPSHOT_TRIES', '3')))
    backoff = float(os.environ.get('MMCLI_SNAPSHOT_BACKOFF_SEC', '1.25'))

    paths: list[str] = []
    for attempt in range(tries):
        try:
            paths = mm.list_sms_paths(modem_index)
            break
        except (MmcliError, OSError) as exc:
            _LOGGER.warning(
                'Initial mmcli --messaging-list-sms failed modem=%s attempt=%s/%s: %s',
                modem_index,
                attempt + 1,
                tries,
                exc,
            )
            if attempt + 1 >= tries:
                return 0
            time.sleep(backoff * (2**attempt))

    n = 0
    for path in paths:
        try:
            persist_inbound_sms(path, modem_index, mm)
            n += 1
        except IntegrityError:
            _LOGGER.warning('Integrity error persisting inbound %s — skipping.', path)
        except DatabaseError as exc:
            _LOGGER.warning('Database error persisting inbound %s at startup sync: %s', path, exc)
        except (MmcliError, ValueError, TypeError) as exc:
            _LOGGER.warning('Unexpected data error persisting inbound %s at startup sync: %s', path, exc)
        except Exception:  # noqa: BLE071
            _LOGGER.exception('Failed to persist inbound %s at startup sync', path)

    _LOGGER.info('Startup snapshot for modem %s synced %s SMS objects', modem_index, n)
    return n


async def _persist_async(mm_path: str, modem_index: int) -> None:
    loop = asyncio.get_running_loop()

    def _persist() -> None:
        persist_inbound_sms(mm_path, modem_index, None)

    await loop.run_in_executor(None, _persist)


def _make_on_added_callback(modem_index: int):
    """Return the Messaging.Added signal handler for the given modem index.

    The ModemManager D-Bus signal signature is: Added(o path, b received)
      - path:     SMS object path e.g. /org/freedesktop/ModemManager1/SMS/N
      - received: True when the SMS just arrived over-the-air,
                  False when loaded from modem storage.
    dbus-next calls this as callback(path, received) — NOT (data, error).
    """
    def _on_added(sms_path: str, received: bool = False) -> None:
        """Synchronous callback for D-Bus Messaging.Added signal."""
        _LOGGER.info(
            'D-Bus Messaging.Added: sms_path=%s received=%s modem_index=%s',
            sms_path, received, modem_index,
        )

        try:
            sms_queue = get_sms_queue()
            success = sms_queue.enqueue(sms_path, modem_index)

            if success:
                _LOGGER.info('SMS enqueued: sms_path=%s modem_index=%s', sms_path, modem_index)
            else:
                _LOGGER.error('Failed to enqueue SMS (queue full): %s', sms_path)
                # Fallback: persist synchronously as last resort
                try:
                    persist_inbound_sms(sms_path, modem_index, None)
                except Exception as fallback_exc:
                    _LOGGER.exception('Fallback persist also failed for %s: %s', sms_path, fallback_exc)
        except Exception as exc:
            _LOGGER.exception('Failed to enqueue SMS %s: %s', sms_path, exc)

    return _on_added


async def run_modem_added_listener(
    modem_index: int,
    reconnect_after: float,
    *,
    initial_snapshot: bool = False,
) -> None:
    """Subscribe to Modem.Messaging `Added`, persisting inbound SMS asynchronously."""

    configured_index = modem_index

    while True:
        loop = asyncio.get_running_loop()
        try:
            modem_index = await loop.run_in_executor(
                None,
                lambda: resolve_modem_mmcli_index(configured_index),
            )
        except MmcliError as exc:
            _LOGGER.warning('SMS watcher: cannot resolve modem index (%s): %s', configured_index, exc)
            await asyncio.sleep(reconnect_after)
            continue

        path = modem_object_path(modem_index)
        bus: MessageBus | None = None
        try:
            bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
            
            # Retry loop: modem interface may not be ready immediately at startup.
            # If the modem is disabled, Modem.Messaging does not appear — try to
            # enable it once halfway through the retries.
            messaging = None
            _enable_attempted = False
            for attempt in range(_MODEM_INTF_WAIT_RETRIES):
                introspection = await bus.introspect(_MM_BUS_NAME, path)
                proxy = bus.get_proxy_object(_MM_BUS_NAME, path, introspection)
                try:
                    messaging = proxy.get_interface(_MM_MODEM_INTF)
                    break
                except InterfaceNotFoundError:
                    if attempt + 1 >= _MODEM_INTF_WAIT_RETRIES:
                        raise
                    _LOGGER.warning(
                        'Modem.Messaging not ready yet on modem %s (attempt %d/%d), retry in %.1fs',
                        modem_index, attempt + 1, _MODEM_INTF_WAIT_RETRIES, _MODEM_INTF_WAIT_SEC,
                    )
                    # Halfway through retries, try enabling the modem in case it is disabled.
                    if not _enable_attempted and attempt + 1 >= _MODEM_INTF_WAIT_RETRIES // 2:
                        _enable_attempted = True
                        loop = asyncio.get_running_loop()
                        await loop.run_in_executor(None, _try_enable_modem, modem_index)
                    await asyncio.sleep(_MODEM_INTF_WAIT_SEC)

            messaging.on_added(_make_on_added_callback(modem_index))
            _LOGGER.info('Listening on %s Messaging.Added (system bus)', path)

            # Listener attached first; snapshot uses get_or_create and won't duplicate dangerously.
            if initial_snapshot:
                exec_loop = asyncio.get_running_loop()
                snap = functools.partial(sync_modem_sms_snapshot, modem_index)
                await exec_loop.run_in_executor(None, snap)
                _LOGGER.info('Initial snapshot processed after subscribing (modem %s).', modem_index)

            await bus.wait_for_disconnect()
            _LOGGER.warning('D-Bus disconnected for modem path %s; reconnecting...', path)

        except DBusError as exc:
            _LOGGER.warning('SMS watcher D-Bus error (%s): %s', modem_index, exc)
        except InterfaceNotFoundError as exc:
            _LOGGER.warning('SMS watcher: Modem.Messaging unavailable on modem %s after all retries: %s', modem_index, exc)
        except OSError as exc:
            _LOGGER.warning('SMS watcher OS level failure (%s): %s', modem_index, exc)
        except Exception as exc:  # noqa: BLE071
            _LOGGER.exception('SMS watcher iteration failed (%s): %s', modem_index, exc)

        finally:
            if bus is not None:
                try:
                    bus.disconnect()
                except OSError:
                    pass

        await asyncio.sleep(reconnect_after)
