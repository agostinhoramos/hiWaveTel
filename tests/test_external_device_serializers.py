"""Tests for external device serializers."""

from __future__ import annotations

import pytest

from apps.external_device.serializers import (
    RegisterDeviceSerializer,
    SmsSendRequestSerializer,
)


class TestRegisterDeviceSerializer:
    """Test RegisterDeviceSerializer."""

    def test_valid_data_with_all_fields(self):
        """Should validate when all fields are provided."""
        data = {
            'device_id': '+351913000387',
            'registration_token': 'test-token-123',
            'name': 'Test Device',
            'device_type': 'modem',
            'mqtt_client_id': 'mqtt-client-123',
            'metadata': {'key': 'value'}
        }
        
        serializer = RegisterDeviceSerializer(data=data)
        assert serializer.is_valid()
        assert serializer.validated_data['device_id'] == '+351913000387'
        assert serializer.validated_data['name'] == 'Test Device'

    def test_valid_data_with_required_fields_only(self):
        """Should validate with only required fields."""
        data = {
            'device_id': '+351913000387',
            'registration_token': 'test-token-123',
            'name': 'Test Device'
        }
        
        serializer = RegisterDeviceSerializer(data=data)
        assert serializer.is_valid()
        assert serializer.validated_data['device_type'] == 'modem'
        assert serializer.validated_data['metadata'] == {}

    def test_invalid_missing_device_id(self):
        """Should fail validation when device_id is missing."""
        data = {
            'registration_token': 'test-token-123',
            'name': 'Test Device'
        }
        
        serializer = RegisterDeviceSerializer(data=data)
        assert not serializer.is_valid()
        assert 'device_id' in serializer.errors

    def test_invalid_missing_registration_token(self):
        """Should fail validation when registration_token is missing."""
        data = {
            'device_id': '+351913000387',
            'name': 'Test Device'
        }
        
        serializer = RegisterDeviceSerializer(data=data)
        assert not serializer.is_valid()
        assert 'registration_token' in serializer.errors

    def test_invalid_missing_name(self):
        """Should fail validation when name is missing."""
        data = {
            'device_id': '+351913000387',
            'registration_token': 'test-token-123'
        }
        
        serializer = RegisterDeviceSerializer(data=data)
        assert not serializer.is_valid()
        assert 'name' in serializer.errors

    def test_mqtt_client_id_can_be_empty(self):
        """Should allow empty mqtt_client_id."""
        data = {
            'device_id': '+351913000387',
            'registration_token': 'test-token-123',
            'name': 'Test Device',
            'mqtt_client_id': ''
        }
        
        serializer = RegisterDeviceSerializer(data=data)
        assert serializer.is_valid()
        assert serializer.validated_data['mqtt_client_id'] == ''

    def test_device_id_max_length(self):
        """Should enforce max_length on device_id."""
        data = {
            'device_id': 'x' * 65,
            'registration_token': 'test-token-123',
            'name': 'Test Device'
        }
        
        serializer = RegisterDeviceSerializer(data=data)
        assert not serializer.is_valid()
        assert 'device_id' in serializer.errors


class TestSmsSendRequestSerializer:
    """Test SmsSendRequestSerializer."""

    def test_valid_data_with_all_fields(self):
        """Should validate with all fields provided."""
        data = {
            'recipients': ['+351912345678', '+351987654321'],
            'message': 'Test SMS message',
            'priority': 'high'
        }
        
        serializer = SmsSendRequestSerializer(data=data)
        assert serializer.is_valid()
        assert len(serializer.validated_data['recipients']) == 2
        assert serializer.validated_data['priority'] == 'high'

    def test_valid_data_with_default_priority(self):
        """Should use default priority when not specified."""
        data = {
            'recipients': ['+351912345678'],
            'message': 'Test message'
        }
        
        serializer = SmsSendRequestSerializer(data=data)
        assert serializer.is_valid()
        assert serializer.validated_data['priority'] == 'normal'

    def test_invalid_empty_recipients(self):
        """Should fail validation when recipients list is empty."""
        data = {
            'recipients': [],
            'message': 'Test message'
        }
        
        serializer = SmsSendRequestSerializer(data=data)
        assert not serializer.is_valid()
        assert 'recipients' in serializer.errors

    def test_invalid_missing_recipients(self):
        """Should fail validation when recipients field is missing."""
        data = {
            'message': 'Test message'
        }
        
        serializer = SmsSendRequestSerializer(data=data)
        assert not serializer.is_valid()
        assert 'recipients' in serializer.errors

    def test_invalid_missing_message(self):
        """Should fail validation when message is missing."""
        data = {
            'recipients': ['+351912345678']
        }
        
        serializer = SmsSendRequestSerializer(data=data)
        assert not serializer.is_valid()
        assert 'message' in serializer.errors

    def test_invalid_priority_choice(self):
        """Should fail validation for invalid priority value."""
        data = {
            'recipients': ['+351912345678'],
            'message': 'Test message',
            'priority': 'invalid'
        }
        
        serializer = SmsSendRequestSerializer(data=data)
        assert not serializer.is_valid()
        assert 'priority' in serializer.errors

    def test_valid_priority_choices(self):
        """Should validate all valid priority choices."""
        for priority in ['normal', 'high', 'urgent']:
            data = {
                'recipients': ['+351912345678'],
                'message': 'Test message',
                'priority': priority
            }
            
            serializer = SmsSendRequestSerializer(data=data)
            assert serializer.is_valid(), f'Priority {priority} should be valid'
            assert serializer.validated_data['priority'] == priority

    def test_single_recipient(self):
        """Should validate with single recipient."""
        data = {
            'recipients': ['+351912345678'],
            'message': 'Test message'
        }
        
        serializer = SmsSendRequestSerializer(data=data)
        assert serializer.is_valid()
        assert len(serializer.validated_data['recipients']) == 1

    def test_multiple_recipients(self):
        """Should validate with multiple recipients."""
        data = {
            'recipients': ['+351912345678', '+351987654321', '+351911111111'],
            'message': 'Test message'
        }
        
        serializer = SmsSendRequestSerializer(data=data)
        assert serializer.is_valid()
        assert len(serializer.validated_data['recipients']) == 3

    def test_recipient_max_length(self):
        """Should enforce max_length on recipient phone numbers."""
        data = {
            'recipients': ['x' * 65],
            'message': 'Test message'
        }
        
        serializer = SmsSendRequestSerializer(data=data)
        assert not serializer.is_valid()
        assert 'recipients' in serializer.errors
