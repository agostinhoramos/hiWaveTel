"""Bootstrap HiDishelinkDevice row from detected modem identity (DB only)."""

from __future__ import annotations

from django.conf import settings
from django.core.management.base import BaseCommand

from apps.external_device.hidishelink_bootstrap import ensure_hidishelink_device_from_modem


class Command(BaseCommand):
    help = (
        'Ensure a HiDishelinkDevice (and ExternalDevice) exists for the modem phone number '
        'detected via mmcli. Does not call remote register/login/mqtt-config.'
    )

    def add_arguments(self, parser):  # type: ignore[override]
        parser.add_argument(
            '--modem-index',
            type=int,
            default=None,
            help='Modem index for mmcli (default: MODEM_MMCLI_INDEX).',
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Probe modem and print planned changes without writing to the database.',
        )

    def handle(self, *args, **options):  # type: ignore[override]
        modem_index = options['modem_index']
        if modem_index is None:
            modem_index = int(getattr(settings, 'MODEM_MMCLI_INDEX', 0))

        stats = ensure_hidishelink_device_from_modem(
            modem_index,
            dry_run=bool(options['dry_run']),
        )

        if stats.get('skipped'):
            self.stdout.write(
                self.style.WARNING(
                    f'ensure_hidishelink_device: skipped ({stats.get("reason", "unknown")}).'
                )
            )
            return

        if stats.get('dry_run'):
            self.stdout.write(
                self.style.SUCCESS(
                    f'ensure_hidishelink_device dry-run: would use device_id={stats["device_id"]!r}'
                )
            )
            self.stdout.write(stats['notes'])
            return

        if stats.get('created'):
            self.stdout.write(
                self.style.SUCCESS(
                    f'ensure_hidishelink_device: created HiDishelinkDevice {stats["device_id"]!r} '
                    f'(status={stats["status"]}).'
                )
            )
        else:
            self.stdout.write(
                f'ensure_hidishelink_device: updated HiDishelinkDevice {stats["device_id"]!r} '
                f'(status={stats["status"]}).'
            )
        if stats.get('external_created'):
            self.stdout.write(f'ExternalDevice {stats["device_id"]!r} created.')
