"""``services`` helpers: inbound persistence and outbound dispatch."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from django.test import override_settings

from apps.sms.mmcli_client import MMCLIClient, MmcliError
from apps.sms.models import InboundSms, OutboundSms
from apps.sms.services import (
    _inbound_field_should_update,
    dispatch_outbound_mmcli,
    format_public_mmcli_error,
    persist_inbound_sms,
    refresh_stale_inbound_sms_rows,
)

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


def test_inbound_field_should_update_mm_state_from_receiving():
    assert _inbound_field_should_update('mm_state', 'receiving', 'received') is True
    assert _inbound_field_should_update('mm_state', 'received', 'receiving') is False


def test_inbound_field_should_update_text_when_longer():
    assert _inbound_field_should_update('text', '', 'hello') is True
    assert _inbound_field_should_update('text', 'hi', 'hello world') is True
    assert _inbound_field_should_update('text', 'hello world', 'hi') is False


def test_patch_updates_mm_state_from_receiving_to_received():
    path = '/org/freedesktop/ModemManager1/SMS/stuck101'
    InboundSms.objects.create(
        mm_path=path,
        modem_index=0,
        from_number='+351961343706',
        text='',
        mm_state='receiving',
    )
    client = MMCLIClient()
    client.show_sms = MagicMock(
        return_value={
            'number': '+351961343706',
            'text': 'second message body',
            'state': 'received',
        },
    )
    obj = persist_inbound_sms(path, 0, client)
    obj.refresh_from_db()
    assert obj.mm_state == 'received'
    assert obj.text == 'second message body'


def test_receiving_polls_until_text_arrives():
    path = '/org/freedesktop/ModemManager1/SMS/poll1'
    client = MMCLIClient()
    client.show_sms = MagicMock(
        side_effect=[
            {'number': '+351900000001', 'text': '', 'state': 'receiving'},
            {'number': '+351900000001', 'text': '', 'state': 'receiving'},
            {'number': '+351900000001', 'text': 'finally', 'state': 'received'},
        ],
    )
    with patch('apps.sms.services.time.sleep'), patch.dict(
        'os.environ',
        {'MMCLI_RECEIVING_MAX_WAIT_SEC': '60', 'MMCLI_EMPTY_TEXT_RETRIES': '8'},
    ):
        obj = persist_inbound_sms(path, 0, client)
    assert obj.text == 'finally'
    assert client.show_sms.call_count == 3


def test_refresh_stale_inbound_sms_rows_fills_stuck_row():
    path = '/org/freedesktop/ModemManager1/SMS/stale1'
    InboundSms.objects.create(
        mm_path=path,
        modem_index=0,
        from_number='+351961343706',
        text='',
        mm_state='receiving',
    )
    client = MMCLIClient()
    client.show_sms = MagicMock(
        return_value={
            'number': '+351961343706',
            'text': 'recovered via stale refresh',
            'state': 'received',
        },
    )
    with patch('apps.sms.services.MMCLIClient', return_value=client):
        stats = refresh_stale_inbound_sms_rows(modem_index=0)
    row = InboundSms.objects.get(mm_path=path)
    assert stats['checked'] == 1
    assert stats['text_filled'] == 1
    assert row.text == 'recovered via stale refresh'
    assert row.mm_state == 'received'


def test_post_save_requeues_webhook_when_text_patched():
    path = '/org/freedesktop/ModemManager1/SMS/mirror_patch'
    InboundSms.objects.create(
        mm_path=path,
        modem_index=0,
        from_number='+351900000001',
        text='',
        mm_state='receiving',
    )
    client = MMCLIClient()
    client.show_sms = MagicMock(
        return_value={
            'number': '+351900000001',
            'text': 'mirror me',
            'state': 'received',
        },
    )
    with patch.dict('os.environ', {'INBOUND_PROCESSOR_WORKERS': '0'}):
        import apps.sms.inbound_processor as ip

        ip._global_processor = None
        with patch(
            'apps.sms.webhook_delivery.deliver_inbound_webhooks',
            return_value=True,
        ) as mock_deliver:
            with patch('django.db.transaction.on_commit', side_effect=lambda fn: fn()):
                persist_inbound_sms(path, 0, client)
        mock_deliver.assert_called()


def test_dispatch_success_updates_state():
    outbound = OutboundSms.objects.create(
        modem_index=0,
        to_number='+351913000387',
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
        to_number='+351913000387',
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
