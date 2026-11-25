"""Dedicated worker to detect modem failures and escalate recovery."""

from __future__ import annotations

import signal

from django.core.management.base import BaseCommand

from apps.sms.modem_watchdog import ModemWatchdogRunner


class Command(BaseCommand):
    help = (
        'Probe modem/SMS health periodically and escalate recovery: '
        'soft recovery → ModemManager restart → container restart.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--interval',
            type=float,
            default=None,
            help='Probe interval seconds (default: MODEM_WATCHDOG_INTERVAL_SEC).',
        )
        parser.add_argument(
            '--once',
            action='store_true',
            help='Run a single probe/escalation cycle and exit.',
        )

    def handle(self, *args, **options):
        runner = ModemWatchdogRunner()

        def _stop(*_args) -> None:
            runner.stop()
            self.stdout.write(self.style.WARNING('Modem watchdog stopping...'))

        signal.signal(signal.SIGTERM, _stop)
        signal.signal(signal.SIGINT, _stop)

        if options.get('once'):
            report = runner.run_once()
            payload = report.to_dict()
            if report.ok:
                self.stdout.write(self.style.SUCCESS(f'Modem health OK: {payload}'))
            else:
                self.stdout.write(self.style.WARNING(f'Modem health FAILED: {payload}'))
            return

        runner.run_forever()
