"""Backfill InboxMessage.body from InboundSms.text and drop orphan inbox rows."""

from __future__ import annotations

import re

from django.core.management.base import BaseCommand

from apps.external_device.models import InboxMessage
from apps.external_device.services import sync_single_inbound_to_all_devices
from apps.sms.models import InboundSms

_MMCLI_MESSAGE_ID_RE = re.compile(r'^mmcli_(\d+)_dev_')


class Command(BaseCommand):
    help = (
        'Re-run inbox mirroring for InboundSms rows with non-empty text so blank '
        'InboxMessage.body rows get patched. Deletes InboxMessage rows whose '
        'mmcli_<pk>_ prefix refers to a missing InboundSms.'
    )

    def handle(self, *args, **options):
        synced = 0
        for inbound in (
            InboundSms.objects.exclude(text='').exclude(text__isnull=True).order_by('pk')
        ):
            sync_single_inbound_to_all_devices(inbound)
            synced += 1

        orphan_pks: list[int] = []
        for msg in InboxMessage.objects.only('pk', 'message_id').iterator(chunk_size=500):
            m = _MMCLI_MESSAGE_ID_RE.match(msg.message_id or '')
            if not m:
                continue
            inbound_pk = int(m.group(1))
            if not InboundSms.objects.filter(pk=inbound_pk).exists():
                orphan_pks.append(msg.pk)
        orphans_removed = len(orphan_pks)
        if orphan_pks:
            InboxMessage.objects.filter(pk__in=orphan_pks).delete()

        self.stdout.write(
            self.style.SUCCESS(
                f'Backfill complete: mirrored {synced} InboundSms; '
                f'removed {orphans_removed} orphan InboxMessage(s).'
            ),
        )
