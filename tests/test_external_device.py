"""Tests for external device gateway: registration, auth, REST API, MQTT handlers."""

from __future__ import annotations

import json
from datetime import timedelta
from unittest.mock import Mock, patch

import pytest
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework.test import APIClient

from apps.external_device.admin import InboxMessageAdmin
from apps.external_device.authentication import ApiKeyAuthentication, hash_api_key
from apps.external_device.models import (
    DeviceHealthTelemetry,
    DeviceSession,
    ExternalDevice,
    HiDishelinkDevice,
    InboxMessage,
    MqttGatewayCatalogEntry,
    SmsRecipientStatus,
    SmsRequest,
)
from apps.external_device.mqtt_client import GatewayMqttClient, sanitize_device_id
from apps.external_device.services import (
    delete_external_device_and_dependencies,
    generate_token,
    hash_token,
    persist_inbox_from_mqtt,
    persist_modem_catalog_from_mqtt,
    process_sms_request,
    register_device,
    sync_single_inbound_to_all_devices,
    update_request_from_mqtt_status,
)
from apps.sms.models import InboundSms, OutboundSms


@pytest.fixture
def api_client():
    """DRF API client."""
    return APIClient()


@pytest.fixture
def admin_client(db):
    """Logged-in Django admin user."""
    User = get_user_model()
    user = User.objects.create_superuser(
        username='adm_ext_dev',
        email='adm-ext@test.invalid',
        password='pw-test-admin-ext-dev',
    )
    client = Client()
    client.force_login(user)
    return client


@pytest.fixture
def pending_device():
    """Create a pending device with registration token."""
    raw_token = generate_token(32)
    token_hash = hash_token(raw_token)
    expires_at = timezone.now() + timedelta(hours=24)

    device = ExternalDevice.objects.create(
        device_id='+351913000001',
        name='Test Device',
        device_type='modem',
        registration_token_hash=token_hash,
        registration_token_expires_at=expires_at,
        status=ExternalDevice.Status.PENDING,
    )
    device.raw_token = raw_token
    return device


@pytest.fixture
def active_device():
    """Create an active device with API key."""
    raw_api_key = generate_token(48)
    api_key_hash = hash_api_key(raw_api_key)

    device = ExternalDevice.objects.create(
        device_id='+351913000002',
        name='Active Device',
        device_type='modem',
        api_key_hash=api_key_hash,
        status=ExternalDevice.Status.ACTIVE,
    )
    device.raw_api_key = raw_api_key
    return device


@pytest.fixture
def sms_request_obj(active_device):
    """Create an SMS request."""
    return SmsRequest.objects.create(
        request_id='sms_test123',
        device=active_device,
        recipients=['+351912345678', '+351987654321'],
        message='Test message',
        status=SmsRequest.Status.COMPLETED,
        sent_count=2,
        failed_count=0,
    )


@pytest.mark.django_db
class TestRegistration:
    """Test device registration flow."""

    def test_register_with_valid_token(self, api_client, pending_device):
        """Register device with valid token returns API key."""
        data = {
            'device_id': pending_device.device_id,
            'registration_token': pending_device.raw_token,
            'name': 'Updated Name',
            'device_type': 'modem',
        }
        response = api_client.post('/api/v1/external-devices/register/', data, format='json')

        assert response.status_code == 200
        assert 'api_key' in response.data
        assert response.data['device_id'] == pending_device.device_id
        assert response.data['status'] == 'active'

        pending_device.refresh_from_db()
        assert pending_device.status == ExternalDevice.Status.ACTIVE
        assert pending_device.name == 'Updated Name'
        assert pending_device.api_key_hash != ''

    def test_register_creates_device_when_not_precreated(self, api_client):
        """Registration auto-creates first-time device and activates it."""
        data = {
            'device_id': '+351913000387',
            'registration_token': 'token_admin_uma_vez',
            'name': 'Modem site A',
            'device_type': 'modem',
            'mqtt_client_id': 'meu_cliente_mqtt_opcional',
            'metadata': {'site': 'Lisboa'},
        }
        response = api_client.post('/api/v1/external-devices/register/', data, format='json')

        assert response.status_code == 200
        assert response.data['device_id'] == data['device_id']
        assert response.data['status'] == 'active'
        assert 'api_key' in response.data

        created = ExternalDevice.objects.get(device_id=data['device_id'])
        assert created.status == ExternalDevice.Status.ACTIVE
        assert created.name == data['name']
        assert created.mqtt_client_id == data['mqtt_client_id']

    def test_register_with_expired_token(self, api_client):
        """Registration with expired token fails."""
        raw_token = generate_token(32)
        token_hash = hash_token(raw_token)
        expires_at = timezone.now() - timedelta(hours=1)

        device = ExternalDevice.objects.create(
            device_id='+351913000003',
            name='Expired Device',
            registration_token_hash=token_hash,
            registration_token_expires_at=expires_at,
            status=ExternalDevice.Status.PENDING,
        )

        data = {
            'device_id': device.device_id,
            'registration_token': raw_token,
            'name': 'Updated Name',
        }
        response = api_client.post('/api/v1/external-devices/register/', data, format='json')

        assert response.status_code == 400
        assert 'expired' in str(response.data).lower()

    def test_register_with_invalid_token(self, api_client, pending_device):
        """Registration with invalid token fails."""
        data = {
            'device_id': pending_device.device_id,
            'registration_token': 'invalid_token',
            'name': 'Updated Name',
        }
        response = api_client.post('/api/v1/external-devices/register/', data, format='json')

        assert response.status_code == 400
        assert 'invalid' in str(response.data).lower()


@pytest.mark.django_db
class TestAuthentication:
    """Test API key authentication."""

    def test_authenticate_with_valid_key(self, api_client, active_device):
        """Valid API key authenticates successfully."""
        api_client.credentials(HTTP_AUTHORIZATION=f'ApiKey {active_device.raw_api_key}')
        response = api_client.get(f'/api/v1/external-devices/{active_device.device_id}/health/')

        assert response.status_code == 200
        assert response.data['device_id'] == active_device.device_id

    def test_authenticate_with_x_api_key_header(self, api_client, active_device):
        """X-API-Key header authenticates successfully."""
        api_client.credentials(HTTP_X_API_KEY=active_device.raw_api_key)
        response = api_client.get(f'/api/v1/external-devices/{active_device.device_id}/health/')

        assert response.status_code == 200

    def test_authenticate_with_invalid_key(self, api_client):
        """Invalid API key fails authentication."""
        api_client.credentials(HTTP_AUTHORIZATION='ApiKey invalid_key')
        response = api_client.get('/api/v1/external-devices/+351913000002/health/')

        assert response.status_code in [401, 403]

    def test_authenticate_without_key(self, api_client):
        """No API key fails authentication."""
        response = api_client.get('/api/v1/external-devices/+351913000002/health/')

        assert response.status_code in [401, 403]


@pytest.mark.django_db
class TestSmsSend:
    """Test SMS send endpoint."""

    @patch('apps.external_device.services.dispatch_outbound_mmcli')
    def test_send_sms_success(self, mock_dispatch, api_client, active_device):
        """Send SMS request succeeds."""
        def mock_dispatch_impl(outbound):
            outbound.state = OutboundSms.State.SENT
            outbound.save()
            return outbound

        mock_dispatch.side_effect = mock_dispatch_impl

        api_client.credentials(HTTP_AUTHORIZATION=f'ApiKey {active_device.raw_api_key}')
        data = {
            'recipients': ['+351912345678'],
            'message': 'Test message',
            'priority': 'normal',
        }
        response = api_client.post('/api/v1/sms/send/', data, format='json')

        assert response.status_code == 202
        assert 'request_id' in response.data
        assert response.data['status'] in ['completed', 'processing']

    def test_send_sms_without_auth(self, api_client):
        """Send SMS without auth fails."""
        data = {
            'recipients': ['+351912345678'],
            'message': 'Test message',
        }
        response = api_client.post('/api/v1/sms/send/', data, format='json')

        assert response.status_code in [401, 403]

    @patch('apps.external_device.services.dispatch_outbound_mmcli')
    def test_send_sms_too_many_recipients(self, mock_dispatch, api_client, active_device):
        """Send SMS with too many recipients fails."""
        active_device.max_recipients_per_request = 2
        active_device.save()

        api_client.credentials(HTTP_AUTHORIZATION=f'ApiKey {active_device.raw_api_key}')
        data = {
            'recipients': ['+351912345678', '+351987654321', '+351911111111'],
            'message': 'Test message',
        }
        response = api_client.post('/api/v1/sms/send/', data, format='json')

        assert response.status_code == 400
        assert 'too many' in str(response.data).lower()


@pytest.mark.django_db
class TestProcessSmsRequestModemIndex:
    """Outbound rows must use the same modem index convention as the JWT SMS API."""

    @patch('apps.external_device.services.dispatch_outbound_mmcli')
    def test_outbound_uses_device_metadata_modem_index(self, mock_dispatch, active_device):
        def impl(ob):
            ob.state = OutboundSms.State.SENT
            ob.save()
            return ob

        mock_dispatch.side_effect = impl
        active_device.metadata = {'modem_index': 5}
        active_device.save(update_fields=['metadata'])
        process_sms_request(active_device, ['+351913000111'], 'hello')
        assert OutboundSms.objects.latest('pk').modem_index == 5

    @patch('apps.sms.mmcli_client.resolve_modem_mmcli_index', side_effect=lambda ix, **_: ix)
    @patch('apps.external_device.services.dispatch_outbound_mmcli')
    @override_settings(MODEM_MMCLI_INDEX=8)
    def test_outbound_falls_back_to_settings_modem_index(self, mock_dispatch, _mock_resolve, active_device):
        def impl(ob):
            ob.state = OutboundSms.State.SENT
            ob.save()
            return ob

        mock_dispatch.side_effect = impl
        active_device.metadata = {}
        active_device.save(update_fields=['metadata'])
        process_sms_request(active_device, ['+351913000222'], 'hello')
        assert OutboundSms.objects.latest('pk').modem_index == 8

    @patch('apps.sms.mmcli_client.resolve_modem_mmcli_index', side_effect=lambda ix, **_: ix)
    @patch('apps.external_device.services.dispatch_outbound_mmcli')
    @override_settings(MODEM_MMCLI_INDEX=3)
    def test_invalid_metadata_modem_index_falls_back_to_settings(self, mock_dispatch, _mock_resolve, active_device):
        def impl(ob):
            ob.state = OutboundSms.State.SENT
            ob.save()
            return ob

        mock_dispatch.side_effect = impl
        active_device.metadata = {'modem_index': 'not-int'}
        active_device.save(update_fields=['metadata'])
        process_sms_request(active_device, ['+351913000333'], 'hello')
        assert OutboundSms.objects.latest('pk').modem_index == 3


@pytest.mark.django_db
class TestSmsStatus:
    """Test SMS status endpoint."""

    def test_get_status_success(self, api_client, active_device, sms_request_obj):
        """Get SMS status returns correct data."""
        SmsRecipientStatus.objects.create(
            request=sms_request_obj,
            phone_number='+351912345678',
            status=SmsRecipientStatus.Status.SENT,
        )

        api_client.credentials(HTTP_AUTHORIZATION=f'ApiKey {active_device.raw_api_key}')
        response = api_client.get(f'/api/v1/sms/status/?request_id={sms_request_obj.request_id}')

        assert response.status_code == 200
        assert response.data['request_id'] == sms_request_obj.request_id
        assert response.data['status'] == 'completed'
        assert len(response.data['recipients']) == 1

    def test_get_status_not_found(self, api_client, active_device):
        """Get status for non-existent request returns 404."""
        api_client.credentials(HTTP_AUTHORIZATION=f'ApiKey {active_device.raw_api_key}')
        response = api_client.get('/api/v1/sms/status/?request_id=invalid_id')

        assert response.status_code == 404

    def test_get_status_missing_request_id(self, api_client, active_device):
        """Get status without request_id returns 400."""
        api_client.credentials(HTTP_AUTHORIZATION=f'ApiKey {active_device.raw_api_key}')
        response = api_client.get('/api/v1/sms/status/')

        assert response.status_code == 400


@pytest.mark.django_db
class TestSmsInbox:
    """Test SMS inbox endpoint."""

    def test_list_inbox_messages(self, api_client, active_device):
        """List inbox messages returns device's messages."""
        InboxMessage.objects.create(
            message_id='inbox_001',
            device=active_device,
            sender='+351911111111',
            body='Test inbox message',
            received_at=timezone.now(),
        )

        api_client.credentials(HTTP_AUTHORIZATION=f'ApiKey {active_device.raw_api_key}')
        response = api_client.get('/api/v1/sms/inbox/')

        assert response.status_code == 200
        assert len(response.data['results']) == 1
        assert response.data['results'][0]['message_id'] == 'inbox_001'

    def test_list_inbox_empty(self, api_client, active_device):
        """List inbox with no messages returns empty list."""
        api_client.credentials(HTTP_AUTHORIZATION=f'ApiKey {active_device.raw_api_key}')
        response = api_client.get('/api/v1/sms/inbox/')

        assert response.status_code == 200
        assert len(response.data['results']) == 0

    def test_list_inbox_includes_modem_inbound_messages(self, api_client, active_device):
        """Inbox endpoint mirrors inbound rows from apps.sms.InboundSms when present."""
        InboundSms.objects.create(
            mm_path='/org/freedesktop/ModemManager1/SMS/77',
            modem_index=0,
            from_number='+351911111111',
            text='Incoming from modem watcher',
        )
        api_client.credentials(HTTP_AUTHORIZATION=f'ApiKey {active_device.raw_api_key}')
        response = api_client.get('/api/v1/sms/inbox/')

        assert response.status_code == 200
        assert response.data['count'] >= 1
        assert response.data['results'][0]['sender'] == '+351911111111'


@pytest.mark.django_db
class TestDeviceHealth:
    """Test device health endpoint."""

    def test_get_health_success(self, api_client, active_device):
        """Get device health returns correct data."""
        api_client.credentials(HTTP_AUTHORIZATION=f'ApiKey {active_device.raw_api_key}')
        response = api_client.get(f'/api/v1/external-devices/{active_device.device_id}/health/')

        assert response.status_code == 200
        assert response.data['device_id'] == active_device.device_id
        assert response.data['status'] == 'active'
        assert 'is_available' in response.data

    @patch('apps.external_device.views.publish_health_ping_ephemeral')
    def test_post_health_ping_publishes_via_api(self, mock_ping, api_client, active_device):
        mock_ping.return_value = (
            {'ping_id': 'ping_deadbeef012', 'timestamp': '2026-05-17T12:00:00+00:00', 'source': 'django'},
            True,
            'pfx/devices/351913000002/health/ping',
        )
        api_client.credentials(HTTP_AUTHORIZATION=f'ApiKey {active_device.raw_api_key}')
        url = f'/api/v1/external-devices/{active_device.device_id}/health/ping/'
        response = api_client.post(url)

        assert response.status_code == 200
        assert response.data['ping_id'] == 'ping_deadbeef012'
        assert response.data['source'] == 'django'
        assert response.data['published'] is True
        assert response.data['mqtt_topic'] == 'pfx/devices/351913000002/health/ping'
        mock_ping.assert_called_once_with(active_device.device_id)

    def test_post_health_ping_device_mismatch_forbidden(self, api_client, active_device):
        api_client.credentials(HTTP_AUTHORIZATION=f'ApiKey {active_device.raw_api_key}')
        response = api_client.post('/api/v1/external-devices/+999/health/ping/')
        assert response.status_code == 403


@pytest.mark.django_db
class TestMqttHandlers:
    """Test MQTT message handlers."""

    def test_update_request_from_mqtt_status(self, sms_request_obj):
        """Update request from MQTT status message."""
        payload = {
            'status': 'success',
            'sent': 2,
            'failed': 0,
            'details': [
                {
                    'recipient': '+351912345678',
                    'status': 'sent',
                    'message_id': 'msg_001',
                },
            ],
        }

        update_request_from_mqtt_status(sms_request_obj.request_id, payload)

        sms_request_obj.refresh_from_db()
        assert sms_request_obj.status == SmsRequest.Status.COMPLETED
        assert sms_request_obj.sent_count == 2

    def test_update_request_from_mqtt_status_accepts_result_field(self, sms_request_obj):
        payload = {
            'result': 'SUCCESS',
            'sent': 2,
            'failed': 0,
            'details': [],
        }
        sms_request_obj.status = SmsRequest.Status.PROCESSING
        sms_request_obj.save(update_fields=['status'])

        update_request_from_mqtt_status(sms_request_obj.request_id, payload)

        sms_request_obj.refresh_from_db()
        assert sms_request_obj.status == SmsRequest.Status.COMPLETED

    def test_update_request_skips_duplicate_terminal_mqtt_update(self, active_device):
        req = SmsRequest.objects.create(
            request_id='sms_dup_terminal',
            device=active_device,
            recipients=['+1'],
            message='x',
            status=SmsRequest.Status.PROCESSING,
            sent_count=0,
            failed_count=0,
        )
        update_request_from_mqtt_status(
            req.request_id,
            {'status': 'success', 'sent': 1, 'failed': 0, 'details': []},
        )
        req.refresh_from_db()
        assert req.status == SmsRequest.Status.COMPLETED
        assert req.sent_count == 1

        update_request_from_mqtt_status(
            req.request_id,
            {'status': 'success', 'sent': 99, 'failed': 0, 'details': []},
        )
        req.refresh_from_db()
        assert req.sent_count == 1

    def test_persist_inbox_from_mqtt(self, active_device):
        """Persist inbox message from MQTT."""
        payload = {
            'message_id': 'inbox_mqtt_001',
            'sender': '+351911111111',
            'body': 'MQTT inbox test',
            'timestamp': timezone.now().isoformat(),
        }

        inbox_msg = persist_inbox_from_mqtt(active_device, payload)

        assert inbox_msg.message_id == 'inbox_mqtt_001'
        assert inbox_msg.sender == '+351911111111'
        assert inbox_msg.device == active_device

    def test_persist_inbox_from_mqtt_field_aliases(self, active_device):
        payload = {
            'id': 'alias_mid',
            'from': '+351900000000',
            'text': 'via text',
            'received_at': timezone.now().isoformat(),
        }
        inbox_msg = persist_inbox_from_mqtt(active_device, payload)
        assert inbox_msg.message_id == 'alias_mid'
        assert inbox_msg.sender == '+351900000000'
        assert inbox_msg.body == 'via text'

    def test_persist_modem_catalog_from_mqtt(self):
        row = persist_modem_catalog_from_mqtt('snapshot', {'modems': [{'idx': 0}]})
        assert isinstance(row, MqttGatewayCatalogEntry)
        assert row.kind == 'snapshot'
        assert row.payload['modems'][0]['idx'] == 0

    def test_sanitize_device_id(self):
        """Sanitize device ID removes + and # characters."""
        assert sanitize_device_id('+351913000001') == '351913000001'
        assert sanitize_device_id('#351913000001') == '351913000001'
        assert sanitize_device_id('+351#913000001') == '351913000001'

    def test_update_request_maps_received_to_processing(self, active_device):
        """status 'received' must map to PROCESSING (not a terminal state)."""
        req = SmsRequest.objects.create(
            request_id='sms_recv_test',
            device=active_device,
            recipients=['+351900000001'],
            message='hello',
            status=SmsRequest.Status.QUEUED,
        )
        update_request_from_mqtt_status(req.request_id, {'status': 'received', 'sent': 0, 'failed': 0, 'details': []})
        req.refresh_from_db()
        assert req.status == SmsRequest.Status.PROCESSING

    def test_update_request_maps_partial_and_error(self, active_device):
        """status 'partial' → PARTIAL; status 'error' → FAILED."""
        req_partial = SmsRequest.objects.create(
            request_id='sms_partial_test',
            device=active_device,
            recipients=['+1', '+2'],
            message='hi',
            status=SmsRequest.Status.PROCESSING,
        )
        update_request_from_mqtt_status(
            req_partial.request_id,
            {'status': 'partial', 'sent': 1, 'failed': 1, 'details': []},
        )
        req_partial.refresh_from_db()
        assert req_partial.status == SmsRequest.Status.PARTIAL
        assert req_partial.sent_count == 1
        assert req_partial.failed_count == 1

        req_error = SmsRequest.objects.create(
            request_id='sms_error_test',
            device=active_device,
            recipients=['+1'],
            message='hi',
            status=SmsRequest.Status.PROCESSING,
        )
        update_request_from_mqtt_status(
            req_error.request_id,
            {'status': 'error', 'sent': 0, 'failed': 1, 'details': []},
        )
        req_error.refresh_from_db()
        assert req_error.status == SmsRequest.Status.FAILED

    def test_update_request_detail_uses_error_key(self, active_device):
        """details[].error (hiDisheLink spec key) must be stored in SmsRecipientStatus.error_message."""
        req = SmsRequest.objects.create(
            request_id='sms_err_key',
            device=active_device,
            recipients=['+351900000099'],
            message='x',
            status=SmsRequest.Status.PROCESSING,
        )
        update_request_from_mqtt_status(req.request_id, {
            'status': 'error',
            'sent': 0,
            'failed': 1,
            'details': [{'recipient': '+351900000099', 'status': 'failed', 'message_id': '', 'error': 'SMS permission not granted'}],
        })
        rs = SmsRecipientStatus.objects.get(request=req, phone_number='+351900000099')
        assert rs.error_message == 'SMS permission not granted'

    def test_update_request_detail_falls_back_to_error_message(self, active_device):
        """Legacy clients that send error_message (not error) must still persist correctly."""
        req = SmsRequest.objects.create(
            request_id='sms_err_msg_legacy',
            device=active_device,
            recipients=['+351900000088'],
            message='x',
            status=SmsRequest.Status.PROCESSING,
        )
        update_request_from_mqtt_status(req.request_id, {
            'status': 'error',
            'sent': 0,
            'failed': 1,
            'details': [{'recipient': '+351900000088', 'status': 'failed', 'message_id': '', 'error_message': 'no signal'}],
        })
        rs = SmsRecipientStatus.objects.get(request=req, phone_number='+351900000088')
        assert rs.error_message == 'no signal'

    def test_update_request_skips_empty_recipient_in_details(self, active_device):
        """Details rows with empty/missing recipient must not create SmsRecipientStatus rows."""
        req = SmsRequest.objects.create(
            request_id='sms_skip_empty',
            device=active_device,
            recipients=['+1'],
            message='hi',
            status=SmsRequest.Status.PROCESSING,
        )
        update_request_from_mqtt_status(req.request_id, {
            'status': 'error',
            'sent': 0,
            'failed': 1,
            'details': [{'recipient': '', 'status': 'failed', 'error': 'SMS permission not granted'}],
        })
        assert SmsRecipientStatus.objects.filter(request=req).count() == 0

    def test_update_request_unknown_request_id_noop(self):
        """Non-existent request_id must return silently without raising."""
        update_request_from_mqtt_status('sms_does_not_exist', {'status': 'success', 'sent': 1, 'failed': 0, 'details': []})

    def test_persist_inbox_dedup_by_message_id(self, active_device):
        """Second call with the same message_id must not create a duplicate InboxMessage row."""
        payload = {
            'message_id': 'dedup_001',
            'sender': '+351911000000',
            'body': 'first body',
            'timestamp': timezone.now().isoformat(),
        }
        msg1 = persist_inbox_from_mqtt(active_device, payload)
        msg2 = persist_inbox_from_mqtt(active_device, {**payload, 'body': 'second body'})
        assert msg1.pk == msg2.pk
        assert InboxMessage.objects.filter(message_id='dedup_001').count() == 1
        msg1.refresh_from_db()
        assert msg1.body == 'first body'


@pytest.mark.django_db
class TestServices:
    """Test service layer functions."""

    def test_generate_token(self):
        """Generate token returns string of correct length."""
        token = generate_token(48)
        assert isinstance(token, str)
        assert len(token) > 0

    def test_hash_token(self):
        """Hash token returns 64-character hex string."""
        token = 'test_token'
        hashed = hash_token(token)
        assert len(hashed) == 64
        assert all(c in '0123456789abcdef' for c in hashed)

    @patch('apps.external_device.mqtt_client.publish_send_request_ephemeral')
    @patch('apps.external_device.services.dispatch_outbound_mmcli')
    def test_process_sms_request(self, mock_dispatch, mock_mqtt_publish, active_device):
        """Process SMS request creates request and dispatches to modem."""
        def mock_dispatch_impl(outbound):
            outbound.state = OutboundSms.State.SENT
            outbound.save()
            return outbound

        mock_dispatch.side_effect = mock_dispatch_impl

        sms_request = process_sms_request(
            device=active_device,
            recipients=['+351912345678'],
            message='Test message',
            priority='normal',
        )

        assert sms_request.device == active_device
        assert sms_request.status == SmsRequest.Status.COMPLETED
        assert sms_request.sent_count == 1
        assert sms_request.failed_count == 0

        mock_mqtt_publish.assert_called_once()
        call_args = mock_mqtt_publish.call_args[0]
        assert call_args[0] == active_device.device_id
        payload = call_args[1]
        assert payload['request_id'] == sms_request.request_id
        assert payload['recipients'] == ['+351912345678']
        assert payload['message'] == 'Test message'
        assert payload['priority'] == 'normal'
        assert 'timestamp' in payload

    @override_settings(MQTT_PUBLISH_SEND_REQUEST=True, MQTT_SEND_RECIPIENTS_CHUNK_SIZE=2)
    @patch('apps.external_device.mqtt_client.publish_send_request_ephemeral')
    @patch('apps.external_device.services.dispatch_outbound_mmcli')
    def test_process_sms_request_chunks_mqtt_recipients(self, mock_dispatch, mock_mqtt_publish, active_device):
        def mock_dispatch_impl(outbound):
            outbound.state = OutboundSms.State.SENT
            outbound.save()
            return outbound

        mock_dispatch.side_effect = mock_dispatch_impl

        recipients = ['+351910000001', '+351910000002', '+351910000003']
        sms_request = process_sms_request(
            device=active_device,
            recipients=recipients,
            message='Bulk',
            priority='normal',
        )

        assert mock_mqtt_publish.call_count == 2
        args1 = mock_mqtt_publish.call_args_list[0][0]
        args2 = mock_mqtt_publish.call_args_list[1][0]
        assert args1[0] == active_device.device_id
        payload1 = args1[1]
        payload2 = args2[1]
        assert payload1['chunk_index'] == 0
        assert payload1['chunk_total'] == 2
        assert payload1['recipients'] == ['+351910000001', '+351910000002']
        assert payload2['chunk_index'] == 1
        assert payload2['chunk_total'] == 2
        assert payload2['recipients'] == ['+351910000003']
        assert payload1['request_id'] == sms_request.request_id
        import re
        ts_re = re.compile(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$')
        assert ts_re.match(payload1['timestamp']), f"unexpected timestamp: {payload1.get('timestamp')}"
        assert ts_re.match(payload2['timestamp']), f"unexpected timestamp: {payload2.get('timestamp')}"

    @override_settings(MQTT_PUBLISH_SEND_REQUEST=False)
    @patch('apps.external_device.mqtt_client.publish_send_request_ephemeral')
    @patch('apps.external_device.services.dispatch_outbound_mmcli')
    def test_process_sms_request_skips_mqtt_when_disabled(
        self,
        mock_dispatch,
        mock_mqtt_publish,
        active_device,
    ):
        def mock_dispatch_impl(outbound):
            outbound.state = OutboundSms.State.SENT
            outbound.save()

        mock_dispatch.side_effect = mock_dispatch_impl

        process_sms_request(
            device=active_device,
            recipients=['+351912345678'],
            message='Test message',
            priority='normal',
        )
        mock_mqtt_publish.assert_not_called()

    def test_register_device_service(self, pending_device):
        """Register device service function works correctly."""
        data = {
            'device_id': pending_device.device_id,
            'registration_token': pending_device.raw_token,
            'name': 'Service Test Device',
            'device_type': 'modem',
        }

        device, raw_api_key = register_device(data)

        assert device.status == ExternalDevice.Status.ACTIVE
        assert device.name == 'Service Test Device'
        assert len(raw_api_key) > 0

    def test_inbox_populated_after_inbound_sms_created(self, active_device):
        """InboxMessage is populated automatically when InboundSms is created via signal."""
        inbound = InboundSms.objects.create(
            mm_path='/org/freedesktop/ModemManager1/SMS/signal_test',
            modem_index=0,
            from_number='+351911111111',
            text='Signal triggered message',
        )

        inbox = InboxMessage.objects.filter(device=active_device, sender='+351911111111')
        assert inbox.exists()
        assert inbox.first().message_id == f'mmcli_{inbound.pk}_dev_{active_device.pk}'
        assert inbox.first().body == 'Signal triggered message'

    @override_settings(MQTT_PUBLISH_MODEM_INBOX=True, MQTT_MODEM_INBOX_DELIVERY_MODE='broadcast')
    @patch('apps.external_device.mqtt_client.publish_modem_inbox_broadcast_ephemeral')
    def test_modem_mirror_publishes_mqtt_broadcast_when_enabled(self, mock_broadcast_mqtt, active_device):
        InboundSms.objects.create(
            mm_path='/org/freedesktop/ModemManager1/SMS/mm_mqtt_mirror',
            modem_index=0,
            from_number='+351911112222',
            text='modem body MQTT',
        )
        mock_broadcast_mqtt.assert_called_once()
        modem_idx, payload = mock_broadcast_mqtt.call_args[0]
        assert modem_idx == 0
        assert payload['sender'] == '+351911112222'
        assert payload['body'] == 'modem body MQTT'
        assert 'received_at' in payload
        assert payload['mirrored_device_ids'] == [active_device.device_id]
        mid_rest = payload['message_id'][len('mmcli_'):]
        assert payload['device_message_ids'][active_device.device_id] == f'mmcli_{mid_rest}_dev_{active_device.pk}'

    @override_settings(MQTT_PUBLISH_MODEM_INBOX=True, MQTT_MODEM_INBOX_DELIVERY_MODE='broadcast')
    @patch('apps.external_device.mqtt_client.publish_modem_inbox_broadcast_ephemeral')
    def test_modem_inbox_broadcast_single_publish_two_devices(self, mock_broadcast):
        d_a = ExternalDevice.objects.create(
            device_id='+351913000701',
            name='Dev A',
            device_type='modem',
            api_key_hash=hash_api_key(generate_token(48)),
            status=ExternalDevice.Status.ACTIVE,
        )
        d_b = ExternalDevice.objects.create(
            device_id='+351913000702',
            name='Dev B',
            device_type='modem',
            api_key_hash=hash_api_key(generate_token(48)),
            status=ExternalDevice.Status.ACTIVE,
        )
        inbound = InboundSms.objects.create(
            mm_path='/org/freedesktop/ModemManager1/SMS/bcast_two',
            modem_index=3,
            from_number='+351900',
            text='same sms',
        )
        mock_broadcast.assert_called_once()
        modem_idx, payload = mock_broadcast.call_args[0]
        assert modem_idx == 3
        assert payload['message_id'] == f'mmcli_{inbound.pk}'
        ids = sorted(payload['mirrored_device_ids'])
        assert ids == sorted([d_a.device_id, d_b.device_id])
        assert payload['device_message_ids'][d_a.device_id] == f'mmcli_{inbound.pk}_dev_{d_a.pk}'
        assert payload['device_message_ids'][d_b.device_id] == f'mmcli_{inbound.pk}_dev_{d_b.pk}'

    @override_settings(MQTT_PUBLISH_MODEM_INBOX=True, MQTT_MODEM_INBOX_DELIVERY_MODE='per_device')
    @patch('apps.external_device.mqtt_client.publish_modem_inbox_delivery_ephemeral')
    def test_modem_inbox_per_device_publishes_per_external_device(self, mock_per_dev):
        ExternalDevice.objects.create(
            device_id='+351913000801',
            name='Dev 1',
            device_type='modem',
            api_key_hash=hash_api_key(generate_token(48)),
            status=ExternalDevice.Status.ACTIVE,
        )
        ExternalDevice.objects.create(
            device_id='+351913000802',
            name='Dev 2',
            device_type='modem',
            api_key_hash=hash_api_key(generate_token(48)),
            status=ExternalDevice.Status.ACTIVE,
        )
        inbound = InboundSms.objects.create(
            mm_path='/org/freedesktop/ModemManager1/SMS/pd_twice',
            modem_index=1,
            from_number='+351988',
            text='dual',
        )
        assert mock_per_dev.call_count == 2

    @override_settings(MQTT_PUBLISH_MODEM_INBOX=True, MQTT_MODEM_INBOX_DELIVERY_MODE='per_device')
    @patch('apps.external_device.mqtt_client.publish_modem_inbox_delivery_ephemeral')
    def test_modem_mirror_per_device_payload(self, mock_deliver_mqtt, active_device):
        inbound = InboundSms.objects.create(
            mm_path='/org/freedesktop/ModemManager1/SMS/mm_mqtt_mirror_pd',
            modem_index=0,
            from_number='+351911112222',
            text='modem body MQTT',
        )
        mock_deliver_mqtt.assert_called_once()
        device_id_arg, mqtt_payload = mock_deliver_mqtt.call_args[0]
        assert device_id_arg == active_device.device_id
        assert mqtt_payload['message_id'] == f'mmcli_{inbound.pk}_dev_{active_device.pk}'
        assert mqtt_payload['sender'] == '+351911112222'

    def test_manual_resync_broadcast_mode_no_extra_duplicate_mqtt_stub(self):
        """Calling sync_single_inbound_to_all_devices again does not re-emit if nothing new to patch."""
        with override_settings(
            MQTT_PUBLISH_MODEM_INBOX=True,
            MQTT_MODEM_INBOX_DELIVERY_MODE='broadcast',
        ), patch(
            'apps.external_device.mqtt_client.publish_modem_inbox_broadcast_ephemeral'
        ) as mock_b:
            ExternalDevice.objects.create(
                device_id='+351913000903',
                name='once',
                device_type='modem',
                api_key_hash=hash_api_key(generate_token(48)),
                status=ExternalDevice.Status.ACTIVE,
            )
            inbound = InboundSms.objects.create(
                mm_path='/org/freedesktop/ModemManager1/SMS/resync_bc',
                modem_index=2,
                from_number='+351955',
                text='once',
            )
            assert mock_b.call_count == 1
            sync_single_inbound_to_all_devices(inbound)
            assert mock_b.call_count == 1

    def test_inbox_sync_respects_modem_index_when_set(self):
        """sync_inbox_from_modem_store filters by modem_index when set in metadata."""
        from apps.external_device.services import sync_inbox_from_modem_store

        device = ExternalDevice.objects.create(
            device_id='+351913000999',
            name='Device with modem_index',
            device_type='modem',
            metadata={'modem_index': 2},
            status=ExternalDevice.Status.ACTIVE,
        )

        InboundSms.objects.create(
            mm_path='/org/freedesktop/ModemManager1/SMS/idx2',
            modem_index=2,
            from_number='+351911111111',
            text='Index 2',
        )
        InboundSms.objects.create(
            mm_path='/org/freedesktop/ModemManager1/SMS/idx0',
            modem_index=0,
            from_number='+351922222222',
            text='Index 0',
        )

        sync_inbox_from_modem_store(device)

        inbox_messages = InboxMessage.objects.filter(device=device)
        assert inbox_messages.count() == 1
        assert inbox_messages.first().sender == '+351911111111'

    def test_inbox_sync_includes_all_indexes_when_no_metadata(self):
        """sync_inbox_from_modem_store includes all InboundSms when no modem_index in metadata."""
        from apps.external_device.services import sync_inbox_from_modem_store

        device = ExternalDevice.objects.create(
            device_id='+351913000998',
            name='Device without modem_index',
            device_type='modem',
            metadata={},
            status=ExternalDevice.Status.ACTIVE,
        )

        InboundSms.objects.create(
            mm_path='/org/freedesktop/ModemManager1/SMS/all1',
            modem_index=0,
            from_number='+351911111111',
            text='Index 0',
        )
        InboundSms.objects.create(
            mm_path='/org/freedesktop/ModemManager1/SMS/all2',
            modem_index=1,
            from_number='+351922222222',
            text='Index 1',
        )

        sync_inbox_from_modem_store(device)

        inbox_messages = InboxMessage.objects.filter(device=device)
        assert inbox_messages.count() == 2
        senders = {msg.sender for msg in inbox_messages}
        assert senders == {'+351911111111', '+351922222222'}


@pytest.mark.django_db
class TestExternalDeviceCascadeDelete:
    def test_delete_external_device_and_dependencies_removes_protect_related(self):
        raw_key = generate_token(32)
        device = ExternalDevice.objects.create(
            device_id='+351913000777',
            name='Cascade Device',
            api_key_hash=hash_api_key(raw_key),
            status=ExternalDevice.Status.ACTIVE,
        )
        HiDishelinkDevice.objects.create(device_id=device.device_id, api_url='http://example.invalid', api_key='k')
        DeviceSession.objects.create(
            session_id='s_sess_del_test123456789012345678901234567890',
            device=device,
            expires_at=timezone.now() + timedelta(hours=1),
        )
        tel = DeviceHealthTelemetry.objects.create(device=device, raw_payload={'x': 1})
        req = SmsRequest.objects.create(
            request_id='req_cascade_del',
            device=device,
            recipients=['+1'],
            message='m',
        )
        SmsRecipientStatus.objects.create(request=req, phone_number='+1', status=SmsRecipientStatus.Status.SENT)
        InboxMessage.objects.create(
            message_id='inbox_cascade_del',
            device=device,
            sender='+351910000000',
            body='hi',
            received_at=timezone.now(),
        )

        delete_external_device_and_dependencies(device)

        assert not ExternalDevice.objects.filter(pk='+351913000777').exists()
        assert not HiDishelinkDevice.objects.filter(pk='+351913000777').exists()
        assert not SmsRequest.objects.filter(request_id='req_cascade_del').exists()
        assert not InboxMessage.objects.filter(message_id='inbox_cascade_del').exists()
        assert not DeviceSession.objects.filter(session_id='s_sess_del_test123456789012345678901234567890').exists()
        assert not DeviceHealthTelemetry.objects.filter(pk=tel.pk).exists()


@pytest.mark.django_db
class TestExternalDeviceAdminSms:
    """Smoke tests for ExternalDevice admin SMS send page and inbox sync action."""

    def test_admin_send_sms_get_active_device(self, admin_client, active_device):
        url = reverse(
            'admin:external_device_externaldevice_sendsms',
            args=[active_device.device_id],
        )
        response = admin_client.get(url)
        assert response.status_code == 200
        content = response.content.decode()
        assert 'Send SMS' in content or 'send SMS' in content.lower()
        assert 'recipient' in content.lower()
        assert 'SMS body' in content or 'sms body' in content.lower()

    def test_admin_send_sms_post_success_redirects(self, admin_client, active_device):
        url = reverse(
            'admin:external_device_externaldevice_sendsms',
            args=[active_device.device_id],
        )
        mock_req = Mock(request_id='admin_smoke_rid', status='dispatched')
        with patch('apps.external_device.admin.process_sms_request', return_value=mock_req):
            response = admin_client.post(
                url,
                {
                    'recipients': '+351912340001\n+351912340002',
                    'message': 'Hi from admin test',
                    'priority': 'normal',
                },
            )
        assert response.status_code == 302
        change_url = reverse(
            'admin:external_device_externaldevice_change',
            args=[active_device.device_id],
        )
        assert response['Location'].endswith(change_url)

    def test_admin_send_sms_post_validation_error_stays_on_form(self, admin_client, active_device):
        url = reverse(
            'admin:external_device_externaldevice_sendsms',
            args=[active_device.device_id],
        )
        with patch(
            'apps.external_device.admin.process_sms_request',
            side_effect=DRFValidationError({'recipients': ['Invalid recipient format.']}),
        ):
            response = admin_client.post(
                url,
                {
                    'recipients': '+351912340001',
                    'message': 'x',
                    'priority': 'normal',
                },
            )
        assert response.status_code == 200

    def test_admin_send_sms_non_active_device_post_redirects_without_dispatch(self, admin_client, pending_device):
        url = reverse(
            'admin:external_device_externaldevice_sendsms',
            args=[pending_device.device_id],
        )
        with patch('apps.external_device.admin.process_sms_request') as mock_proc:
            response = admin_client.post(
                url,
                {
                    'recipients': '+351912340001',
                    'message': 'should not send',
                    'priority': 'normal',
                },
            )
        mock_proc.assert_not_called()
        assert response.status_code == 302

    def test_admin_sync_inbox_action_calls_service(self, admin_client, active_device):
        changelist_url = reverse('admin:external_device_externaldevice_changelist')
        with patch('apps.external_device.admin.sync_inbox_from_modem_store') as sync_mock:
            response = admin_client.post(
                changelist_url,
                {
                    'action': 'sync_inbox_from_modem',
                    '_selected_action': active_device.device_id,
                },
            )
        assert response.status_code == 302
        sync_mock.assert_called_once()
        assert sync_mock.call_args[0][0].pk == active_device.device_id

    def test_admin_change_page_shows_inbox_preview_row(self, admin_client, active_device):
        InboxMessage.objects.create(
            message_id='admin_preview_mid',
            device=active_device,
            sender='+351910009999',
            body='preview body',
            received_at=timezone.now(),
        )
        change_url = reverse(
            'admin:external_device_externaldevice_change',
            args=[active_device.device_id],
        )
        response = admin_client.get(change_url)
        assert response.status_code == 200
        content = response.content.decode()
        assert 'admin_preview_mid' in content
        assert '+351910009999' in content


@pytest.mark.django_db
class TestInboxMessageAdminAdd:
    """InboxMessage admin add flow and received_at safeguards."""

    def test_admin_add_inbox_message_post_succeeds(self, admin_client, active_device):
        add_url = reverse('admin:external_device_inboxmessage_add')
        response = admin_client.post(
            add_url,
            {
                'device': active_device.device_id,
                'sender': '+351910000011',
                'body': 'Manual inbox row',
                '_save': 'Save',
            },
        )
        assert response.status_code == 302
        row = InboxMessage.objects.get(sender='+351910000011', body='Manual inbox row')
        assert row.device_id == active_device.device_id
        assert row.message_id.startswith('inbox_manual_')
        assert row.received_at is not None

    def test_admin_add_with_send_checkbox_calls_process_sms_request(self, admin_client, active_device):
        add_url = reverse('admin:external_device_inboxmessage_add')
        with patch('apps.external_device.admin.process_sms_request') as mock_proc:
            mock_proc.return_value = Mock(
                request_id='rid_inbox_send',
                status=SmsRequest.Status.COMPLETED,
                sent_count=1,
                failed_count=0,
            )
            response = admin_client.post(
                add_url,
                {
                    'device': active_device.device_id,
                    'sender': '+351910000099',
                    'body': 'reply text',
                    'also_send_via_modem': 'on',
                    '_save': 'Save',
                },
            )
        assert response.status_code == 302
        assert InboxMessage.objects.filter(sender='+351910000099', body='reply text').exists()
        mock_proc.assert_called_once()
        kwargs = mock_proc.call_args.kwargs
        assert kwargs['device'].pk == active_device.pk
        assert kwargs['recipients'] == ['+351910000099']
        assert kwargs['message'] == 'reply text'
        assert kwargs['priority'] == 'normal'

    def test_admin_add_without_send_checkbox_skips_process_sms_request(self, admin_client, active_device):
        add_url = reverse('admin:external_device_inboxmessage_add')
        with patch('apps.external_device.admin.process_sms_request') as mock_proc:
            admin_client.post(
                add_url,
                {
                    'device': active_device.device_id,
                    'sender': '+351910000088',
                    'body': 'inbox only',
                    '_save': 'Save',
                },
            )
        mock_proc.assert_not_called()

    def test_admin_add_send_checkbox_inactive_device_skips_process(self, admin_client, pending_device):
        add_url = reverse('admin:external_device_inboxmessage_add')
        with patch('apps.external_device.admin.process_sms_request') as mock_proc:
            admin_client.post(
                add_url,
                {
                    'device': pending_device.device_id,
                    'sender': '+351910000077',
                    'body': 'try send',
                    'also_send_via_modem': 'on',
                    '_save': 'Save',
                },
            )
        mock_proc.assert_not_called()
        assert InboxMessage.objects.filter(sender='+351910000077').exists()

    def test_save_model_generates_message_id_when_blank(self, rf, active_device):
        User = get_user_model()
        user = User.objects.create_superuser('adm_inbox_mid', 'mid@test.invalid', 'pw')
        request = rf.post('/admin/')
        request.user = user

        ma = InboxMessageAdmin(InboxMessage, admin.site)
        obj = InboxMessage(
            device=active_device,
            sender='+351910000033',
            body='midgen',
            message_id='',
        )
        ma.save_model(request, obj, form=None, change=False)
        obj.refresh_from_db()
        assert obj.message_id.startswith('inbox_manual_')
        assert obj.received_at is not None

    def test_save_model_defaults_received_at_when_none_on_add(self, rf, active_device):
        User = get_user_model()
        user = User.objects.create_superuser('adm_inbox_ra', 'inbox_ra@test.invalid', 'pw')
        request = rf.post('/admin/')
        request.user = user

        ma = InboxMessageAdmin(InboxMessage, admin.site)
        obj = InboxMessage(
            message_id='admin_save_mid_ra',
            device=active_device,
            sender='+351910000022',
            body='body',
        )
        assert obj.received_at is None
        ma.save_model(request, obj, form=None, change=False)
        obj.refresh_from_db()
        assert obj.received_at is not None
