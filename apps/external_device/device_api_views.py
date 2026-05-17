"""REST endpoints `/api/sms/device/*` for Android hiDisheLink SMS client."""

from __future__ import annotations

import logging
from typing import Any

from rest_framework.exceptions import ValidationError
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from .device_android_sessions import (
    active_sessions_count,
    authenticate_device_login,
    create_device_session,
    issue_pending_device_api_key,
    refresh_device_session,
    register_device_android_payload,
    revoke_device_session,
)
from .hidishelink_client import HiDishelinkApiError
from .hidishelink_response import iso8601_offset
from .models import ExternalDevice
from .mqtt_config_remote import fetch_mqtt_config_from_hidishelink

_LOGGER = logging.getLogger(__name__)


def _verify_active_api_key(device_id: str, api_key: str) -> ExternalDevice | None:
    device, err = authenticate_device_login(device_id, api_key)
    if device is None:
        return None
    return device


class DeviceRegisterAndroidView(APIView):
    authentication_classes = []
    permission_classes = []

    def post(self, request: Request) -> Response:
        try:
            device, raw_key = register_device_android_payload(dict(request.data))
        except ValidationError as exc:
            err = getattr(exc, 'detail', exc)
            msg = ''
            if isinstance(err, dict):
                msg = str(err.get('error') or err.get('detail') or err)
            else:
                msg = str(err)
            return Response({'success': False, 'data': None, 'error': msg}, status=400)

        data: dict[str, Any] = {
            'device_id': device.device_id,
            'api_key': raw_key,
            'status': device.status,
            'message': 'Registered successfully.',
        }
        return Response({'success': True, 'data': data, 'error': None}, status=200)


class DeviceLoginAndroidView(APIView):
    authentication_classes = []
    permission_classes = []

    def post(self, request: Request) -> Response:
        body = dict(request.data)
        device_id = str(body.get('device_id') or '').strip()
        api_key = str(body.get('api_key') or '').strip()

        device, err = authenticate_device_login(device_id, api_key)
        if err == 'pending':
            return Response(
                {'success': False, 'data': None, 'error': 'Device pending administrator activation.'},
                status=403,
            )
        if err == 'inactive':
            return Response(
                {'success': False, 'data': None, 'error': 'Device not authorized to login.'},
                status=403,
            )
        if device is None:
            return Response(
                {'success': False, 'data': None, 'error': 'Invalid device_id or api_key.'},
                status=401,
            )

        sess = create_device_session(device)
        data = {
            'device_id': device.device_id,
            'session_id': sess.session_id,
            'expires_at': iso8601_offset(sess.expires_at),
            'status': device.status,
        }
        return Response({'success': True, 'data': data, 'error': None}, status=200)


class DeviceRefreshAndroidView(APIView):
    authentication_classes = []
    permission_classes = []

    def post(self, request: Request) -> Response:
        body = dict(request.data)
        device_id = str(body.get('device_id') or '').strip()
        session_id = str(body.get('session_id') or '').strip()

        sess, err_msg = refresh_device_session(device_id, session_id)
        if sess is None:
            return Response({'success': False, 'data': None, 'error': err_msg or 'Refresh failed.'}, status=401)

        data = {
            'session_id': sess.session_id,
            'expires_at': iso8601_offset(sess.expires_at),
            'extended': True,
        }
        return Response({'success': True, 'data': data, 'error': None}, status=200)


class DeviceLogoutAndroidView(APIView):
    authentication_classes = []
    permission_classes = []

    def post(self, request: Request) -> Response:
        body = dict(request.data)
        device_id = str(body.get('device_id') or '').strip()
        session_id = str(body.get('session_id') or '').strip()

        if revoke_device_session(device_id, session_id):
            return Response(
                {'success': True, 'data': {'message': 'Session terminated.'}, 'error': None},
                status=200,
            )
        return Response({'success': False, 'data': None, 'error': 'Invalid session.'}, status=401)


class DeviceStatusAndroidView(APIView):
    authentication_classes = []
    permission_classes = []

    def get(self, request: Request) -> Response:
        device_id = str(request.query_params.get('device_id') or '').strip()
        raw_key = str(request.META.get('HTTP_X_API_KEY') or '').strip()

        if not device_id or not raw_key:
            return Response(
                {'success': False, 'data': None, 'error': 'device_id and X-API-Key are required.'},
                status=400,
            )

        device = _verify_active_api_key(device_id, raw_key)
        if device is None:
            return Response({'success': False, 'data': None, 'error': 'Unauthorized.'}, status=401)

        last_seen = iso8601_offset(device.last_seen) if device.last_seen else None
        registered_at = iso8601_offset(device.created_at) if device.created_at else None

        data = {
            'device_id': device.device_id,
            'name': device.name,
            'device_name': device.name,
            'status': device.status,
            'last_seen': last_seen,
            'active_sessions': active_sessions_count(device),
            'registered_at': registered_at,
        }
        return Response({'success': True, 'data': data, 'error': None}, status=200)


class DeviceMqttConfigAndroidView(APIView):
    authentication_classes = []
    permission_classes = []

    def get(self, request: Request) -> Response:
        device_id = str(request.query_params.get('device_id') or '').strip()
        raw_key = str(request.META.get('HTTP_X_API_KEY') or '').strip()

        if not device_id or not raw_key:
            return Response(
                {'success': False, 'data': None, 'error': 'device_id and X-API-Key are required.'},
                status=400,
            )

        device = _verify_active_api_key(device_id, raw_key)
        if device is None:
            return Response({'success': False, 'data': None, 'error': 'Unauthorized.'}, status=401)

        try:
            cfg = fetch_mqtt_config_from_hidishelink(device_id=device.device_id, request_api_key=raw_key)
        except HiDishelinkApiError as exc:
            http_status = 503 if exc.status_code is None else 502
            msg = str(exc).strip() or 'Remote mqtt-config failed.'
            _LOGGER.warning(
                'mqtt-config proxy failed device_id=%s status=%s err=%s',
                device.device_id,
                getattr(exc, 'status_code', None),
                msg,
            )
            return Response({'success': False, 'data': None, 'error': msg}, status=http_status)

        device.mark_seen()
        return Response({'success': True, 'data': cfg, 'error': None}, status=200)


class DeviceGetPendingKeyAndroidView(APIView):
    authentication_classes = []
    permission_classes = []

    def post(self, request: Request) -> Response:
        device_id = str(dict(request.data).get('device_id') or '').strip()
        if not device_id:
            return Response({'success': False, 'data': None, 'error': 'device_id is required.'}, status=400)

        device, outcome = issue_pending_device_api_key(device_id)

        if outcome == 'not_found':
            return Response({'success': False, 'data': None, 'error': 'Device not found.'}, status=404)

        if outcome == 'not_pending':
            return Response(
                {'success': False, 'data': None, 'error': 'Device is not pending key issuance.'},
                status=403,
            )

        assert device is not None
        raw_key = getattr(device, '_pending_raw_api_key', None)
        if not raw_key:
            _LOGGER.error('issue_pending_device_api_key missing raw key device=%s', device.device_id)
            return Response({'success': False, 'data': None, 'error': 'Internal error.'}, status=500)

        data = {
            'device_id': device.device_id,
            'name': device.name,
            'api_key': raw_key,
            'status': device.status,
            'message': 'API key issued.',
        }
        return Response({'success': True, 'data': data, 'error': None}, status=200)

