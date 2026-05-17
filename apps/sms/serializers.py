from django.conf import settings
from django.core.validators import MinLengthValidator
from rest_framework import serializers

from .models import InboundSms, OutboundSms
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
