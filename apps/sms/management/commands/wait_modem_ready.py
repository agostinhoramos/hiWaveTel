"""Wait until ModemManager reports the modem ready for SMS/Messaging."""

from __future__ import annotations

from django.core.management.base import BaseCommand

from apps.sms.modem_ready import ensure_modem_ready_for_sms


class Command(BaseCommand):
    help = 'Wait for modem enabled/registered state and optional Messaging interface readiness.'

    def add_arguments(self, parser):
        parser.add_argument('--modem-index', type=int, default=0)
        parser.add_argument(
            '--require-messaging',
            action='store_true',
            default=True,
            help='Also wait until mmcli --messaging-list-sms succeeds (default: true).',
        )
        parser.add_argument(
            '--no-require-messaging',
            action='store_false',
            dest='require_messaging',
            help='Only wait for enabled/registered modem state.',
        )

    def handle(self, *args, **options):
        modem_index = int(options['modem_index'])
        require_messaging = bool(options['require_messaging'])
        ok = ensure_modem_ready_for_sms(
            modem_index,
            require_messaging=require_messaging,
        )
        if ok:
            self.stdout.write(
                self.style.SUCCESS(
                    f'Modem {modem_index} ready for SMS'
                    + (' (Messaging OK)' if require_messaging else '')
                    + '.',
                )
            )
            return
        self.stdout.write(
            self.style.WARNING(
                f'Modem {modem_index} not ready within MODEM_ENABLE_WAIT_SEC '
                f'(Messaging required={require_messaging}).',
            )
        )
