"""DRF serializers for external device gateway API."""

from __future__ import annotations

from rest_framework import serializers

from .models import ExternalDevice, InboxMessage, SmsRecipientStatus, SmsRequest


class RegisterDeviceSerializer(serializers.Serializer):
    """Serializer for device registration request."""

    device_id = serializers.CharField(max_length=64)
    registration_token = serializers.CharField(max_length=255)
    name = serializers.CharField(max_length=255)
    device_type = serializers.CharField(max_length=64, default='modem')
    mqtt_client_id = serializers.CharField(max_length=255, required=False, allow_blank=True)
    metadata = serializers.JSONField(required=False, default=dict)


class RegisterDeviceResponseSerializer(serializers.Serializer):
    """Serializer for device registration response."""

    api_key = serializers.CharField()
    device_id = serializers.CharField()
    status = serializers.CharField()


class SmsSendRequestSerializer(serializers.Serializer):
    """Serializer for SMS send request."""

    recipients = serializers.ListField(
        child=serializers.CharField(max_length=64),
        allow_empty=False,
    )
    message = serializers.CharField()
    priority = serializers.ChoiceField(
        choices=['normal', 'high', 'urgent'],
        default='normal',
    )


class SmsSendResponseSerializer(serializers.Serializer):
    """Serializer for SMS send response."""

    request_id = serializers.CharField()
    status = serializers.CharField()


class SmsRecipientStatusSerializer(serializers.ModelSerializer):
    """Serializer for per-recipient SMS status."""

    class Meta:
        model = SmsRecipientStatus
        fields = ['phone_number', 'status', 'message_id', 'error_message']


class SmsStatusResponseSerializer(serializers.ModelSerializer):
    """Serializer for SMS status response."""

    recipients = SmsRecipientStatusSerializer(source='recipient_statuses', many=True, read_only=True)

    class Meta:
        model = SmsRequest
        fields = ['request_id', 'status', 'sent_count', 'failed_count', 'recipients']


class InboxMessageSerializer(serializers.ModelSerializer):
    """Serializer for inbox messages."""

    class Meta:
        model = InboxMessage
        fields = ['message_id', 'sender', 'body', 'received_at']


class DeviceHealthSerializer(serializers.ModelSerializer):
    """Serializer for device health status."""

    class Meta:
        model = ExternalDevice
        fields = ['device_id', 'status', 'is_available', 'last_seen']


class DeviceHealthPingResponseSerializer(serializers.Serializer):
    """Response after publishing an active MQTT health ping (hiDisheLink)."""

    ping_id = serializers.CharField()
    timestamp = serializers.CharField()
    source = serializers.CharField()
    published = serializers.BooleanField()
    mqtt_topic = serializers.CharField()
