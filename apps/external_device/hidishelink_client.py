"""HTTP client for remote hiDisheLink REST API (device onboarding, MQTT config)."""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import quote, urlencode, urljoin

import requests
from django.conf import settings

_LOGGER = logging.getLogger(__name__)


class HiDishelinkApiError(Exception):
    """Non-success response from hiDisheLink API."""

    def __init__(self, message: str, *, status_code: int | None = None, body: Any = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body


def _timeout_sec() -> float:
    return float(getattr(settings, 'HIDISHELINK_API_TIMEOUT_SEC', 30))


def _normalize_json(payload: Any) -> dict[str, Any]:
    if isinstance(payload, dict):
        return payload
    return {}


class HiDishelinkApiClient:
    """Thin wrapper around hiDisheLink `/api/sms/device/...` endpoints."""

    def __init__(self, base_url: str | None = None) -> None:
        raw = (base_url or getattr(settings, 'HIDISHELINK_API_URL', '') or '').strip().rstrip('/')
        self.base_url = raw or 'http://127.0.0.1:5201'

    def _url(self, path: str) -> str:
        path = path if path.startswith('/') else f'/{path}'
        return urljoin(self.base_url + '/', path.lstrip('/'))

    def _headers_json(self, *, api_key: str | None = None) -> dict[str, str]:
        h = {'Accept': 'application/json', 'Content-Type': 'application/json'}
        if api_key:
            h['X-API-Key'] = api_key.strip()
        return h

    def register(
        self,
        *,
        device_id: str,
        registration_token: str,
        device_model: str | None = None,
        android_version: str | None = None,
        app_version: str | None = None,
    ) -> dict[str, Any]:
        """POST /api/sms/device/register/"""
        body: dict[str, Any] = {
            'device_id': device_id.strip(),
            'registration_token': registration_token.strip(),
        }
        if device_model:
            body['device_model'] = device_model
        if android_version:
            body['android_version'] = android_version
        if app_version:
            body['app_version'] = app_version

        url = self._url('/api/sms/device/register/')
        r = requests.post(url, json=body, headers=self._headers_json(), timeout=_timeout_sec())
        data = self._parse_response(r)
        _LOGGER.info('hiDisheLink register device_id=%s http_status=%s', device_id, r.status_code)
        return data

    def get_pending_key(self, *, device_id: str) -> dict[str, Any]:
        """POST /api/sms/device/get-pending-key/"""
        url = self._url('/api/sms/device/get-pending-key/')
        r = requests.post(
            url,
            json={'device_id': device_id.strip()},
            headers=self._headers_json(),
            timeout=_timeout_sec(),
        )
        data = self._parse_response(r)
        _LOGGER.info('hiDisheLink get-pending-key device_id=%s http_status=%s', device_id, r.status_code)
        return data

    def login(self, *, device_id: str, api_key: str) -> dict[str, Any]:
        """POST /api/sms/device/login/"""
        url = self._url('/api/sms/device/login/')
        r = requests.post(
            url,
            json={'device_id': device_id.strip(), 'api_key': api_key.strip()},
            headers=self._headers_json(),
            timeout=_timeout_sec(),
        )
        data = self._parse_response(r)
        _LOGGER.info('hiDisheLink login device_id=%s http_status=%s', device_id, r.status_code)
        return data

    def refresh_session(self, *, device_id: str, session_id: str) -> dict[str, Any]:
        """POST /api/sms/device/refresh/"""
        url = self._url('/api/sms/device/refresh/')
        r = requests.post(
            url,
            json={'device_id': device_id.strip(), 'session_id': session_id.strip()},
            headers=self._headers_json(),
            timeout=_timeout_sec(),
        )
        data = self._parse_response(r)
        _LOGGER.info('hiDisheLink refresh device_id=%s http_status=%s', device_id, r.status_code)
        return data

    def logout(self, *, device_id: str, session_id: str) -> dict[str, Any]:
        """POST /api/sms/device/logout/"""
        url = self._url('/api/sms/device/logout/')
        r = requests.post(
            url,
            json={'device_id': device_id.strip(), 'session_id': session_id.strip()},
            headers=self._headers_json(),
            timeout=_timeout_sec(),
        )
        data = self._parse_response(r)
        _LOGGER.info('hiDisheLink logout device_id=%s http_status=%s', device_id, r.status_code)
        return data

    def get_mqtt_config(self, *, device_id: str, api_key: str) -> dict[str, Any]:
        """GET /api/sms/device/mqtt-config/?device_id=…"""
        q = urlencode({'device_id': device_id.strip()})
        url = self._url(f'/api/sms/device/mqtt-config/?{q}')
        r = requests.get(url, headers=self._headers_json(api_key=api_key), timeout=_timeout_sec())
        data = self._parse_response(r)
        _LOGGER.info('hiDisheLink mqtt-config device_id=%s http_status=%s', device_id, r.status_code)
        return data

    def get_device_status(self, *, device_id: str, api_key: str) -> dict[str, Any]:
        """GET /api/sms/device/status/?device_id=…"""
        q = urlencode({'device_id': device_id.strip()})
        url = self._url(f'/api/sms/device/status/?{q}')
        r = requests.get(url, headers=self._headers_json(api_key=api_key), timeout=_timeout_sec())
        data = self._parse_response(r)
        _LOGGER.info('hiDisheLink device-status device_id=%s http_status=%s', device_id, r.status_code)
        return data

    def _parse_response(self, r: requests.Response) -> dict[str, Any]:
        try:
            payload = r.json()
        except ValueError:
            payload = {'raw': r.text}

        if r.status_code >= 400:
            err_msg = ''
            if isinstance(payload, dict):
                err_msg = str(payload.get('error') or payload.get('detail') or payload.get('message') or '')
            raise HiDishelinkApiError(
                err_msg or f'HTTP {r.status_code}',
                status_code=r.status_code,
                body=payload,
            )

        if isinstance(payload, dict) and payload.get('success') is False:
            raise HiDishelinkApiError(
                str(payload.get('error') or 'success=false'),
                status_code=r.status_code,
                body=payload,
            )

        return _normalize_json(payload)


def mqtt_config_flat(api_payload: dict[str, Any]) -> dict[str, Any]:
    """Return flat MQTT/device settings dict from API root or nested ``data``."""
    inner = api_payload.get('data')
    if isinstance(inner, dict):
        return dict(inner)
    return dict(api_payload)


def quote_device_id_for_query(device_id: str) -> str:
    """Return path-safe quoted device id for documentation URLs (+ → %2B)."""
    return quote(device_id, safe='')
