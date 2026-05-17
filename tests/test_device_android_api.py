"""Tests for `/api/sms/device/` Android-compatible REST API."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from apps.external_device.authentication import hash_api_key
from apps.external_device.models import DeviceSession, ExternalDevice
from apps.external_device.services import generate_token, hash_token


@pytest.fixture
def android_api_client():
    return APIClient()


@pytest.mark.django_db
class TestAndroidDeviceRegisterLogin:
    def test_register_envelope_success(self, android_api_client):
        raw_token = generate_token(24)
        ExternalDevice.objects.create(
            device_id='+351910000099',
            name='pending placeholder',
            registration_token_hash=hash_token(raw_token),
            registration_token_expires_at=timezone.now() + timedelta(hours=24),
            status=ExternalDevice.Status.PENDING,
        )
        resp = android_api_client.post(
            '/api/sms/device/register/',
            {
                'device_id': '+351910000099',
                'registration_token': raw_token,
                'device_model': 'Pixel',
                'app_version': '2.1',
            },
            format='json',
        )
        assert resp.status_code == 200
        assert resp.data['success'] is True
        assert resp.data['error'] is None
        assert resp.data['data']['device_id'] == '+351910000099'
        assert resp.data['data']['status'] == ExternalDevice.Status.ACTIVE
        assert len(resp.data['data']['api_key']) > 20

    def test_login_returns_session_iso8601(self, android_api_client):
        raw_key = generate_token(32)
        ExternalDevice.objects.create(
            device_id='+351910000088',
            name='Active',
            api_key_hash=hash_api_key(raw_key),
            status=ExternalDevice.Status.ACTIVE,
        )
        resp = android_api_client.post(
            '/api/sms/device/login/',
            {'device_id': '+351910000088', 'api_key': raw_key},
            format='json',
        )
        assert resp.status_code == 200
        assert resp.data['success'] is True
        assert resp.data['data']['session_id'].startswith('s_')
        exp = resp.data['data']['expires_at']
        assert 'T' in exp and ('+' in exp or exp.endswith('Z'))

    def test_login_pending_returns_403(self, android_api_client):
        ExternalDevice.objects.create(
            device_id='+351910000077',
            name='Wait',
            status=ExternalDevice.Status.PENDING,
        )
        resp = android_api_client.post(
            '/api/sms/device/login/',
            {'device_id': '+351910000077', 'api_key': 'any'},
            format='json',
        )
        assert resp.status_code == 403
        assert resp.data['success'] is False

    def test_refresh_extends_session(self, android_api_client):
        raw_key = generate_token(32)
        dev = ExternalDevice.objects.create(
            device_id='+351910000066',
            name='Active',
            api_key_hash=hash_api_key(raw_key),
            status=ExternalDevice.Status.ACTIVE,
        )
        login_r = android_api_client.post(
            '/api/sms/device/login/',
            {'device_id': dev.device_id, 'api_key': raw_key},
            format='json',
        )
        sid = login_r.data['data']['session_id']
        refresh_r = android_api_client.post(
            '/api/sms/device/refresh/',
            {'device_id': dev.device_id, 'session_id': sid},
            format='json',
        )
        assert refresh_r.status_code == 200
        assert refresh_r.data['data']['extended'] is True

    def test_logout_revokes_session(self, android_api_client):
        raw_key = generate_token(32)
        dev = ExternalDevice.objects.create(
            device_id='+351910000055',
            name='Active',
            api_key_hash=hash_api_key(raw_key),
            status=ExternalDevice.Status.ACTIVE,
        )
        login_r = android_api_client.post(
            '/api/sms/device/login/',
            {'device_id': dev.device_id, 'api_key': raw_key},
            format='json',
        )
        sid = login_r.data['data']['session_id']
        out_r = android_api_client.post(
            '/api/sms/device/logout/',
            {'device_id': dev.device_id, 'session_id': sid},
            format='json',
        )
        assert out_r.status_code == 200
        sess = DeviceSession.objects.get(session_id=sid)
        assert sess.revoked_at is not None


@pytest.mark.django_db
class TestAndroidDeviceStatusMqttConfig:
    def test_status_active_sessions_and_names(self, android_api_client):
        raw_key = generate_token(32)
        dev = ExternalDevice.objects.create(
            device_id='+351910000044',
            name='Display Name',
            api_key_hash=hash_api_key(raw_key),
            status=ExternalDevice.Status.ACTIVE,
        )
        DeviceSession.objects.create(
            session_id='s_test_sess123456789012345678901234567890',
            device=dev,
            expires_at=timezone.now() + timedelta(hours=1),
        )
        resp = android_api_client.get(
            '/api/sms/device/status/',
            {'device_id': dev.device_id},
            HTTP_X_API_KEY=raw_key,
        )
        assert resp.status_code == 200
        assert resp.data['data']['name'] == 'Display Name'
        assert resp.data['data']['device_name'] == 'Display Name'
        assert resp.data['data']['active_sessions'] >= 1

    @patch('apps.external_device.device_api_views.fetch_mqtt_config_from_hidishelink')
    def test_mqtt_config_returns_remote_payload(self, mock_fetch, android_api_client):
        raw_key = generate_token(32)
        ExternalDevice.objects.create(
            device_id='+351910000033',
            name='MQTT dev',
            api_key_hash=hash_api_key(raw_key),
            status=ExternalDevice.Status.ACTIVE,
        )
        remote = {
            'MQTT_BROKER_URL': 'mqtt.example.com',
            'MQTT_PORT': 43827,
            'MQTT_USERNAME': 'u',
            'MQTT_PASSWORD': 'p',
            'MQTT_KEEPALIVE': 60,
            'MQTT_QOS': 1,
            'MQTT_CLEAN_SESSION': False,
            'MQTT_AUTO_RECONNECT': True,
            'MQTT_CONNECTION_TIMEOUT': 30,
            'MQTT_RECONNECT_INITIAL_DELAY_MS': 2000,
            'MQTT_RECONNECT_MAX_DELAY_MS': 60000,
            'MQTT_RECONNECT_BACKOFF_MULTIPLIER': 2,
            'MQTT_RECONNECT_MAX_RETRIES': 0,
            'MQTT_CONNECTION_WATCHDOG_INTERVAL_MS': 20000,
            'SMS_INBOX_ENABLED': True,
            'TOPIC_SMS_SEND': 'hidishelink_dev/devices/{device_id}/sms/send',
            'TOPIC_HEALTH_PING': 'hidishelink_dev/devices/{device_id}/health/ping',
        }
        mock_fetch.return_value = remote

        resp = android_api_client.get(
            '/api/sms/device/mqtt-config/',
            {'device_id': '+351910000033'},
            HTTP_X_API_KEY=raw_key,
        )
        assert resp.status_code == 200
        assert resp.data['success'] is True
        assert resp.data['data'] == remote
        mock_fetch.assert_called_once_with(device_id='+351910000033', request_api_key=raw_key)
        assert '{device_id}' in resp.data['data']['TOPIC_HEALTH_PING']
        assert resp.data['data']['TOPIC_HEALTH_PING'].endswith('/health/ping')

    @patch('apps.external_device.device_api_views.fetch_mqtt_config_from_hidishelink')
    def test_mqtt_config_remote_error_502_when_upstream_http_error(self, mock_fetch, android_api_client):
        from apps.external_device.hidishelink_client import HiDishelinkApiError

        raw_key = generate_token(32)
        ExternalDevice.objects.create(
            device_id='+351910000099',
            name='MQTT err',
            api_key_hash=hash_api_key(raw_key),
            status=ExternalDevice.Status.ACTIVE,
        )
        mock_fetch.side_effect = HiDishelinkApiError('forbidden', status_code=403)

        resp = android_api_client.get(
            '/api/sms/device/mqtt-config/',
            {'device_id': '+351910000099'},
            HTTP_X_API_KEY=raw_key,
        )
        assert resp.status_code == 502
        assert resp.data['success'] is False
        assert resp.data['error'] == 'forbidden'

    @patch('apps.external_device.device_api_views.fetch_mqtt_config_from_hidishelink')
    def test_mqtt_config_remote_error_503_when_unreachable(self, mock_fetch, android_api_client):
        from apps.external_device.hidishelink_client import HiDishelinkApiError

        raw_key = generate_token(32)
        ExternalDevice.objects.create(
            device_id='+351910000098',
            name='MQTT unreachable',
            api_key_hash=hash_api_key(raw_key),
            status=ExternalDevice.Status.ACTIVE,
        )
        mock_fetch.side_effect = HiDishelinkApiError('timeout', status_code=None)

        resp = android_api_client.get(
            '/api/sms/device/mqtt-config/',
            {'device_id': '+351910000098'},
            HTTP_X_API_KEY=raw_key,
        )
        assert resp.status_code == 503
        assert resp.data['success'] is False


@pytest.mark.django_db
class TestAndroidGetPendingKey:
    def test_get_pending_key_404_unknown_device(self, android_api_client):
        r = android_api_client.post('/api/sms/device/get-pending-key/', {'device_id': '+399999'}, format='json')
        assert r.status_code == 404

    def test_get_pending_key_403_when_active(self, android_api_client):
        ExternalDevice.objects.create(
            device_id='+351910000022',
            name='Already',
            api_key_hash='abcd',
            status=ExternalDevice.Status.ACTIVE,
        )
        r = android_api_client.post(
            '/api/sms/device/get-pending-key/',
            {'device_id': '+351910000022'},
            format='json',
        )
        assert r.status_code == 403

    def test_get_pending_key_success(self, android_api_client):
        ExternalDevice.objects.create(
            device_id='+351910000011',
            name='Pending user',
            status=ExternalDevice.Status.PENDING,
            api_key_hash='',
        )
        r = android_api_client.post(
            '/api/sms/device/get-pending-key/',
            {'device_id': '+351910000011'},
            format='json',
        )
        assert r.status_code == 200
        assert r.data['success'] is True
        assert len(r.data['data']['api_key']) > 20
        dev = ExternalDevice.objects.get(pk='+351910000011')
        assert dev.status == ExternalDevice.Status.ACTIVE
