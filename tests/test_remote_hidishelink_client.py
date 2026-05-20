"""Tests for RemoteHiDishelinkClient (hiDisheLink bridge mode)."""

from unittest.mock import MagicMock, patch

import pytest
from django.test import override_settings
from django.utils import timezone

from apps.external_device.mqtt_client import RemoteHiDishelinkClient
from apps.external_device.services import handle_remote_sms_send, publish_inbound_to_remote
from apps.sms.models import InboundSms, OutboundSms


class TestRemoteHiDishelinkClient:
    """Tests for RemoteHiDishelinkClient initialization and basic operations."""
    
    @pytest.fixture
    def mqtt_config(self):
        """Sample mqtt-config response from hiDisheLink API."""
        return {
            'MQTT_BROKER_URL': '192.168.1.77',
            'MQTT_PORT': 1883,
            'MQTT_USERNAME': 'testuser',
            'MQTT_PASSWORD': 'testpass',
            'MQTT_KEEPALIVE': 60,
            'MQTT_QOS': 1,
            'MQTT_CLEAN_SESSION': False,
            'TOPIC_SMS_SEND': 'hidishelink_dev/devices/{device_id}/sms/send',
            'TOPIC_SMS_STATUS': 'hidishelink_dev/devices/{device_id}/sms/status',
            'TOPIC_SMS_INBOX': 'hidishelink_dev/devices/{device_id}/sms/inbox',
            'TOPIC_SMS_INBOX_ACK': 'hidishelink_dev/devices/{device_id}/sms/inbox/ack',
            'TOPIC_HEALTH_PING': 'hidishelink_dev/devices/{device_id}/health/ping',
            'TOPIC_HEALTH_PONG': 'hidishelink_dev/devices/{device_id}/health/pong',
        }
    
    @pytest.fixture
    def remote_client(self, mqtt_config):
        """Create RemoteHiDishelinkClient instance with mocked Paho client."""
        with patch('apps.external_device.mqtt_client.mqtt.Client') as mock_client_class:
            mock_client = MagicMock()
            mock_client_class.return_value = mock_client
            
            client = RemoteHiDishelinkClient(mqtt_config, '+351912329317')
            client.client = mock_client
            return client
    
    def test_client_initialization(self, mqtt_config):
        """Test RemoteHiDishelinkClient initialization from mqtt-config."""
        with patch('apps.external_device.mqtt_client.mqtt.Client'):
            client = RemoteHiDishelinkClient(mqtt_config, '+351912329317')
        
        assert client.device_id == '+351912329317'
        assert client.sanitized_device_id == '351912329317'
        assert client.broker_url == '192.168.1.77'
        assert client.port == 1883
        assert client.qos == 1
        assert client.clean_session is False
        assert 'hidishelink_dev/devices/351912329317/sms/send' in client.topic_sms_send
    
    def test_topic_resolution(self, remote_client):
        """Test topic resolution with sanitized device_id."""
        assert remote_client.topic_sms_send == 'hidishelink_dev/devices/351912329317/sms/send'
        assert remote_client.topic_sms_status == 'hidishelink_dev/devices/351912329317/sms/status'
        assert remote_client.topic_sms_inbox == 'hidishelink_dev/devices/351912329317/sms/inbox'
        assert remote_client.topic_health_ping == 'hidishelink_dev/devices/351912329317/health/ping'
        assert remote_client.topic_health_pong == 'hidishelink_dev/devices/351912329317/health/pong'
    
    def test_publish_sms_status_qos1(self, remote_client):
        """Test SMS status publishing uses QoS 1."""
        payload = {'sent': 1, 'failed': 0, 'details': []}
        remote_client.publish_sms_status('req_123', 'success', payload)
        
        # Verify QoS 1 was used
        call_args = remote_client.client.publish.call_args
        assert call_args[1]['qos'] == 1
    
    def test_publish_health_pong_qos1(self, remote_client):
        """Test health pong publishing uses QoS 1."""
        remote_client.publish_health_pong('ping_abc123', '2026-05-20T12:00:00Z')
        
        # Verify QoS 1 was used
        call_args = remote_client.client.publish.call_args
        assert call_args[1]['qos'] == 1
    
    def test_publish_inbox_qos1(self, remote_client):
        """Test inbox publishing uses QoS 1."""
        remote_client.publish_sms_inbox('msg_001', '+351912345678', 'Hello', '2026-05-20T12:00:00Z')
        
        # Verify QoS 1 was used
        call_args = remote_client.client.publish.call_args
        assert call_args[1]['qos'] == 1
    
    def test_chunking_buffer_single_chunk(self, remote_client):
        """Test SMS send with single chunk (no buffering)."""
        payload = {
            'request_id': 'req_123',
            'recipients': ['+351912345678'],
            'message': 'Test',
        }
        
        with patch('apps.external_device.mqtt_client.threading.Thread') as mock_thread:
            remote_client._handle_sms_send(payload)
            # Should spawn thread immediately
            mock_thread.assert_called_once()
    
    def test_chunking_buffer_multiple_chunks(self, remote_client):
        """Test SMS send with chunking aggregation."""
        # First chunk
        payload1 = {
            'request_id': 'req_123',
            'recipients': ['+351912345678'],
            'message': 'Test',
            'chunk_index': 0,
            'chunk_total': 2,
        }
        
        with patch('apps.external_device.mqtt_client.threading.Thread') as mock_thread:
            remote_client._handle_sms_send(payload1)
            # Should not spawn thread yet
            assert mock_thread.call_count == 0
            
            # Buffer should contain chunk
            assert 'req_123' in remote_client._chunk_buffer
            assert 0 in remote_client._chunk_buffer['req_123']['chunks']
            
            # Second chunk
            payload2 = {
                'request_id': 'req_123',
                'recipients': ['+351987654321'],
                'message': 'Test',
                'chunk_index': 1,
                'chunk_total': 2,
            }
            
            remote_client._handle_sms_send(payload2)
            # Should spawn thread with aggregated recipients
            assert mock_thread.call_count == 1
            
            # Buffer should be cleared
            assert 'req_123' not in remote_client._chunk_buffer


@pytest.mark.django_db
class TestRemoteSmsHandlers:
    """Tests for remote SMS handler functions."""
    
    @pytest.fixture
    def remote_client(self):
        """Mock RemoteHiDishelinkClient."""
        client = MagicMock()
        client.device_id = '+351912329317'
        client.publish_sms_status = MagicMock(return_value=True)
        return client
    
    @override_settings(MODEM_MMCLI_INDEX=0)
    def test_handle_remote_sms_send_success(self, remote_client):
        """Test successful SMS send via remote handler."""
        payload = {
            'request_id': 'req_123',
            'recipients': ['+351912345678'],
            'message': 'Test message',
            'priority': 'normal',
        }
        
        with patch('apps.external_device.services.dispatch_outbound_mmcli') as mock_dispatch:
            # Mock successful dispatch
            def mock_dispatch_fn(outbound):
                outbound.state = OutboundSms.State.SENT
                outbound.mm_path = '/org/freedesktop/ModemManager1/SMS/1'
                outbound.save()
            
            mock_dispatch.side_effect = mock_dispatch_fn
            
            handle_remote_sms_send(remote_client, payload)
            
            # Should publish received ACK
            assert remote_client.publish_sms_status.call_count >= 2
            first_call = remote_client.publish_sms_status.call_args_list[0]
            assert first_call[0][1] == 'received'
            
            # Should publish success status
            last_call = remote_client.publish_sms_status.call_args_list[-1]
            assert last_call[0][1] == 'success'
            assert last_call[0][2]['sent'] == 1
            assert last_call[0][2]['failed'] == 0
    
    def test_handle_remote_sms_send_invalid_payload(self, remote_client):
        """Test handler with invalid payload."""
        payload = {'request_id': 'req_123'}  # Missing recipients
        
        handle_remote_sms_send(remote_client, payload)
        
        # Should publish error status
        remote_client.publish_sms_status.assert_called_once()
        call_args = remote_client.publish_sms_status.call_args
        assert call_args[0][1] == 'error'
    
    @override_settings(MODEM_MMCLI_INDEX=0)
    @pytest.mark.django_db
    def test_publish_inbound_to_remote(self, remote_client):
        """Test publishing InboundSms to remote broker."""
        inbound = InboundSms.objects.create(
            mm_path='/org/freedesktop/ModemManager1/SMS/1',
            modem_index=0,
            from_number='+351912345678',
            text='Inbound message',
            mm_state='received',
        )
        
        remote_client.publish_sms_inbox = MagicMock(return_value=True)
        
        result = publish_inbound_to_remote(inbound, remote_client)
        
        assert result is True
        remote_client.publish_sms_inbox.assert_called_once()
        call_args = remote_client.publish_sms_inbox.call_args[0]
        assert call_args[1] == '+351912345678'
        assert call_args[2] == 'Inbound message'
    
    @pytest.mark.django_db
    def test_publish_inbound_to_remote_not_ready(self, remote_client):
        """Test publishing InboundSms that's not ready (no sender/body)."""
        inbound = InboundSms.objects.create(
            mm_path='/org/freedesktop/ModemManager1/SMS/1',
            modem_index=0,
            from_number='',
            text='',
            mm_state='receiving',
        )
        
        remote_client.publish_sms_inbox = MagicMock()
        
        result = publish_inbound_to_remote(inbound, remote_client)
        
        assert result is False
        remote_client.publish_sms_inbox.assert_not_called()


class TestRemoteHealthHandlers:
    """Tests for remote health check handlers."""
    
    def test_handle_health_ping_django_source(self):
        """Test handling health ping with source=django."""
        remote_client = MagicMock()
        remote_client.publish_health_pong = MagicMock(return_value=True)
        
        payload = {
            'source': 'django',
            'ping_id': 'ping_abc123',
            'timestamp': '2026-05-20T12:00:00Z',
        }
        
        remote_client._handle_health_ping(payload)
        
        # Should publish pong with same ping_id
        remote_client.publish_health_pong.assert_called_once_with(
            'ping_abc123',
            '2026-05-20T12:00:00Z',
        )
    
    def test_handle_health_ping_no_django_source(self):
        """Test handling health ping without source=django (should ignore)."""
        remote_client = MagicMock()
        remote_client.publish_health_pong = MagicMock()
        
        payload = {
            'timestamp': '2026-05-20T12:00:00Z',
            'battery_level': 100,
        }
        
        remote_client._handle_health_ping(payload)
        
        # Should not publish pong
        remote_client.publish_health_pong.assert_not_called()
    
    def test_health_heartbeat_payload(self):
        """Test health heartbeat payload format (tipo A telemetry)."""
        with patch('apps.external_device.mqtt_client.mqtt.Client'):
            with patch('apps.external_device.mqtt_client.timezone.now') as mock_now:
                mock_now.return_value = timezone.datetime(2026, 5, 20, 12, 0, 0)
                
                mqtt_config = {
                    'MQTT_BROKER_URL': '192.168.1.77',
                    'MQTT_PORT': 1883,
                    'TOPIC_HEALTH_PING': 'hidishelink_dev/devices/{device_id}/health/ping',
                }
                
                client = RemoteHiDishelinkClient(mqtt_config, '+351912329317')
                client.client = MagicMock()
                
                result = client.publish_health_heartbeat()
                
                # Should publish with battery_level and network_type (no source:django)
                call_args = client.client.publish.call_args
                import json
                payload = json.loads(call_args[0][1])
                
                assert 'battery_level' in payload
                assert 'network_type' in payload
                assert payload.get('source') != 'django'
                assert call_args[1]['qos'] == 1  # QoS 1


@pytest.mark.django_db
class TestChunkCleanup:
    """Tests for chunk buffer TTL cleanup."""
    
    def test_cleanup_expired_chunks(self):
        """Test expired chunk buffers are cleaned up."""
        with patch('apps.external_device.mqtt_client.mqtt.Client'):
            mqtt_config = {
                'MQTT_BROKER_URL': '192.168.1.77',
                'MQTT_PORT': 1883,
            }
            
            client = RemoteHiDishelinkClient(mqtt_config, '+351912329317')
            
            # Add expired chunk
            old_time = timezone.now() - timezone.timedelta(seconds=400)
            client._chunk_buffer['req_old'] = {
                'chunks': {0: ['+351912345678']},
                'chunk_total': 2,
                'timestamp': old_time,
            }
            
            # Add recent chunk
            client._chunk_buffer['req_recent'] = {
                'chunks': {0: ['+351987654321']},
                'chunk_total': 2,
                'timestamp': timezone.now(),
            }
            
            client._cleanup_expired_chunks()
            
            # Old chunk should be removed
            assert 'req_old' not in client._chunk_buffer
            # Recent chunk should remain
            assert 'req_recent' in client._chunk_buffer
