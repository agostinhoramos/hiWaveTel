"""Tests for SMS storage rotation (gateway + modem layers)."""

from __future__ import annotations

import io
from datetime import timedelta

import pytest
from django.core.management import call_command
from django.test import override_settings
from django.utils import timezone

from apps.external_device.models import ExternalDevice, InboxMessage, SmsRequest
from apps.external_device.services import gateway_sms_storage_status, rotate_gateway_sms_storage
from apps.sms.models import InboundSms, OutboundSms
from apps.sms.services import rotate_modem_sms_storage


@pytest.fixture
def active_device(db):
    return ExternalDevice.objects.create(
        device_id='+351913000500',
        name='Rotation Test Device',
        status=ExternalDevice.Status.ACTIVE,
    )


def _create_inbox(device: ExternalDevice, suffix: str, *, offset_minutes: int) -> InboxMessage:
    received = timezone.now() - timedelta(minutes=offset_minutes)
    return InboxMessage.objects.create(
        message_id=f'inbox_rot_{suffix}',
        device=device,
        sender='+351900000001',
        body=f'body {suffix}',
        received_at=received,
    )


def _create_request(device: ExternalDevice, suffix: str, *, offset_minutes: int) -> SmsRequest:
    created = timezone.now() - timedelta(minutes=offset_minutes)
    req = SmsRequest.objects.create(
        request_id=f'req_rot_{suffix}',
        device=device,
        recipients=['+351900000002'],
        message=f'message {suffix}',
    )
    SmsRequest.objects.filter(pk=req.pk).update(created_at=created)
    req.refresh_from_db()
    return req


def _create_inbound(modem_index: int, suffix: str, *, offset_minutes: int) -> InboundSms:
    created = timezone.now() - timedelta(minutes=offset_minutes)
    row = InboundSms.objects.create(
        mm_path=f'/org/freedesktop/ModemManager1/SMS/rot_in_{suffix}',
        modem_index=modem_index,
        from_number='+351900000003',
        text=f'inbound {suffix}',
    )
    InboundSms.objects.filter(pk=row.pk).update(created_at=created)
    row.refresh_from_db()
    return row


def _create_outbound(modem_index: int, suffix: str, *, offset_minutes: int) -> OutboundSms:
    created = timezone.now() - timedelta(minutes=offset_minutes)
    row = OutboundSms.objects.create(
        modem_index=modem_index,
        to_number='+351900000004',
        text=f'outbound {suffix}',
    )
    OutboundSms.objects.filter(pk=row.pk).update(created_at=created)
    row.refresh_from_db()
    return row


@pytest.mark.django_db
class TestRotateGatewaySmsStorage:
    @override_settings(SMS_ROTATION_BATCH_SIZE=2)
    def test_deletes_oldest_inbox_when_over_limit(self, active_device):
        for i in range(5):
            _create_inbox(active_device, str(i), offset_minutes=10 - i)

        stats = rotate_gateway_sms_storage(
            active_device,
            inbox_limit=3,
            request_limit=100,
            batch_size=2,
        )

        assert stats['inbox_deleted'] == 2
        assert InboxMessage.objects.filter(device=active_device).count() == 3
        remaining_ids = set(
            InboxMessage.objects.filter(device=active_device).values_list('message_id', flat=True)
        )
        assert 'inbox_rot_0' not in remaining_ids
        assert 'inbox_rot_1' not in remaining_ids
        assert 'inbox_rot_4' in remaining_ids

    def test_no_deletion_when_under_limit(self, active_device):
        _create_inbox(active_device, 'only', offset_minutes=1)

        stats = rotate_gateway_sms_storage(
            active_device,
            inbox_limit=10,
            request_limit=10,
        )

        assert stats['inbox_deleted'] == 0
        assert stats['requests_deleted'] == 0
        assert InboxMessage.objects.filter(device=active_device).count() == 1

    def test_deletes_oldest_requests_when_over_limit(self, active_device):
        for i in range(4):
            _create_request(active_device, str(i), offset_minutes=10 - i)

        stats = rotate_gateway_sms_storage(
            active_device,
            inbox_limit=100,
            request_limit=2,
        )

        assert stats['requests_deleted'] == 2
        assert SmsRequest.objects.filter(device=active_device).count() == 2
        remaining = set(
            SmsRequest.objects.filter(device=active_device).values_list('request_id', flat=True)
        )
        assert 'req_rot_0' not in remaining
        assert 'req_rot_1' not in remaining
        assert 'req_rot_3' in remaining

    def test_dry_run_does_not_delete(self, active_device):
        for i in range(4):
            _create_inbox(active_device, str(i), offset_minutes=i)

        stats = rotate_gateway_sms_storage(
            active_device,
            inbox_limit=2,
            request_limit=100,
            dry_run=True,
        )

        assert stats['inbox_deleted'] == 2
        assert InboxMessage.objects.filter(device=active_device).count() == 4

    def test_gateway_storage_status_counts(self, active_device):
        _create_inbox(active_device, 'a', offset_minutes=1)
        _create_request(active_device, 'a', offset_minutes=1)

        stats = gateway_sms_storage_status(active_device)
        assert stats == {'inbox_messages': 1, 'sms_requests': 1}


@pytest.mark.django_db
class TestRotateModemSmsStorage:
    def test_deletes_oldest_inbound_and_outbound(self):
        modem_index = 7
        for i in range(5):
            _create_inbound(modem_index, str(i), offset_minutes=10 - i)
        for i in range(5):
            _create_outbound(modem_index, str(i), offset_minutes=10 - i)

        stats = rotate_modem_sms_storage(modem_index, limit=4, batch_size=10)

        assert stats['inbound_deleted'] == 3
        assert stats['outbound_deleted'] == 3
        assert InboundSms.objects.filter(modem_index=modem_index).count() == 2
        assert OutboundSms.objects.filter(modem_index=modem_index).count() == 2
        assert stats['per_type_limit'] == 2

    def test_filters_by_modem_index(self):
        _create_inbound(1, 'm1', offset_minutes=1)
        _create_inbound(2, 'm2_old', offset_minutes=10)
        _create_inbound(2, 'm2_new', offset_minutes=1)

        stats = rotate_modem_sms_storage(2, limit=2, batch_size=10)

        assert stats['inbound_deleted'] == 1
        assert InboundSms.objects.filter(modem_index=1).count() == 1
        assert InboundSms.objects.filter(modem_index=2).count() == 1

    def test_no_deletion_when_under_limit(self):
        _create_inbound(0, 'only', offset_minutes=1)

        stats = rotate_modem_sms_storage(0, limit=10)

        assert stats['inbound_deleted'] == 0
        assert stats['outbound_deleted'] == 0


@pytest.mark.django_db
class TestCleanupSmsStorageCommand:
    @override_settings(
        SMS_STORAGE_ROTATION_ENABLED=True,
        SMS_MAX_MESSAGES_PER_DEVICE=2,
        SMS_MAX_MESSAGES_PER_MODEM=4,
        SMS_ROTATION_BATCH_SIZE=50,
    )
    def test_command_rotates_gateway_and_modem(self, active_device):
        for i in range(4):
            _create_inbox(active_device, str(i), offset_minutes=10 - i)
        for i in range(4):
            _create_inbound(0, str(i), offset_minutes=10 - i)

        out = io.StringIO()
        call_command('cleanup_sms_storage', stdout=out)

        assert InboxMessage.objects.filter(device=active_device).count() == 2
        assert InboundSms.objects.filter(modem_index=0).count() == 2
        assert 'Summary:' in out.getvalue()

    @override_settings(
        SMS_STORAGE_ROTATION_ENABLED=True,
        SMS_MAX_MESSAGES_PER_DEVICE=1,
        SMS_MAX_MESSAGES_PER_MODEM=100,
    )
    def test_dry_run_leaves_rows(self, active_device):
        for i in range(3):
            _create_inbox(active_device, str(i), offset_minutes=i)

        out = io.StringIO()
        call_command('cleanup_sms_storage', '--dry-run', stdout=out)

        assert InboxMessage.objects.filter(device=active_device).count() == 3
        assert '[dry-run]' in out.getvalue()

    @override_settings(
        SMS_STORAGE_ROTATION_ENABLED=True,
        SMS_MAX_MESSAGES_PER_DEVICE=1,
        SMS_MAX_MESSAGES_PER_MODEM=100,
    )
    def test_device_id_filter(self, active_device):
        other = ExternalDevice.objects.create(
            device_id='+351913000501',
            name='Other',
            status=ExternalDevice.Status.ACTIVE,
        )
        for i in range(3):
            _create_inbox(active_device, str(i), offset_minutes=i)
            _create_inbox(other, f'o{i}', offset_minutes=i)

        call_command('cleanup_sms_storage', device_id=active_device.device_id)

        assert InboxMessage.objects.filter(device=active_device).count() == 1
        assert InboxMessage.objects.filter(device=other).count() == 3

    @override_settings(
        SMS_STORAGE_ROTATION_ENABLED=True,
        SMS_MAX_MESSAGES_PER_DEVICE=100,
        SMS_MAX_MESSAGES_PER_MODEM=4,
    )
    def test_modem_index_filter(self):
        for i in range(4):
            _create_inbound(1, str(i), offset_minutes=10 - i)
            _create_inbound(2, f'x{i}', offset_minutes=10 - i)

        call_command('cleanup_sms_storage', modem_index=1)

        assert InboundSms.objects.filter(modem_index=1).count() == 2
        assert InboundSms.objects.filter(modem_index=2).count() == 4

    @override_settings(SMS_STORAGE_ROTATION_ENABLED=False)
    def test_skips_when_rotation_disabled(self, active_device):
        for i in range(3):
            _create_inbox(active_device, str(i), offset_minutes=i)

        out = io.StringIO()
        call_command('cleanup_sms_storage', stdout=out)

        assert InboxMessage.objects.filter(device=active_device).count() == 3
        assert 'disabled' in out.getvalue().lower()

    @override_settings(SMS_STORAGE_ROTATION_ENABLED=False)
    def test_force_runs_when_disabled(self, active_device):
        for i in range(3):
            _create_inbox(active_device, str(i), offset_minutes=i)

        call_command('cleanup_sms_storage', '--force')

        assert InboxMessage.objects.filter(device=active_device).count() <= 3
