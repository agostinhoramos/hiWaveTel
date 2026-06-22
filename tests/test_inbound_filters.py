"""Tests for inbound webhook echo detection."""

from __future__ import annotations

import pytest

from apps.sms.inbound_filters import find_matching_outbound, inbound_should_skip_webhook
from apps.sms.models import InboundSms, OutboundSms


@pytest.mark.django_db
def test_find_matching_outbound_by_mm_path():
    path = '/org/freedesktop/ModemManager1/SMS/99'
    outbound = OutboundSms.objects.create(
        modem_index=0,
        to_number='+351913000387',
        text='hello echo',
        state=OutboundSms.State.SENT,
        mm_path=path,
    )
    inbound = InboundSms.objects.create(
        mm_path=path,
        modem_index=0,
        from_number='',
        text='hello echo',
        mm_state='sent',
    )
    assert find_matching_outbound(inbound) == outbound
    assert inbound_should_skip_webhook(inbound) is True


@pytest.mark.django_db
def test_find_matching_outbound_by_sent_mm_state_and_text():
    outbound = OutboundSms.objects.create(
        modem_index=0,
        to_number='+351913000387',
        text='api message',
        state=OutboundSms.State.SENT,
        mm_path='/org/freedesktop/ModemManager1/SMS/100',
    )
    inbound = InboundSms.objects.create(
        mm_path='/org/freedesktop/ModemManager1/SMS/101',
        modem_index=0,
        from_number='unknown',
        text='api message',
        mm_state='sent',
    )
    assert find_matching_outbound(inbound) == outbound
    assert inbound_should_skip_webhook(inbound) is True


@pytest.mark.django_db
def test_inbound_should_not_skip_unrelated_message():
    OutboundSms.objects.create(
        modem_index=0,
        to_number='+351913000387',
        text='sent by api',
        state=OutboundSms.State.SENT,
    )
    inbound = InboundSms.objects.create(
        mm_path='/org/freedesktop/ModemManager1/SMS/102',
        modem_index=0,
        from_number='+351912345678',
        text='real inbound',
        mm_state='received',
    )
    assert find_matching_outbound(inbound) is None
    assert inbound_should_skip_webhook(inbound) is False
