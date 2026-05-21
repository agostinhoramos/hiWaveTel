"""Fetch MQTT flat config from remote hiDisheLink ``GET /api/sms/device/mqtt-config/``."""

from __future__ import annotations

import logging
from typing import Any

import requests
from django.utils import timezone

from .hidishelink_client import HiDishelinkApiClient, HiDishelinkApiError, mqtt_config_flat
from .models import HiDishelinkDevice

_LOGGER = logging.getLogger(__name__)


def resolve_mqtt_config_for_hidishelink_row(
    hid: HiDishelinkDevice,
    *,
    refresh: bool = False,
) -> tuple[dict[str, Any], str]:
    """Return (flat_config, source) for gateway startup with cache-first behavior.

    Returns:
        Tuple of (config_dict, source) where source is 'remote' or 'cache'.

    Strategy:
        - If ``refresh=False`` and cached ``hid.mqtt_config`` is valid dict → return cache immediately.
        - Otherwise attempt remote fetch via :func:`fetch_mqtt_config_for_hidishelink_row`.
        - On transport error: if cache exists → return cache (log INFO, no traceback); else re-raise.

    Used by ``run_mqtt_gateway`` startup to avoid noisy connection failures when cached config works.
    """
    cached = hid.mqtt_config if isinstance(hid.mqtt_config, dict) and hid.mqtt_config else None

    if not refresh and cached:
        fetched_at = hid.mqtt_config_fetched_at.isoformat() if hid.mqtt_config_fetched_at else 'unknown'
        _LOGGER.info(
            'Using cached mqtt-config device_id=%s fetched_at=%s',
            hid.device_id,
            fetched_at,
        )
        return cached, 'cache'

    try:
        flat = fetch_mqtt_config_for_hidishelink_row(hid, log_transport_error=False)
        return flat, 'remote'
    except HiDishelinkApiError as exc:
        if cached:
            _LOGGER.info(
                'Remote mqtt-config fetch failed device_id=%s; using cached snapshot (%s)',
                hid.device_id,
                exc,
            )
            return cached, 'cache'
        raise


def fetch_mqtt_config_for_hidishelink_row(hid: HiDishelinkDevice, *, log_transport_error: bool = True) -> dict[str, Any]:
    """Fetch mqtt-config using ``hid.api_url`` and ``hid.api_key``; persist snapshot on ``hid``.

    Args:
        hid: HiDishelinkDevice with api_url and api_key.
        log_transport_error: If True, log transport errors with full traceback (admin actions).
                             If False, suppress detailed logging (resolver will handle it).
    """
    url = str(hid.api_url or '').strip()
    key = str(hid.api_key or '').strip()
    if not url or not key:
        raise HiDishelinkApiError('HiDisheLink device missing api_url or api_key.', status_code=None, body=None)

    client = HiDishelinkApiClient(url)
    try:
        payload = client.get_mqtt_config(device_id=str(hid.device_id).strip(), api_key=key)
    except HiDishelinkApiError:
        raise
    except requests.RequestException as exc:
        if log_transport_error:
            _LOGGER.warning(
                'hiDisheLink mqtt-config transport error device_id=%s',
                hid.device_id,
                exc_info=True,
            )
        raise HiDishelinkApiError(f'hiDisheLink unreachable: {exc}', status_code=None, body=None) from exc

    flat = mqtt_config_flat(payload)
    if not flat:
        raise HiDishelinkApiError('Remote mqtt-config returned empty data.', status_code=None, body=payload)

    hid.mqtt_config = flat
    hid.mqtt_config_fetched_at = timezone.now()
    hid.save(update_fields=['mqtt_config', 'mqtt_config_fetched_at'])
    return flat


def fetch_mqtt_config_from_hidishelink(*, device_id: str, request_api_key: str) -> dict[str, Any]:
    """Return ``data`` dict from hiDisheLink for ``GET mqtt-config``.

    Uses stored ``HiDishelinkDevice`` URL/key when present for ``device_id``; otherwise proxies to
    ``HIDISHELINK_API_URL`` with ``request_api_key``.

    On success, refreshes ``HiDishelinkDevice.mqtt_config`` when a matching row exists (also done
    inside :func:`fetch_mqtt_config_for_hidishelink_row`).
    """
    did = device_id.strip()
    key_in = request_api_key.strip()
    hid = HiDishelinkDevice.objects.filter(pk=did).first()

    if hid and str(hid.api_url or '').strip() and str(hid.api_key or '').strip():
        return fetch_mqtt_config_for_hidishelink_row(hid)

    client = HiDishelinkApiClient()
    try:
        payload = client.get_mqtt_config(device_id=did, api_key=key_in)
    except HiDishelinkApiError:
        raise
    except requests.RequestException as exc:
        _LOGGER.warning('hiDisheLink mqtt-config transport error device_id=%s', did, exc_info=True)
        raise HiDishelinkApiError(f'hiDisheLink unreachable: {exc}', status_code=None, body=None) from exc

    flat = mqtt_config_flat(payload)
    if not flat:
        raise HiDishelinkApiError('Remote mqtt-config returned empty data.', status_code=None, body=payload)

    if hid:
        hid.mqtt_config = flat
        hid.mqtt_config_fetched_at = timezone.now()
        hid.save(update_fields=['mqtt_config', 'mqtt_config_fetched_at'])

    return flat
