"""Run persistent ModemManager inbound SMS ingestion over D-Bus."""

import asyncio

from django.conf import settings
from django.core.management.base import BaseCommand

from apps.sms.dbus_watch import run_modem_added_listener


class Command(BaseCommand):
    help = (
        "Listen for Modem.Messaging.Added on the system D-Bus and persist inbound SMS to the database. "
        "Requires ModemManager/mmcli configured on this system."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--modem-index',
            type=int,
            default=settings.MODEM_MMCLI_INDEX,
            help='ModemManager modem index (`mmcli -L` Modem/N suffix, or MODEM_MMCLI_INDEX env var).',
        )
        parser.add_argument(
            '--reconnect-after',
            type=float,
            default=5.0,
            help='Seconds to wait after D-Bus disconnect before reconnecting.',
        )
        parser.add_argument(
            '--skip-initial-sync',
            action='store_true',
            help='Do not run mmcli --messaging-list-sms immediately after subscribing to Added.',
        )

    def handle(self, *args, **options):
        modem_index = int(options['modem_index'])
        reconnect = float(options['reconnect_after'])

        initial_snapshot = not bool(options['skip_initial_sync'])

        self.stdout.write(
            self.style.NOTICE(
                f"SMS watcher modem={modem_index} initial_snapshot={'on' if initial_snapshot else 'off'} ...",
            ),
        )

        try:
            asyncio.run(
                run_modem_added_listener(
                    modem_index,
                    reconnect,
                    initial_snapshot=initial_snapshot,
                ),
            )
        except KeyboardInterrupt:
            self.stdout.write(self.style.WARNING('Interrupted by the user.'))
