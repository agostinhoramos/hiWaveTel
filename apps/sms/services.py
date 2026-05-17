"""Persist ModemManager inbound SMS snapshots into Django models (and outbound send coordination)."""

from __future__ import annotations

import contextlib
import logging
import random
import threading
import time

from django.conf import settings
from django.db import close_old_connections, transaction
from django.db.utils import OperationalError

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

# Same-process SQLite writers (e.g. several ``SmsProcessingQueue`` threads) contend with Django ORM;
# serialize here so inbound SMS rows are not raced with ``OperationalError: database is locked``.
_sqlite_inbound_persist_lock = threading.Lock()


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


def _looks_sqlite_concurrency_error(exc: BaseException) -> bool:
    lowered = str(exc).lower()
    return 'locked' in lowered or 'busy' in lowered


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

    from django.db import connection

    lock_ctx = _sqlite_inbound_persist_lock if connection.vendor == 'sqlite' else contextlib.nullcontext()
    retries = int(getattr(settings, 'SQLITE_LOCKED_RETRY_COUNT', 15))
    backoff_sec = float(getattr(settings, 'SQLITE_LOCKED_RETRY_BACKOFF_SEC', 0.02))

    obj: InboundSms | None = None
    created = False
    last_lock_exc: OperationalError | None = None

    with lock_ctx:
        for attempt in range(retries):
            try:
                with transaction.atomic():
                    obj, created = InboundSms.objects.get_or_create(mm_path=mm_path, defaults=defaults)
                break
            except OperationalError as exc:
                last_lock_exc = exc
                if not _looks_sqlite_concurrency_error(exc) or attempt >= retries - 1:
                    raise
                delay = backoff_sec * (2**attempt) + random.random() * 0.02
                _LOGGER.warning(
                    'SQLite busy persisting inbound (attempt %s/%s, mm_path=%s): %s; retry %.3fs',
                    attempt + 1,
                    retries,
                    mm_path,
                    exc,
                    delay,
                )
                close_old_connections()
                time.sleep(delay)
        else:
            if last_lock_exc is not None:
                raise last_lock_exc
            raise RuntimeError('persist_inbound_sms exhausted retries without OperationalError')

    assert obj is not None

    if created:
        text_preview = defaults['text'][:50] + '...' if len(defaults['text']) > 50 else defaults['text']
        _LOGGER.info(
            'SMS recebida: from=%s modem_index=%s texto=%s mm_path=%s',
            defaults['from_number'] or '(unknown)',
            modem_index,
            text_preview,
            mm_path,
        )
        if not (defaults['text'] or '').strip():
            _LOGGER.debug(
                'InboundSms created with empty texto mm_path=%s mm_state=%s raw_keys_sample=%s',
                mm_path,
                defaults['mm_state'] or '(unset)',
                sorted(raw.keys())[:12],
            )

    if not created:
        patched = {}
        for field_name, desired in defaults.items():
            current = getattr(obj, field_name)
            if isinstance(current, str) and not current.strip() and isinstance(desired, str) and desired.strip():
                patched[field_name] = desired
        if patched:
            _LOGGER.debug('InboundSms pk=%s updated fields: %s', obj.pk, list(patched.keys()))
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
