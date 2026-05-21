"""Manually recover missing inbound SMS from modem storage and DLQ."""

from __future__ import annotations

from django.conf import settings
from django.core.management.base import BaseCommand

from apps.sms.dead_letter_queue import get_sms_dlq
from apps.sms.dbus_watch import sync_modem_sms_snapshot
from apps.sms.metrics import get_metrics_collector
from apps.sms.models import InboundSms
from apps.sms.services import refresh_stale_inbound_sms_rows


class Command(BaseCommand):
    help = 'Recover missing inbound SMS via modem snapshot sync and/or DLQ processing.'

    def add_arguments(self, parser):  # type: ignore[override]
        parser.add_argument(
            '--modem-index',
            type=int,
            default=None,
            help='Modem index for mmcli snapshot (default: MODEM_MMCLI_INDEX).',
        )
        parser.add_argument(
            '--force-recheck',
            action='store_true',
            help='Re-fetch all modem SMS paths (persist_inbound_sms patches empty fields).',
        )
        parser.add_argument(
            '--check-dlq',
            action='store_true',
            help='Process DLQ immediately instead of waiting for background worker.',
        )
        parser.add_argument(
            '--refresh-stale',
            action='store_true',
            help='Re-fetch mmcli for InboundSms rows stuck in receiving/unknown or with empty text.',
        )

    def handle(self, *args, **options):  # type: ignore[override]
        modem_index = options['modem_index']
        if modem_index is None:
            modem_index = int(getattr(settings, 'MODEM_MMCLI_INDEX', 0))

        force = bool(options['force_recheck'])
        check_dlq = bool(options['check_dlq'])
        refresh_stale = bool(options['refresh_stale'])

        before_count = InboundSms.objects.filter(modem_index=modem_index).count()
        self.stdout.write(
            f'InboundSms before recovery modem_index={modem_index}: {before_count}'
        )

        if force or (not check_dlq and not refresh_stale):
            synced = sync_modem_sms_snapshot(modem_index)
            after_count = InboundSms.objects.filter(modem_index=modem_index).count()
            self.stdout.write(
                self.style.SUCCESS(
                    f'Modem snapshot synced {synced} path(s); '
                    f'InboundSms count {before_count} -> {after_count}'
                )
            )

        if refresh_stale:
            stats = refresh_stale_inbound_sms_rows(modem_index)
            self.stdout.write(
                self.style.SUCCESS(f'Stale InboundSms refresh: {stats}')
            )

        dlq = get_sms_dlq()
        if check_dlq:
            if dlq is None:
                self.stdout.write(self.style.WARNING('SMS DLQ is disabled (SMS_DLQ_ENABLED=false).'))
            else:
                pending = dlq.pending_count()
                stats = dlq.process_batch(limit=500)
                self.stdout.write(
                    self.style.SUCCESS(
                        f'DLQ processed pending={pending} stats={stats}'
                    )
                )
        elif dlq is not None:
            self.stdout.write(f'DLQ pending items: {dlq.pending_count()}')

        metrics = get_metrics_collector().get_stats()
        self.stdout.write(f'SMS metrics snapshot: {metrics}')
