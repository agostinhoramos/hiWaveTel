"""Tests for MQTT client (apps/external_device/mqtt_client.py)."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, call, patch

import pytest
from django.test import override_settings

from apps.external_device.models import ExternalDevice, InboxMessage
from apps.external_device.mqtt_client import (
    GatewayMqttClient,
    modem_index_from_status_request_topic,
    modem_mmcli_flat_fingerprint,
    publish_modem_inbox_broadcast_ephemeral,
    publish_modem_inbox_delivery_ephemeral,
    publish_send_request_ephemeral,
    sanitize_device_id,
)


class _ImmediateDaemonThread:
    """Standalone double for threading.Thread — runs ``target()`` when ``start()`` is called."""

    def __init__(self, group=None, target=None, name=None, args=(), kwargs=None, *, daemon=False):
        self._fn = target
        self.daemon = daemon

    def start(self) -> None:
        if self._fn:
            self._fn()


class TestSanitizeDeviceId:
    """Test sanitize_device_id utility function."""

    def test_removes_plus_and_hash(self):
        """Should remove + and # characters."""
        assert sanitize_device_id('+351913000387') == '351913000387'
        assert sanitize_device_id('+351#123#456') == '351123456'

    def test_leaves_other_chars_intact(self):
        """Should leave other characters unchanged."""
        assert sanitize_device_id('351-913-000-387') == '351-913-000-387'
        assert sanitize_device_id('ABC123') == 'ABC123'

    def test_empty_string(self):
        """Should handle empty string."""
        assert sanitize_device_id('') == ''


@pytest.mark.django_db
class TestGatewayMqttClientInit:
    """Test GatewayMqttClient initialization."""

    @patch('apps.external_device.mqtt_client.mqtt.Client')
    @override_settings(
        MQTT_BROKER_URL='test-broker.local',
        MQTT_PORT=1883,
        MQTT_USER='testuser',
        MQTT_PASS='testpass',
        MQTT_CLIENT_ID='test-gateway',
        MQTT_KEEPALIVE=60,
        MQTT_QOS=1,
        MQTT_CLEAN_SESSION=True,
        MQTT_EXTERNAL_TOPIC_PREFIX='hiwavetel',
    )
    def test_init_with_credentials(self, mock_mqtt_client_class):
        """Should initialize client with credentials."""
        mock_client_instance = MagicMock()
        mock_mqtt_client_class.return_value = mock_client_instance

        client = GatewayMqttClient()

        assert client.broker_url == 'test-broker.local'
        assert client.port == 1883
        assert client.username == 'testuser'
        assert client.password == 'testpass'
        assert client.client_id == 'test-gateway'
        assert client.keepalive == 60
        assert client.qos == 1
        assert client.clean_session is True
        assert client.topic_prefix == 'hiwavetel'

        mock_client_instance.username_pw_set.assert_called_once_with('testuser', 'testpass')
        mock_client_instance.tls_set.assert_not_called()

    @patch('apps.external_device.mqtt_client.mqtt.Client')
    @override_settings(
        MQTT_BROKER_URL='test-broker.local',
        MQTT_PORT=8883,
        MQTT_USER=None,
        MQTT_PASS=None,
        MQTT_CLIENT_ID='test-gateway',
        MQTT_KEEPALIVE=60,
        MQTT_QOS=1,
        MQTT_CLEAN_SESSION=True,
        MQTT_EXTERNAL_TOPIC_PREFIX='hiwavetel',
    )
    def test_init_with_tls(self, mock_mqtt_client_class):
        """Should configure TLS for port 8883."""
        mock_client_instance = MagicMock()
        mock_mqtt_client_class.return_value = mock_client_instance

        client = GatewayMqttClient()

        mock_client_instance.username_pw_set.assert_not_called()
        mock_client_instance.tls_set.assert_called_once()


@pytest.mark.django_db
class TestGatewayMqttClientMethods:
    """Test GatewayMqttClient wrapper methods."""

    @patch('apps.external_device.mqtt_client.mqtt.Client')
    def test_connect(self, mock_mqtt_client_class):
        """Should call client.connect with broker settings."""
        mock_client_instance = MagicMock()
        mock_mqtt_client_class.return_value = mock_client_instance

        client = GatewayMqttClient()
        client.connect()

        mock_client_instance.connect.assert_called_once_with(
            client.broker_url, client.port, client.keepalive
        )

    @patch('apps.external_device.mqtt_client.mqtt.Client')
    def test_loop_forever(self, mock_mqtt_client_class):
        """Should call client.loop_forever."""
        mock_client_instance = MagicMock()
        mock_mqtt_client_class.return_value = mock_client_instance

        client = GatewayMqttClient()
        client.loop_forever()

        mock_client_instance.loop_forever.assert_called_once()

    @patch('apps.external_device.mqtt_client.mqtt.Client')
    def test_loop_start(self, mock_mqtt_client_class):
        """Should call client.loop_start."""
        mock_client_instance = MagicMock()
        mock_mqtt_client_class.return_value = mock_client_instance

        client = GatewayMqttClient()
        client.loop_start()

        mock_client_instance.loop_start.assert_called_once()

    @patch('apps.external_device.mqtt_client.mqtt.Client')
    def test_loop_stop(self, mock_mqtt_client_class):
        """Should call client.loop_stop."""
        mock_client_instance = MagicMock()
        mock_mqtt_client_class.return_value = mock_client_instance

        client = GatewayMqttClient()
        client.loop_stop()

        mock_client_instance.loop_stop.assert_called_once()

    @patch('apps.external_device.mqtt_client.mqtt.Client')
    def test_disconnect(self, mock_mqtt_client_class):
        """Should call client.disconnect."""
        mock_client_instance = MagicMock()
        mock_mqtt_client_class.return_value = mock_client_instance

        client = GatewayMqttClient()
        client.disconnect()

        mock_client_instance.disconnect.assert_called_once()


@pytest.mark.django_db
class TestGatewayMqttClientPublish:
    """Test GatewayMqttClient publish methods."""

    @patch('apps.external_device.mqtt_client.mqtt.Client')
    @override_settings(
        MQTT_EXTERNAL_TOPIC_PREFIX='hiwavetel',
        MQTT_QOS=1,
        MQTT_BROKER_URL='test-broker.local',
        MQTT_PORT=1883,
        MQTT_USER=None,
        MQTT_PASS=None,
        MQTT_CLIENT_ID='test-gateway',
        MQTT_KEEPALIVE=60,
        MQTT_CLEAN_SESSION=True,
    )
    def test_publish_send_request(self, mock_mqtt_client_class):
        """Should publish send request to correct topic with sanitized device_id."""
        mock_client_instance = MagicMock()
        mock_mqtt_client_class.return_value = mock_client_instance

        client = GatewayMqttClient()
        payload = {'request_id': 'req-123', 'to': '351912345678', 'body': 'Test message'}
        client.publish_send_request('+351913000387', payload)

        expected_topic = 'hiwavetel/devices/351913000387/sms/send'
        expected_message = json.dumps(payload)
        mock_client_instance.publish.assert_called_once_with(
            expected_topic, expected_message, qos=1
        )

    @patch('apps.external_device.mqtt_client.mqtt.Client')
    @override_settings(
        MQTT_EXTERNAL_TOPIC_PREFIX='hiwavetel',
        MQTT_QOS=1,
        MQTT_BROKER_URL='test-broker.local',
        MQTT_PORT=1883,
        MQTT_USER=None,
        MQTT_PASS=None,
        MQTT_CLIENT_ID='test-gateway',
        MQTT_KEEPALIVE=60,
        MQTT_CLEAN_SESSION=True,
    )
    def test_publish_inbox_ack(self, mock_mqtt_client_class):
        """Should publish inbox ACK to correct topic with sanitized device_id."""
        mock_client_instance = MagicMock()
        mock_mqtt_client_class.return_value = mock_client_instance

        client = GatewayMqttClient()
        client.publish_inbox_ack('+351913000387', 'msg-456')

        expected_topic = 'hiwavetel/devices/351913000387/sms/inbox/ack'
        expected_message = json.dumps({'message_id': 'msg-456'})
        mock_client_instance.publish.assert_called_once_with(
            expected_topic, expected_message, qos=1
        )


@pytest.mark.django_db
class TestGatewayMqttClientCallbacks:
    """Test GatewayMqttClient callbacks."""

    @patch('apps.external_device.mqtt_client.mqtt.Client')
    @override_settings(
        MQTT_EXTERNAL_TOPIC_PREFIX='hiwavetel',
        MQTT_QOS=1,
        MQTT_BROKER_URL='test-broker.local',
        MQTT_PORT=1883,
        MQTT_USER=None,
        MQTT_PASS=None,
        MQTT_CLIENT_ID='test-gateway',
        MQTT_KEEPALIVE=60,
        MQTT_CLEAN_SESSION=True,
    )
    def test_on_connect_success(self, mock_mqtt_client_class):
        """Should subscribe to topics on successful connection (rc=0)."""
        mock_client_instance = MagicMock()
        mock_mqtt_client_class.return_value = mock_client_instance

        client = GatewayMqttClient()
        client._on_connect(mock_client_instance, None, {}, 0)

        assert mock_client_instance.subscribe.call_count == 3
        expected_calls = [
            call('hiwavetel/devices/+/sms/status', qos=1),
            call('hiwavetel/devices/+/sms/inbox', qos=1),
            call('hiwavetel/modems/+/status/request', qos=1),
        ]
        mock_client_instance.subscribe.assert_has_calls(expected_calls, any_order=True)

    @patch('apps.external_device.mqtt_client.mqtt.Client')
    @override_settings(
        MQTT_EXTERNAL_TOPIC_PREFIX='hiwavetel',
        MQTT_QOS=1,
        MQTT_BROKER_URL='test-broker.local',
        MQTT_PORT=1883,
        MQTT_USER=None,
        MQTT_PASS=None,
        MQTT_CLIENT_ID='test-gateway',
        MQTT_KEEPALIVE=60,
        MQTT_CLEAN_SESSION=True,
        MQTT_MODEM_STATUS_SUBSCRIBE=False,
    )
    def test_on_connect_modem_status_subscription_disabled(self, mock_mqtt_client_class):
        """When MQTT_MODEM_STATUS_SUBSCRIBE is false, do not subscribe to modem status request."""
        mock_client_instance = MagicMock()
        mock_mqtt_client_class.return_value = mock_client_instance

        client = GatewayMqttClient()
        client._on_connect(mock_client_instance, None, {}, 0)

        assert mock_client_instance.subscribe.call_count == 2
        topics_subscribed = {c[0][0] for c in mock_client_instance.subscribe.call_args_list}
        assert 'hiwavetel/modems/+/status/request' not in topics_subscribed

    @patch('apps.external_device.mqtt_client.mqtt.Client')
    def test_on_connect_failure(self, mock_mqtt_client_class):
        """Should not subscribe on connection failure (rc!=0)."""
        mock_client_instance = MagicMock()
        mock_mqtt_client_class.return_value = mock_client_instance

        client = GatewayMqttClient()
        client._on_connect(mock_client_instance, None, {}, 5)

        mock_client_instance.subscribe.assert_not_called()

    @patch('apps.external_device.mqtt_client.mqtt.Client')
    def test_on_disconnect_graceful(self, mock_mqtt_client_class):
        """Should log graceful disconnect (rc=0)."""
        mock_client_instance = MagicMock()
        mock_mqtt_client_class.return_value = mock_client_instance

        client = GatewayMqttClient()
        # Should not raise exception
        client._on_disconnect(mock_client_instance, None, 0)

    @patch('apps.external_device.mqtt_client.mqtt.Client')
    def test_on_disconnect_unexpected(self, mock_mqtt_client_class):
        """Should log unexpected disconnect (rc!=0)."""
        mock_client_instance = MagicMock()
        mock_mqtt_client_class.return_value = mock_client_instance

        client = GatewayMqttClient()
        # Should not raise exception
        client._on_disconnect(mock_client_instance, None, 1)


@pytest.mark.django_db
class TestGatewayMqttClientOnMessage:
    """Test _on_message routing."""

    @patch('apps.external_device.mqtt_client.mqtt.Client')
    def test_on_message_routes_to_status_handler(self, mock_mqtt_client_class):
        """Should route /sms/status messages to _handle_status_message."""
        mock_client_instance = MagicMock()
        mock_mqtt_client_class.return_value = mock_client_instance

        client = GatewayMqttClient()

        msg = SimpleNamespace(
            topic='hiwavetel/devices/351913000387/sms/status',
            payload=json.dumps({'request_id': 'req-123', 'status': 'sent'}).encode('utf-8')
        )

        with patch.object(client, '_handle_status_message') as mock_handle_status:
            client._on_message(mock_client_instance, None, msg)
            mock_handle_status.assert_called_once()

    @patch('apps.external_device.mqtt_client.mqtt.Client')
    def test_on_message_routes_to_inbox_handler(self, mock_mqtt_client_class):
        """Should route /sms/inbox messages to _handle_inbox_message."""
        mock_client_instance = MagicMock()
        mock_mqtt_client_class.return_value = mock_client_instance

        client = GatewayMqttClient()

        msg = SimpleNamespace(
            topic='hiwavetel/devices/351913000387/sms/inbox',
            payload=json.dumps({
                'message_id': 'msg-456',
                'from': '351912345678',
                'body': 'Hello'
            }).encode('utf-8')
        )

        with patch.object(client, '_handle_inbox_message') as mock_handle_inbox:
            client._on_message(mock_client_instance, None, msg)
            mock_handle_inbox.assert_called_once()

    @patch('apps.external_device.mqtt_client.mqtt.Client')
    def test_on_message_invalid_json(self, mock_mqtt_client_class):
        """Should handle invalid JSON gracefully."""
        mock_client_instance = MagicMock()
        mock_mqtt_client_class.return_value = mock_client_instance

        client = GatewayMqttClient()

        msg = SimpleNamespace(
            topic='hiwavetel/devices/351913000387/sms/status',
            payload=b'not-json'
        )

        # Should not raise exception
        client._on_message(mock_client_instance, None, msg)

    @patch('apps.external_device.mqtt_client.mqtt.Client')
    def test_on_message_modem_status_empty_payload_schedules_snapshot(self, mock_mqtt_client_class):
        mock_client_instance = MagicMock()
        mock_mqtt_client_class.return_value = mock_client_instance

        client = GatewayMqttClient()
        topic = 'hiwavetel/modems/11/status/request'

        msg = SimpleNamespace(topic=topic, payload=b'')

        with patch.object(client, '_schedule_modem_status_snapshot') as mock_sched:
            client._on_message(mock_client_instance, None, msg)
            mock_sched.assert_called_once_with(topic)

    @patch('apps.external_device.mqtt_client.mqtt.Client')
    @override_settings(MQTT_MODEM_STATUS_SUBSCRIBE=False)
    def test_on_message_modem_status_ignored_when_disabled(self, mock_mqtt_client_class):
        mock_client_instance = MagicMock()
        mock_mqtt_client_class.return_value = mock_client_instance

        client = GatewayMqttClient()
        msg = SimpleNamespace(
            topic='hiwavetel/modems/0/status/request',
            payload=b'{}',
        )

        with patch.object(client, '_schedule_modem_status_snapshot') as mock_sched:
            client._on_message(mock_client_instance, None, msg)
            mock_sched.assert_not_called()

    @override_settings(MQTT_EXTERNAL_TOPIC_PREFIX='pfx')
    @patch('apps.external_device.mqtt_client.mqtt.Client')
    @patch('apps.external_device.mqtt_client._publish_json_ephemeral')
    @patch('apps.external_device.mqtt_client.build_modem_status_mqtt_payload')
    @patch('apps.external_device.mqtt_client.threading.Thread', new=_ImmediateDaemonThread)
    def test_modem_status_request_snapshot_published(
        self, mock_build_status, mock_publish, mock_mqtt_client_class,
    ):
        mock_client_instance = MagicMock()
        mock_mqtt_client_class.return_value = mock_client_instance
        snap = {'modem_index': 2, 'success': True, 'mmcli_flat': {}, 'error': None, 'gathered_at': 'ts'}
        mock_build_status.return_value = snap

        client = GatewayMqttClient()
        client._schedule_modem_status_snapshot('pfx/modems/2/status/request')

        mock_build_status.assert_called_once_with(2)
        mock_publish.assert_called_once_with('pfx/modems/2/status/response', snap)

    @patch('apps.external_device.mqtt_client.mqtt.Client')
    def test_on_message_unknown_topic(self, mock_mqtt_client_class):
        """Should log warning for unknown topic patterns."""
        mock_client_instance = MagicMock()
        mock_mqtt_client_class.return_value = mock_client_instance

        client = GatewayMqttClient()

        msg = SimpleNamespace(
            topic='hiwavetel/devices/351913000387/unknown',
            payload=json.dumps({'data': 'test'}).encode('utf-8')
        )

        # Should not raise exception
        client._on_message(mock_client_instance, None, msg)


@pytest.mark.django_db
class TestGatewayMqttClientHandleStatus:
    """Test _handle_status_message."""

    @patch('apps.external_device.mqtt_client.mqtt.Client')
    @patch('apps.external_device.mqtt_client.update_request_from_mqtt_status')
    def test_handle_status_message_success(self, mock_update_request, mock_mqtt_client_class):
        """Should call update_request_from_mqtt_status with payload."""
        mock_client_instance = MagicMock()
        mock_mqtt_client_class.return_value = mock_client_instance

        client = GatewayMqttClient()
        payload = {'request_id': 'req-123', 'status': 'delivered'}
        
        client._handle_status_message('hiwavetel/devices/351913000387/sms/status', payload)

        mock_update_request.assert_called_once_with('req-123', payload)

    @patch('apps.external_device.mqtt_client.mqtt.Client')
    @patch('apps.external_device.mqtt_client.update_request_from_mqtt_status')
    def test_handle_status_message_missing_request_id(self, mock_update_request, mock_mqtt_client_class):
        """Should not call service when request_id is missing."""
        mock_client_instance = MagicMock()
        mock_mqtt_client_class.return_value = mock_client_instance

        client = GatewayMqttClient()
        payload = {'status': 'delivered'}
        
        client._handle_status_message('hiwavetel/devices/351913000387/sms/status', payload)

        mock_update_request.assert_not_called()

    @patch('apps.external_device.mqtt_client.mqtt.Client')
    @patch('apps.external_device.mqtt_client.update_request_from_mqtt_status')
    def test_handle_status_message_exception(self, mock_update_request, mock_mqtt_client_class):
        """Should catch and log exceptions from service."""
        mock_client_instance = MagicMock()
        mock_mqtt_client_class.return_value = mock_client_instance
        mock_update_request.side_effect = Exception('DB error')

        client = GatewayMqttClient()
        payload = {'request_id': 'req-123', 'status': 'delivered'}
        
        # Should not raise exception
        client._handle_status_message('hiwavetel/devices/351913000387/sms/status', payload)


@pytest.mark.django_db
class TestGatewayMqttClientHandleInbox:
    """Test _handle_inbox_message."""

    @patch('apps.external_device.mqtt_client.mqtt.Client')
    @patch('apps.external_device.mqtt_client.persist_inbox_from_mqtt')
    def test_handle_inbox_message_success_with_ack(self, mock_persist, mock_mqtt_client_class):
        """Should persist inbox message and send ACK."""
        mock_client_instance = MagicMock()
        mock_mqtt_client_class.return_value = mock_client_instance

        device = ExternalDevice.objects.create(
            device_id='+351913000387',
            name='Test Device',
            api_key_hash='test-hash',
            status=ExternalDevice.Status.ACTIVE
        )

        mock_inbox_msg = Mock(spec=InboxMessage)
        mock_inbox_msg.message_id = 'msg-456'
        mock_inbox_msg.ack_sent = False
        mock_persist.return_value = mock_inbox_msg

        client = GatewayMqttClient()
        payload = {'message_id': 'msg-456', 'from': '351912345678', 'body': 'Hello'}
        
        with patch.object(client, 'publish_inbox_ack') as mock_publish_ack:
            client._handle_inbox_message('hiwavetel/devices/351913000387/sms/inbox', payload)

            mock_persist.assert_called_once_with(device, payload)
            mock_publish_ack.assert_called_once_with('+351913000387', 'msg-456')
            mock_inbox_msg.save.assert_called_once_with(update_fields=['ack_sent'])
            assert mock_inbox_msg.ack_sent is True

    @patch('apps.external_device.mqtt_client.mqtt.Client')
    @patch('apps.external_device.mqtt_client.persist_inbox_from_mqtt')
    def test_handle_inbox_message_already_acked(self, mock_persist, mock_mqtt_client_class):
        """Should not send ACK if already sent."""
        mock_client_instance = MagicMock()
        mock_mqtt_client_class.return_value = mock_client_instance

        device = ExternalDevice.objects.create(
            device_id='+351913000387',
            name='Test Device',
            api_key_hash='test-hash',
            status=ExternalDevice.Status.ACTIVE
        )

        mock_inbox_msg = Mock(spec=InboxMessage)
        mock_inbox_msg.message_id = 'msg-456'
        mock_inbox_msg.ack_sent = True
        mock_persist.return_value = mock_inbox_msg

        client = GatewayMqttClient()
        payload = {'message_id': 'msg-456', 'from': '351912345678', 'body': 'Hello'}
        
        with patch.object(client, 'publish_inbox_ack') as mock_publish_ack:
            client._handle_inbox_message('hiwavetel/devices/351913000387/sms/inbox', payload)

            mock_publish_ack.assert_not_called()
            mock_inbox_msg.save.assert_not_called()

    @patch('apps.external_device.mqtt_client.mqtt.Client')
    def test_handle_inbox_message_invalid_topic(self, mock_mqtt_client_class):
        """Should handle invalid topic format gracefully."""
        mock_client_instance = MagicMock()
        mock_mqtt_client_class.return_value = mock_client_instance

        client = GatewayMqttClient()
        payload = {'message_id': 'msg-456', 'from': '351912345678', 'body': 'Hello'}
        
        # Should not raise exception
        client._handle_inbox_message('invalid-topic', payload)

    @patch('apps.external_device.mqtt_client.mqtt.Client')
    def test_handle_inbox_message_device_not_found(self, mock_mqtt_client_class):
        """Should handle device not found gracefully."""
        mock_client_instance = MagicMock()
        mock_mqtt_client_class.return_value = mock_client_instance

        client = GatewayMqttClient()
        payload = {'message_id': 'msg-456', 'from': '351912345678', 'body': 'Hello'}
        
        # Should not raise exception
        client._handle_inbox_message('hiwavetel/devices/351913000387/sms/inbox', payload)

    @patch('apps.external_device.mqtt_client.mqtt.Client')
    @patch('apps.external_device.mqtt_client.persist_inbox_from_mqtt')
    def test_handle_inbox_message_persist_exception(self, mock_persist, mock_mqtt_client_class):
        """Should catch and log exceptions from persist service."""
        mock_client_instance = MagicMock()
        mock_mqtt_client_class.return_value = mock_client_instance

        ExternalDevice.objects.create(
            device_id='+351913000387',
            name='Test Device',
            api_key_hash='test-hash',
            status=ExternalDevice.Status.ACTIVE
        )

        mock_persist.side_effect = Exception('DB error')

        client = GatewayMqttClient()
        payload = {'message_id': 'msg-456', 'from': '351912345678', 'body': 'Hello'}
        
        # Should not raise exception
        client._handle_inbox_message('hiwavetel/devices/351913000387/sms/inbox', payload)


@pytest.mark.django_db
class TestGatewayMqttClientHelpers:
    """Test helper methods."""

    @patch('apps.external_device.mqtt_client.mqtt.Client')
    def test_extract_device_id_from_topic_success(self, mock_mqtt_client_class):
        """Should extract device_id from valid topic."""
        mock_client_instance = MagicMock()
        mock_mqtt_client_class.return_value = mock_client_instance

        client = GatewayMqttClient()
        
        device_id = client._extract_device_id_from_topic('hiwavetel/devices/351913000387/sms/inbox')
        assert device_id == '351913000387'

    @patch('apps.external_device.mqtt_client.mqtt.Client')
    def test_extract_device_id_from_topic_no_devices_keyword(self, mock_mqtt_client_class):
        """Should return None if 'devices' keyword not found."""
        mock_client_instance = MagicMock()
        mock_mqtt_client_class.return_value = mock_client_instance

        client = GatewayMqttClient()
        
        device_id = client._extract_device_id_from_topic('hiwavetel/invalid/351913000387/sms/inbox')
        assert device_id is None

    @patch('apps.external_device.mqtt_client.mqtt.Client')
    def test_extract_device_id_from_topic_index_out_of_range(self, mock_mqtt_client_class):
        """Should return None if index after 'devices' is out of range."""
        mock_client_instance = MagicMock()
        mock_mqtt_client_class.return_value = mock_client_instance

        client = GatewayMqttClient()
        
        device_id = client._extract_device_id_from_topic('hiwavetel/devices')
        assert device_id is None

    @patch('apps.external_device.mqtt_client.mqtt.Client')
    def test_find_device_id_by_sanitized_found(self, mock_mqtt_client_class):
        """Should find device_id by matching sanitized version."""
        mock_client_instance = MagicMock()
        mock_mqtt_client_class.return_value = mock_client_instance

        ExternalDevice.objects.create(
            device_id='+351913000387',
            name='Test Device',
            api_key_hash='test-hash',
            status=ExternalDevice.Status.ACTIVE
        )
        ExternalDevice.objects.create(
            device_id='+351912345678',
            name='Test Device 2',
            api_key_hash='test-hash2',
            status=ExternalDevice.Status.ACTIVE
        )

        client = GatewayMqttClient()
        
        device_id = client._find_device_id_by_sanitized('351913000387')
        assert device_id == '+351913000387'

    @patch('apps.external_device.mqtt_client.mqtt.Client')
    def test_find_device_id_by_sanitized_not_found(self, mock_mqtt_client_class):
        """Should return None if no device matches sanitized ID."""
        mock_client_instance = MagicMock()
        mock_mqtt_client_class.return_value = mock_client_instance

        ExternalDevice.objects.create(
            device_id='+351913000387',
            name='Test Device',
            api_key_hash='test-hash',
            status=ExternalDevice.Status.ACTIVE
        )

        client = GatewayMqttClient()
        
        device_id = client._find_device_id_by_sanitized('999999999999')
        assert device_id is None


@pytest.mark.django_db
class TestEphemeralMqttPublish:
    @patch('apps.external_device.mqtt_client.mqtt.Client')
    @override_settings(
        MQTT_EXTERNAL_TOPIC_PREFIX='hiwavetel',
        MQTT_QOS=2,
        MQTT_BROKER_URL='brk.example',
        MQTT_PORT=11883,
        MQTT_USER='',
        MQTT_PASS='',
        MQTT_CLIENT_ID='gw-main',
        MQTT_KEEPALIVE=30,
        MQTT_EPHEMERAL_PUBLISH_TIMEOUT_SEC=5.0,
    )
    def test_publish_send_request_ephemeral_roundtrip(self, mock_client_class):
        mock_ci = MagicMock()
        mock_info = MagicMock()
        mock_ci.publish.return_value = mock_info
        mock_client_class.return_value = mock_ci

        publish_send_request_ephemeral('+351913000387', {'request_id': 'sms_x', 'message': 'hi'})

        mock_ci.connect.assert_called_once_with('brk.example', 11883, 30)
        mock_ci.loop_start.assert_called_once()
        mock_ci.publish.assert_called_once()
        call_kw = mock_ci.publish.call_args
        assert call_kw[0][0] == 'hiwavetel/devices/351913000387/sms/send'
        assert json.loads(call_kw[0][1])['request_id'] == 'sms_x'
        mock_info.wait_for_publish.assert_called_once_with(timeout=5.0)
        mock_ci.loop_stop.assert_called_once()
        mock_ci.disconnect.assert_called_once()

    @patch('apps.external_device.mqtt_client.mqtt.Client')
    @override_settings(
        MQTT_EXTERNAL_TOPIC_PREFIX='p',
        MQTT_QOS=1,
        MQTT_BROKER_URL='x',
        MQTT_PORT=11883,
        MQTT_USER='u',
        MQTT_PASS='pw',
        MQTT_CLIENT_ID='g',
        MQTT_KEEPALIVE=60,
        MQTT_EPHEMERAL_PUBLISH_TIMEOUT_SEC=1,
    )
    def test_modem_inbox_delivery_topic(self, mock_client_class):
        mock_ci = MagicMock()
        mock_ci.publish.return_value = MagicMock()
        mock_client_class.return_value = mock_ci

        publish_modem_inbox_delivery_ephemeral(
            '+7999',
            {'message_id': 'm1', 'sender': '+1', 'body': 'txt', 'received_at': 't'},
        )

        mock_ci.username_pw_set.assert_called_once_with('u', 'pw')
        assert mock_ci.publish.call_args[0][0].endswith('/sms/inbox_delivery')

    @patch('apps.external_device.mqtt_client.mqtt.Client')
    @override_settings(
        MQTT_EXTERNAL_TOPIC_PREFIX='p2',
        MQTT_QOS=2,
        MQTT_BROKER_URL='x',
        MQTT_PORT=1883,
        MQTT_USER='',
        MQTT_PASS='',
        MQTT_KEEPALIVE=60,
        MQTT_EPHEMERAL_PUBLISH_TIMEOUT_SEC=1,
    )
    def test_modem_inbox_broadcast_topic_modem_path(self, mock_client_class):
        mock_ci = MagicMock()
        mock_ci.publish.return_value = MagicMock()
        mock_client_class.return_value = mock_ci

        publish_modem_inbox_broadcast_ephemeral(
            1,
            {
                'message_id': 'mmcli_42',
                'sender': '+1',
                'body': 'x',
                'received_at': 't',
                'modem_index': 1,
                'mirrored_device_ids': ['+7999'],
            },
        )

        topic = mock_ci.publish.call_args[0][0]
        assert topic == 'p2/modems/1/sms/inbox_delivery'

    @patch('apps.external_device.mqtt_client.mqtt.Client')
    @override_settings(
        MQTT_EXTERNAL_TOPIC_PREFIX='pfx',
        MQTT_QOS=1,
        MQTT_BROKER_URL='x',
        MQTT_PORT=1883,
        MQTT_USER='',
        MQTT_PASS='',
        MQTT_KEEPALIVE=60,
        MQTT_EPHEMERAL_PUBLISH_TIMEOUT_SEC=1,
    )
    def test_ephemeral_publish_swallows_publish_errors(self, mock_client_class):
        mock_ci = MagicMock()
        mock_ci.publish.side_effect = RuntimeError('net')
        mock_client_class.return_value = mock_ci

        publish_send_request_ephemeral('+1', {'request_id': 'r'})


def test_modem_index_from_status_request_topic_parsing():
    assert modem_index_from_status_request_topic('hidishe/modems/7/status/request') == 7
    assert modem_index_from_status_request_topic('prefix/modems/0/status/request') == 0
    assert modem_index_from_status_request_topic('no/pe') is None


def test_modem_mmcli_flat_fingerprint_order_independent():
    a = modem_mmcli_flat_fingerprint({'b': '2', 'a': '1'})
    b = modem_mmcli_flat_fingerprint({'a': '1', 'b': '2'})
    assert a == b
    assert modem_mmcli_flat_fingerprint({'a': '1'}) != a


@pytest.mark.django_db
class TestGatewayModemTelemetryPublish:
    @patch('apps.external_device.mqtt_client.mqtt.Client')
    def test_modem_status_publish_all_bootstrap(self, mqtt_cls):
        mock_ci = MagicMock()
        mqtt_cls.return_value = mock_ci

        client = GatewayMqttClient()
        stub_body = {
            'modem_index': 0,
            'gathered_at': 't',
            'mmcli_flat': {'modem.generic.state': 'enabled'},
            'success': True,
            'error': None,
        }
        with (
            patch.object(client, '_modem_status_list_indices', return_value=[0]),
            patch('apps.external_device.mqtt_client.build_modem_status_mqtt_payload', return_value=stub_body),
        ):
            client._modem_status_publish_all(event='bootstrap')

        mock_ci.publish.assert_called_once()
        topic, payload_s = mock_ci.publish.call_args[0][:2]
        assert topic.endswith('/modems/0/status/telemetry')
        assert json.loads(payload_s)['event'] == 'bootstrap'
        assert 0 in client._modem_status_fingerprints

    @patch('apps.external_device.mqtt_client.mqtt.Client')
    def test_modem_status_poll_tick_publishes_on_change_only(self, mqtt_cls):
        mock_ci = MagicMock()
        mqtt_cls.return_value = mock_ci

        client = GatewayMqttClient()

        stub_v1 = {
            'modem_index': 0,
            'gathered_at': 't',
            'mmcli_flat': {'modem.generic.state': 'enabled'},
            'success': True,
            'error': None,
        }
        stub_v2 = {
            'modem_index': 0,
            'gathered_at': 't',
            'mmcli_flat': {'modem.generic.state': 'registered'},
            'success': True,
            'error': None,
        }

        with patch.object(client, '_modem_status_list_indices', return_value=[0]):
            with patch(
                'apps.external_device.mqtt_client.build_modem_status_mqtt_payload',
                side_effect=[stub_v1, stub_v1, stub_v2],
            ):
                client._modem_status_poll_tick()
                assert mock_ci.publish.call_count == 1
                assert json.loads(mock_ci.publish.call_args[0][1])['event'] == 'state_change'

                client._modem_status_poll_tick()
                assert mock_ci.publish.call_count == 1

                client._modem_status_poll_tick()
                assert mock_ci.publish.call_count == 2
                assert json.loads(mock_ci.publish.call_args[0][1])['event'] == 'state_change'

    @patch('apps.external_device.mqtt_client.mqtt.Client')
    @override_settings(MQTT_MODEM_STATUS_AUTO_PUBLISH=True)
    @patch('apps.external_device.mqtt_client.threading.Thread')
    def test_on_connect_spawns_modem_push_thread_when_auto_enabled(self, mock_thread, mqtt_cls):
        mock_ci = MagicMock()
        mqtt_cls.return_value = mock_ci
        client = GatewayMqttClient()
        client._on_connect(mock_ci, None, {}, 0)
        mock_thread.assert_called_once()
        bound = mock_thread.call_args.kwargs['target']
        assert bound.__self__ is client
        assert bound.__func__.__name__ == '_modem_status_auto_publish_runner'
        assert mock_thread.call_args.kwargs['daemon'] is True

    @patch('apps.external_device.mqtt_client.mqtt.Client')
    @patch('apps.external_device.mqtt_client.threading.Thread')
    def test_on_connect_skips_modem_push_when_auto_disabled(self, mock_thread, mqtt_cls):
        mock_ci = MagicMock()
        mqtt_cls.return_value = mock_ci
        client = GatewayMqttClient()
        client._on_connect(mock_ci, None, {}, 0)
        mock_thread.assert_not_called()

    @patch('apps.external_device.mqtt_client.mqtt.Client')
    def test_on_disconnect_signals_modem_push_stop(self, mqtt_cls):
        mock_ci = MagicMock()
        mqtt_cls.return_value = mock_ci
        client = GatewayMqttClient()
        client._modem_push_stop.clear()
        client._on_disconnect(mock_ci, None, 1)
        assert client._modem_push_stop.is_set()


def test_mqtt_short_client_id_length():
    from apps.external_device import mqtt_client as mc

    for _ in range(5):
        assert len(mc._mqtt_short_client_id()) <= 23

