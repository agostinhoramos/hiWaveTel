"""Serialisers, validators, model helpers."""

from __future__ import annotations

import pytest
from django.core.exceptions import ValidationError

from apps.sms.models import InboundSms, OutboundSms
from apps.sms.serializers import OutboundSmsCreateSerializer
from apps.sms.validators import sms_destination_validator

pytestmark = pytest.mark.django_db


def test_outbound_serialiser_validates_digit_length():
    ser = OutboundSmsCreateSerializer(data={'to': '+449111', 'text': 'hello'})
    assert not ser.is_valid()


def test_outbound_serialiser_requires_text():
    ser = OutboundSmsCreateSerializer(data={'to': '+4412345678910'})
    assert not ser.is_valid()


def test_inbound_str_representation(db):
    row = InboundSms.objects.create(
        mm_path='/org/freedesktop/ModemManager1/SMS/99',
        modem_index=0,
        from_number='+44123456789',
        text='x',
    )
    assert '+44123456789' in str(row)


def test_outbound_str_representation(db):
    row = OutboundSms.objects.create(
        modem_index=0,
        to_number='+4412345678910',
        text='hello',
        state=OutboundSms.State.CREATED,
    )
    s = str(row)
    assert '4412345678910' in s


def test_sms_destination_validator_accepts_international_numbers():
    sms_destination_validator(' +44 7911 112233 ')
    sms_destination_validator('00447911112233')


def test_sms_destination_validator_requires_min_digits():
    with pytest.raises(ValidationError):
        sms_destination_validator('+123456')


def test_sms_destination_validator_rejects_blank_after_strip():
    with pytest.raises(ValidationError):
        sms_destination_validator('   ')


def test_sms_destination_validator_rejects_more_than_fifteen_digits():
    too_long = '+' + '1' * 16
    with pytest.raises(ValidationError):
        sms_destination_validator(too_long)
