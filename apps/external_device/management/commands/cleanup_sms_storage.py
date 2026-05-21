"""Rotate stored SMS rows when per-device or per-modem limits are exceeded."""

from __future__ import annotations

from django.conf import settings
from django.core.management.base import BaseCommand

from apps.external_device.models import ExternalDevice
from apps.external_device.services import rotate_gateway_sms_storage
from apps.sms.models import InboundSms, OutboundSms
from apps.sms.services import rotate_modem_sms_storage


class Command(BaseCommand):
    help = (
        'Delete oldest stored SMS rows when gateway (per ExternalDevice) or '
        'modem (per modem_index) limits are exceeded.'
    )

    def add_arguments(self, parser):  # type: ignore[override]
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Report rows that would be deleted without deleting them.',
        )
        parser.add_argument(
            '--device-id',
            dest='device_id',
            default='',
            help='Limit gateway cleanup to a single ExternalDevice device_id.',
        )
        parser.add_argument(
            '--modem-index',
            dest='modem_index',
            type=int,
            default=None,
            help='Limit modem cleanup to a single modem_index.',
        )
        parser.add_argument(
            '--force',
            action='store_true',
            help='Run even when SMS_STORAGE_ROTATION_ENABLED is false.',
        )

    def handle(self, *args, **options):  # type: ignore[override]
        dry_run = bool(options['dry_run'])
        force = bool(options['force'])
        device_id = (options.get('device_id') or '').strip()
        modem_index = options.get('modem_index')

        if not force and not getattr(settings, 'SMS_STORAGE_ROTATION_ENABLED', True):
            self.stdout.write(
                self.style.WARNING(
                    'SMS storage rotation is disabled (SMS_STORAGE_ROTATION_ENABLED=false). '
                    'Use --force to run anyway.'
                )
            )
            return

        device_limit = getattr(settings, 'SMS_MAX_MESSAGES_PER_DEVICE', 1000)
        modem_limit = getattr(settings, 'SMS_MAX_MESSAGES_PER_MODEM', 2000)
        batch_size = getattr(settings, 'SMS_ROTATION_BATCH_SIZE', 100)

        totals = {
            'inbox_deleted': 0,
            'requests_deleted': 0,
            'inbound_deleted': 0,
            'outbound_deleted': 0,
        }

        devices = ExternalDevice.objects.filter(status=ExternalDevice.Status.ACTIVE)
        if device_id:
            devices = devices.filter(device_id=device_id)
            if not devices.exists():
                self.stdout.write(self.style.ERROR(f'No active ExternalDevice with device_id={device_id!r}'))
                return

        self.stdout.write(
            f'Gateway rotation: limit={device_limit} per type, batch_size={batch_size}, '
            f'dry_run={dry_run}, devices={devices.count()}'
        )
        for device in devices.iterator():
            stats = rotate_gateway_sms_storage(
                device,
                inbox_limit=device_limit,
                request_limit=device_limit,
                batch_size=batch_size,
                dry_run=dry_run,
            )
            totals['inbox_deleted'] += stats['inbox_deleted']
            totals['requests_deleted'] += stats['requests_deleted']
            if stats['inbox_deleted'] or stats['requests_deleted']:
                prefix = '[dry-run] ' if dry_run else ''
                self.stdout.write(
                    f"  {prefix}{device.device_id}: "
                    f"inbox -{stats['inbox_deleted']} (was {stats['inbox_count_before']}), "
                    f"requests -{stats['requests_deleted']} (was {stats['requests_count_before']})"
                )

        if modem_index is not None:
            modem_indexes = [modem_index]
        else:
            inbound_indexes = set(
                InboundSms.objects.values_list('modem_index', flat=True).distinct()
            )
            outbound_indexes = set(
                OutboundSms.objects.values_list('modem_index', flat=True).distinct()
            )
            modem_indexes = sorted(inbound_indexes | outbound_indexes)

        self.stdout.write(
            f'Modem rotation: limit={modem_limit} combined ({modem_limit // 2} per type), '
            f'modem_indexes={len(modem_indexes)}, dry_run={dry_run}'
        )
        for idx in modem_indexes:
            stats = rotate_modem_sms_storage(
                idx,
                limit=modem_limit,
                batch_size=batch_size,
                dry_run=dry_run,
            )
            totals['inbound_deleted'] += stats['inbound_deleted']
            totals['outbound_deleted'] += stats['outbound_deleted']
            if stats['inbound_deleted'] or stats['outbound_deleted']:
                prefix = '[dry-run] ' if dry_run else ''
                self.stdout.write(
                    f"  {prefix}modem_index={idx}: "
                    f"inbound -{stats['inbound_deleted']} (was {stats['inbound_count_before']}), "
                    f"outbound -{stats['outbound_deleted']} (was {stats['outbound_count_before']})"
                )

        summary = (
            f"Summary: inbox_deleted={totals['inbox_deleted']}, "
            f"requests_deleted={totals['requests_deleted']}, "
            f"inbound_deleted={totals['inbound_deleted']}, "
            f"outbound_deleted={totals['outbound_deleted']}"
        )
        if dry_run:
            self.stdout.write(self.style.WARNING(f'[dry-run] {summary}'))
        else:
            self.stdout.write(self.style.SUCCESS(summary))
