"""Run persistent ModemManager inbound SMS ingestion over D-Bus."""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

from apps.sms.dbus_watch import run_modem_added_listener
from apps.sms.modem_registry import list_watcher_modem_indices


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
            help='ModemManager modem index (`mmcli -L` Modem/N suffix).',
        )
        parser.add_argument(
            '--all-modems',
            action='store_true',
            help='Spawn one watcher subprocess per present enabled modem.',
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
        if options.get('all_modems'):
            self._spawn_all_watchers(options)
            return
        self._run_single_watcher(int(options['modem_index']), options)

    def _spawn_all_watchers(self, options) -> None:
        indices = list_watcher_modem_indices()
        if not indices:
            self.stdout.write(self.style.WARNING('No modems to watch.'))
            return

        manage_py = Path(__file__).resolve().parents[4] / 'manage.py'
        manage_path = str(manage_py) if manage_py.is_file() else 'manage.py'

        argv_base = [
            sys.executable,
            manage_path,
            'run_sms_watcher',
            '--reconnect-after',
            str(float(options['reconnect_after'])),
        ]
        if options['skip_initial_sync']:
            argv_base.append('--skip-initial-sync')

        for modem_index in indices:
            argv = [*argv_base, '--modem-index', str(modem_index)]
            proc = subprocess.Popen(argv)  # noqa: S603
            self.stdout.write(
                self.style.NOTICE(
                    f'SMS watcher started modem_index={modem_index} pid={proc.pid}',
                ),
            )

    def _run_single_watcher(self, modem_index: int, options) -> None:
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
