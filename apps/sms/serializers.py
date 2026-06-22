from django.conf import settings
from django.core.validators import MinLengthValidator
from rest_framework import serializers

from .models import InboundSms, InboundWebhook, OutboundSms
from .validators import sms_destination_validator


class InboundSmsSerializer(serializers.ModelSerializer):
    """Read-only inbound SMS persisted from ModemManager."""

    class Meta:
        model = InboundSms
        fields = (
            'id',
            'mm_path',
            'modem_index',
            'from_number',
            'text',
            'mm_state',
            'smsc',
            'modem_timestamp_raw',
            'created_at',
        )
        read_only_fields = fields


class OutboundSmsSerializer(serializers.ModelSerializer):
    class Meta:
        model = OutboundSms
        fields = (
            'id',
            'mm_path',
            'modem_index',
            'to_number',
            'text',
            'state',
            'error_message',
            'created_at',
            'updated_at',
        )
        read_only_fields = (
            'id',
            'mm_path',
            'state',
            'error_message',
            'created_at',
            'updated_at',
        )


class OutboundSmsCreateSerializer(serializers.Serializer):
    modem_index = serializers.IntegerField(required=False, min_value=0, default=settings.MODEM_MMCLI_INDEX)
    to = serializers.CharField(
        required=True,
        max_length=64,
        validators=[sms_destination_validator],
    )
    text = serializers.CharField(
        required=True,
        trim_whitespace=False,
        validators=[MinLengthValidator(1)],
        help_text=(
            'Full SMS payload (UTF-8). Length is constrained by modem/network encoding, '
            'not by this gateway.'
        ),
    )


class ReadinessIssueSerializer(serializers.Serializer):
    code = serializers.CharField()
    message = serializers.CharField()


class SystemReadinessSerializer(serializers.Serializer):
    ready = serializers.BooleanField()
    phone_number = serializers.CharField(allow_blank=True)
    modem_index = serializers.IntegerField(allow_null=True)
    modem_state = serializers.CharField()
    checked_at = serializers.CharField()
    capabilities = serializers.DictField(child=serializers.BooleanField())
    issues = ReadinessIssueSerializer(many=True)
    components = serializers.DictField()
    last_persisted_at = serializers.CharField(allow_null=True, required=False)


class ModemLastActivitySerializer(serializers.Serializer):
    at = serializers.CharField(allow_null=True)
    source = serializers.CharField(allow_null=True)
    inbound_sms_at = serializers.CharField(allow_null=True, required=False)
    outbound_sms_at = serializers.CharField(allow_null=True, required=False)
    device_last_seen_at = serializers.CharField(allow_null=True, required=False)
    readiness_checked_at = serializers.CharField(allow_null=True, required=False)


class ModemAvailabilitySerializer(serializers.Serializer):
    modem_index = serializers.IntegerField()
    available = serializers.BooleanField()
    state = serializers.CharField()
    checked_at = serializers.CharField()
    enumerated_indices = serializers.ListField(child=serializers.IntegerField())
    ping_ok = serializers.BooleanField(allow_null=True, required=False)
    phone_number = serializers.CharField(allow_blank=True, required=False)
    detail = serializers.CharField()
    last_activity = ModemLastActivitySerializer()


class ContainerRestartSerializer(serializers.Serializer):
    accepted = serializers.BooleanField()
    message = serializers.CharField()
    scheduled_at = serializers.CharField()
    delay_sec = serializers.FloatField()
    requested_by = serializers.CharField()


class ModemDeviceSummarySerializer(serializers.Serializer):
    modem_index = serializers.IntegerField()
    enabled = serializers.BooleanField()
    is_present = serializers.BooleanField()
    dbus_path = serializers.CharField()
    phone_number = serializers.CharField()
    manufacturer = serializers.CharField()
    model = serializers.CharField()
    state = serializers.CharField()
    available = serializers.BooleanField()
    first_detected_at = serializers.CharField()
    last_detected_at = serializers.CharField()


class ModemDeviceDetailSerializer(serializers.Serializer):
    modem_index = serializers.IntegerField()
    enabled = serializers.BooleanField()
    is_present = serializers.BooleanField()
    dbus_path = serializers.CharField()
    phone_number = serializers.CharField()
    manufacturer = serializers.CharField()
    model = serializers.CharField()
    imei = serializers.CharField()
    firmware = serializers.CharField()
    sim_path = serializers.CharField()
    state = serializers.CharField()
    available = serializers.BooleanField()
    checked_at = serializers.CharField()
    enumerated_indices = serializers.ListField(child=serializers.IntegerField())
    ping_ok = serializers.BooleanField(allow_null=True, required=False)
    detail = serializers.CharField()
    last_activity = ModemLastActivitySerializer()
    first_detected_at = serializers.CharField()
    last_detected_at = serializers.CharField()


class ModemDeviceUpdateSerializer(serializers.Serializer):
    enabled = serializers.BooleanField()


class InboundWebhookSerializer(serializers.ModelSerializer):
    class Meta:
        model = InboundWebhook
        fields = ('id', 'modem_index', 'name', 'url', 'enabled', 'created_at')
        read_only_fields = fields


class InboundWebhookCreateSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=128)
    url = serializers.URLField(max_length=512)
    enabled = serializers.BooleanField(required=False, default=True)

    def validate_url(self, value: str) -> str:
        from apps.sms.webhook_delivery import normalize_webhook_url

        normalized = normalize_webhook_url(value)
        if normalized != (value or '').strip():
            return normalized
        return value
