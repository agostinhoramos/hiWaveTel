"""Asynchronous ingestion of ModemManager inbound SMS notifications over D-Bus."""

from __future__ import annotations

import asyncio
import functools
import logging
import os
import time

from dbus_next import BusType
from dbus_next.aio import MessageBus
from dbus_next.errors import DBusError

from django.db import DatabaseError, IntegrityError

from .mmcli_client import MMCLIClient, MmcliError
from .services import persist_inbound_sms

_LOGGER = logging.getLogger(__name__)

_MM_BUS_NAME = 'org.freedesktop.ModemManager1'
_MM_MODEM_INTF = 'org.freedesktop.ModemManager1.Modem.Messaging'


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


async def run_modem_added_listener(
    modem_index: int,
    reconnect_after: float,
    *,
    initial_snapshot: bool = False,
) -> None:
    """Subscribe to Modem.Messaging `Added`, persisting inbound SMS asynchronously."""

    path = modem_object_path(modem_index)

    while True:
        bus: MessageBus | None = None
        try:
            bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
            introspection = await bus.introspect(_MM_BUS_NAME, path)
            proxy = bus.get_proxy_object(_MM_BUS_NAME, path, introspection)
            messaging = proxy.get_interface(_MM_MODEM_INTF)

            async def _on_added(sms_path: str) -> None:
                try:
                    await _persist_async(sms_path, modem_index)
                except Exception as exc:  # noqa: BLE071
                    _LOGGER.exception('Persist inbound %s failed: %s', sms_path, exc)

            messaging.on_added(_on_added)
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
