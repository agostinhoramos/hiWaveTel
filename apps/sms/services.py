"""Persist ModemManager inbound SMS snapshots into Django models (and outbound send coordination)."""

from __future__ import annotations

import logging

from django.db import transaction

from .mmcli_client import (
    MMCLIClient,
    MmcliError,
    extract_from_number,
    extract_smsc,
    extract_state,
    extract_text,
    extract_timestamp,
)
from .models import InboundSms, OutboundSms

_LOGGER = logging.getLogger(__name__)


def format_public_mmcli_error(exc: MmcliError) -> str:
    """Return a short, client-safe description (no stderr dumps).

    Operators can still tail container logs where full ``mmcli`` output is emitted.
    """
    base = (str(exc).strip().split('\n')[0] or '').strip()
    suffix = getattr(exc, 'stderr', None) or ''
    if suffix:
        first_line = (suffix.strip().split('\n')[0] or '').strip()
        combined = ': '.join(p for p in (base, first_line) if p)
        return combined[:200]
    return base[:200]


def persist_inbound_sms(mm_path: str, modem_index: int, client: MMCLIClient | None = None) -> InboundSms:
    mm = client or MMCLIClient()
    try:
        raw = mm.show_sms(mm_path)
    except MmcliError as exc:
        _LOGGER.warning('mmcli show failed for %s: %s', mm_path, exc)
        raw = {}

    defaults = {
        'modem_index': modem_index,
        'from_number': extract_from_number(raw),
        'text': extract_text(raw),
        'mm_state': extract_state(raw),
        'smsc': extract_smsc(raw),
        'modem_timestamp_raw': extract_timestamp(raw),
    }

    with transaction.atomic():
        obj, created = InboundSms.objects.get_or_create(mm_path=mm_path, defaults=defaults)

    if not created:
        patched = {}
        for field_name, desired in defaults.items():
            current = getattr(obj, field_name)
            if isinstance(current, str) and not current.strip() and isinstance(desired, str) and desired.strip():
                patched[field_name] = desired
        if patched:
            for key, val in patched.items():
                setattr(obj, key, val)
            obj.save(update_fields=list(patched.keys()))

    return obj


def dispatch_outbound_mmcli(outbound: OutboundSms, *, client: MMCLIClient | None = None) -> OutboundSms:
    """Drive mmcli create/send after ``OutboundSms`` DB row creation; mutates outbound state on failure."""

    mm = client or MMCLIClient()
    try:
        mm.ensure_modem_index(outbound.modem_index)
        sms_path = mm.create_sms(outbound.modem_index, outbound.to_number, outbound.text)
        outbound.mm_path = sms_path
        outbound.state = OutboundSms.State.SENDING
        outbound.save(update_fields=('mm_path', 'state'))

        mm.send_sms(sms_path)

        outbound.state = OutboundSms.State.SENT
        outbound.save(update_fields=('state',))
        return outbound
    except MmcliError as exc:
        outbound.state = OutboundSms.State.FAILED
        outbound.error_message = format_public_mmcli_error(exc)
        outbound.save(update_fields=('state', 'error_message'))
        _LOGGER.warning('Outbound id=%s failed: %s', outbound.pk, exc)
        return outbound
