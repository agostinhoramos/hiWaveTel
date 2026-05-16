"""``services`` helpers: inbound persistence and outbound dispatch."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from apps.sms.mmcli_client import MMCLIClient, MmcliError
from apps.sms.models import InboundSms, OutboundSms
from apps.sms.services import dispatch_outbound_mmcli, format_public_mmcli_error, persist_inbound_sms

pytestmark = pytest.mark.django_db


def test_persist_creates_from_show():
    path = '/org/freedesktop/ModemManager1/SMS/z1'
    client = MMCLIClient()
    client.show_sms = MagicMock(
        return_value={
            'number': '+4498765432111',
            'text': 'hello',
            'state': 'received',
            'smsc': '+440000',
            'timestamp': '2024-05-05T01:02:03Z',
        },
    )
    obj = persist_inbound_sms(path, 3, client)
    assert obj.mm_path == path
    assert obj.from_number == '+4498765432111'
    assert InboundSms.objects.count() == 1


def test_persist_handles_show_failure_gracefully():
    bad = MMCLIClient()
    bad.show_sms = MagicMock(side_effect=MmcliError('nope'))
    obj = persist_inbound_sms('/org/freedesktop/ModemManager1/SMS/x9', 0, bad)
    assert obj.mm_path == '/org/freedesktop/ModemManager1/SMS/x9'
    assert obj.modem_index == 0


def test_persist_patches_existing_blank_strings():
    path = '/org/freedesktop/ModemManager1/SMS/patch_blank'
    InboundSms.objects.create(
        mm_path=path,
        modem_index=0,
        from_number='',
        text='',
    )
    client = MMCLIClient()
    client.show_sms = MagicMock(return_value={'number': '+4498765432111', 'text': 'patched'})
    obj = persist_inbound_sms(path, 0, client)
    obj.refresh_from_db()
    assert obj.from_number == '+4498765432111'
    assert obj.text == 'patched'


def test_dispatch_success_updates_state():
    outbound = OutboundSms.objects.create(
        modem_index=0,
        to_number='+4412345678910',
        text='svc',
        state=OutboundSms.State.CREATED,
    )
    dummy = MMCLIClient()
    dummy.ensure_modem_index = MagicMock(return_value=None)
    dummy.create_sms = MagicMock(return_value='/org/freedesktop/ModemManager1/SMS/3')
    dummy.send_sms = MagicMock(return_value=None)
    updated = dispatch_outbound_mmcli(outbound, client=dummy)
    assert updated.state == OutboundSms.State.SENT
    assert updated.mm_path == '/org/freedesktop/ModemManager1/SMS/3'


def test_dispatch_send_failure_marks_failed():
    outbound = OutboundSms.objects.create(
        modem_index=0,
        to_number='+4412345678910',
        text='svc',
        state=OutboundSms.State.CREATED,
    )
    dummy = MMCLIClient()
    dummy.ensure_modem_index = MagicMock(return_value=None)
    dummy.create_sms = MagicMock(return_value='/org/freedesktop/ModemManager1/SMS/4')
    dummy.send_sms = MagicMock(side_effect=MmcliError('modem hung up'))
    updated = dispatch_outbound_mmcli(outbound, client=dummy)
    assert updated.state == OutboundSms.State.FAILED
    assert updated.error_message


def test_format_public_mmcli_error_truncates_stderr():
    err = MmcliError('root', stderr='first line stderr\nsecret line')
    formatted = format_public_mmcli_error(err)
    assert 'root' in formatted
    assert len(formatted) <= 205
