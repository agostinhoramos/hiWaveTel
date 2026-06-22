"""Rotate old InboundSms/OutboundSms rows per modem_index."""

from __future__ import annotations

from django.conf import settings
from django.core.management.base import BaseCommand

from apps.sms.models import InboundSms, OutboundSms


def _per_type_limit() -> int:
    modem_limit = int(getattr(settings, 'SMS_MAX_MESSAGES_PER_MODEM', 2000))
    return max(1, modem_limit // 2)


def _rotate_model(model, *, modem_index: int | None, batch_size: int, dry_run: bool) -> int:
    qs = model.objects.all()
    if modem_index is not None:
        qs = qs.filter(modem_index=modem_index)
    deleted = 0
    limit = _per_type_limit()
    for idx in qs.values_list('modem_index', flat=True).distinct():
        rows = model.objects.filter(modem_index=idx).order_by('-created_at')
        overflow = rows.count() - limit
        if overflow <= 0:
            continue
        pks = list(rows.values_list('pk', flat=True)[limit : limit + batch_size])
        if not pks:
            continue
        if dry_run:
            deleted += len(pks)
        else:
            deleted += model.objects.filter(pk__in=pks).delete()[0]
    return deleted


class Command(BaseCommand):
    help = 'Delete oldest SMS rows exceeding SMS_MAX_MESSAGES_PER_MODEM per modem_index.'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true')
        parser.add_argument('--force', action='store_true', help='Run even when rotation is disabled.')
        parser.add_argument('--modem-index', type=int, dest='modem_index')

    def handle(self, *args, **options):
        if not getattr(settings, 'SMS_STORAGE_ROTATION_ENABLED', True) and not options['force']:
            self.stdout.write('SMS storage rotation disabled (SMS_STORAGE_ROTATION_ENABLED=false).')
            return

        batch_size = int(getattr(settings, 'SMS_ROTATION_BATCH_SIZE', 100))
        modem_index = options.get('modem_index')
        dry_run = options['dry_run']

        inbound_deleted = _rotate_model(
            InboundSms,
            modem_index=modem_index,
            batch_size=batch_size,
            dry_run=dry_run,
        )
        outbound_deleted = _rotate_model(
            OutboundSms,
            modem_index=modem_index,
            batch_size=batch_size,
            dry_run=dry_run,
        )
        prefix = 'Would delete' if dry_run else 'Deleted'
        self.stdout.write(
            f'{prefix} inbound={inbound_deleted} outbound={outbound_deleted} '
            f'(limit per type per modem={_per_type_limit()}).'
        )
