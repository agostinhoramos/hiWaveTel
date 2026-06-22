"""SMS storage rotation via cleanup_sms_storage management command."""

from __future__ import annotations

import pytest
from django.core.management import call_command
from django.test import override_settings

from apps.sms.models import InboundSms, OutboundSms

pytestmark = pytest.mark.django_db


def _seed_inbound(modem_index: int, count: int) -> None:
    InboundSms.objects.bulk_create(
        [
            InboundSms(
                mm_path=f'/org/freedesktop/ModemManager1/SMS/in/{modem_index}/{i}',
                modem_index=modem_index,
                from_number='+351913000387',
                text=f'in-{i}',
            )
            for i in range(count)
        ]
    )


def _seed_outbound(modem_index: int, count: int) -> None:
    OutboundSms.objects.bulk_create(
        [
            OutboundSms(
                modem_index=modem_index,
                to_number='+351913000387',
                text=f'out-{i}',
            )
            for i in range(count)
        ]
    )


@override_settings(
    SMS_STORAGE_ROTATION_ENABLED=True,
    SMS_MAX_MESSAGES_PER_MODEM=4,
    SMS_ROTATION_BATCH_SIZE=10,
)
def test_cleanup_sms_storage_trims_oldest():
    _seed_inbound(0, 5)
    _seed_outbound(0, 5)
    call_command('cleanup_sms_storage')
    assert InboundSms.objects.filter(modem_index=0).count() == 2
    assert OutboundSms.objects.filter(modem_index=0).count() == 2


@override_settings(
    SMS_STORAGE_ROTATION_ENABLED=True,
    SMS_MAX_MESSAGES_PER_MODEM=4,
    SMS_ROTATION_BATCH_SIZE=10,
)
def test_cleanup_sms_storage_dry_run():
    _seed_inbound(0, 5)
    call_command('cleanup_sms_storage', '--dry-run')
    assert InboundSms.objects.filter(modem_index=0).count() == 5


@override_settings(SMS_STORAGE_ROTATION_ENABLED=False)
def test_cleanup_sms_storage_respects_disabled_flag():
    _seed_inbound(0, 5)
    call_command('cleanup_sms_storage')
    assert InboundSms.objects.filter(modem_index=0).count() == 5


@override_settings(
    SMS_STORAGE_ROTATION_ENABLED=False,
    SMS_MAX_MESSAGES_PER_MODEM=4,
)
def test_cleanup_sms_storage_force_overrides_disabled():
    _seed_inbound(1, 5)
    call_command('cleanup_sms_storage', '--modem-index', '1', '--force')
    assert InboundSms.objects.filter(modem_index=1).count() == 2
