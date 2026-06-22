"""Sync ModemDevice rows from mmcli -L."""

from __future__ import annotations

from django.core.management.base import BaseCommand

from apps.sms.modem_registry import sync_detected_modems


class Command(BaseCommand):
    help = 'Detect modems via mmcli -L and upsert ModemDevice records.'

    def handle(self, *args, **options):
        devices = sync_detected_modems()
        if not devices:
            self.stdout.write(self.style.WARNING('No modems registered (mmcli -L empty or unreachable).'))
            return
        for device in devices:
            present = 'present' if device.is_present else 'absent'
            self.stdout.write(
                f'modem_index={device.modem_index} {present} enabled={device.enabled} '
                f'path={device.dbus_path or "(none)"}',
            )
        self.stdout.write(self.style.SUCCESS(f'Synced {len(devices)} modem record(s).'))
