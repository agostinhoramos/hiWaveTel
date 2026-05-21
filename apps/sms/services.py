"""Persist ModemManager inbound SMS snapshots into Django models (and outbound send coordination)."""

from __future__ import annotations

import contextlib
import logging
import os
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
from .mmcli_client import resolve_modem_mmcli_index
from .modem_ready import prepare_modem_for_outbound_sms
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


# ModemManager sometimes signals Messaging.Added before multipart text is readable via mmcli;
# retries give the assembled body time to appear. Prefer explicit MM states when possible,
# plus a hint-based path when SMS metadata exists but texto is empty (multipart assembly gap).
_RETRY_MMCLI_SHOW_WHILE_STATES = frozenset({'receiving', 'unknown'})


def _should_retry_empty_mmcli_snapshot(raw: dict[str, str], state_norm: str) -> bool:
    """True when texto may still populate after subsequent mmcli snapshots."""
    if state_norm in _RETRY_MMCLI_SHOW_WHILE_STATES:
        return True
    if state_norm != '':
        return False
    # State unset/omitted briefly; multipart often exposes number/timestamp antes do texto completo.
    return bool(extract_from_number(raw).strip() or extract_timestamp(raw).strip())


def persist_inbound_sms(mm_path: str, modem_index: int, client: MMCLIClient | None = None) -> InboundSms:
    mm = client or MMCLIClient()

    empty_retries_raw = os.environ.get('MMCLI_EMPTY_TEXT_RETRIES', '5').strip()
    empty_backoff_raw = os.environ.get('MMCLI_EMPTY_TEXT_BACKOFF_SEC', '1.5').strip()
    try:
        max_snapshot_tries = max(1, int(empty_retries_raw) if empty_retries_raw else 5)
    except ValueError:
        max_snapshot_tries = 5
    try:
        empty_backoff = float(empty_backoff_raw) if empty_backoff_raw else 1.5
    except ValueError:
        empty_backoff = 1.5
    if empty_backoff <= 0:
        empty_backoff = 1.5

    raw: dict[str, str] = {}
    for snap_attempt in range(max_snapshot_tries):
        try:
            raw = mm.show_sms(mm_path)
        except MmcliError as exc:
            _LOGGER.warning('mmcli show failed for %s: %s', mm_path, exc)
            raw = {}
        body = (extract_text(raw) or '').strip()
        mm_state_norm = extract_state(raw).strip().lower()
        if body:
            break
        keep_trying_because_state = _should_retry_empty_mmcli_snapshot(raw, mm_state_norm)
        if not keep_trying_because_state or snap_attempt >= max_snapshot_tries - 1:
            break
        _LOGGER.info(
            'Inbound SMS snapshot empty texto (multipart/race?): mm_path=%s state=%s attempt=%s/%s; retry in %.2fs',
            mm_path,
            mm_state_norm or '(unset)',
            snap_attempt + 1,
            max_snapshot_tries,
            empty_backoff,
        )
        time.sleep(empty_backoff)

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
        modem_ix = outbound.modem_index
        if client is None:
            modem_ix = resolve_modem_mmcli_index(modem_ix, client=mm)
            if modem_ix != outbound.modem_index:
                outbound.modem_index = modem_ix
                outbound.save(update_fields=('modem_index',))
            prepare_modem_for_outbound_sms(modem_ix, mmcli_path=mm.mmcli_path)
        else:
            mm.ensure_modem_index(modem_ix)
        sms_path = mm.create_sms(modem_ix, outbound.to_number, outbound.text)
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


def _delete_oldest_modem_rows(
    model,
    *,
    modem_index: int,
    limit: int,
    batch_size: int,
    dry_run: bool,
) -> int:
    """Delete oldest rows for ``model`` at ``modem_index`` when count exceeds ``limit``."""
    qs = model.objects.filter(modem_index=modem_index)
    count = qs.count()
    excess = max(0, count - limit)
    if excess <= 0:
        return 0

    deleted = 0
    remaining = excess
    while remaining > 0:
        chunk = min(batch_size, remaining)
        ids = list(qs.order_by('created_at', 'pk').values_list('pk', flat=True)[:chunk])
        if not ids:
            break
        if dry_run:
            deleted += len(ids)
        else:
            model.objects.filter(pk__in=ids).delete()
            deleted += len(ids)
        remaining -= len(ids)
    return deleted


def rotate_modem_sms_storage(
    modem_index: int,
    *,
    limit: int,
    batch_size: int = 100,
    dry_run: bool = False,
) -> dict[str, int]:
    """Trim modem-layer SMS rows; ``limit`` is split evenly between inbound and outbound."""
    per_type_limit = max(1, limit // 2)

    inbound_before = InboundSms.objects.filter(modem_index=modem_index).count()
    outbound_before = OutboundSms.objects.filter(modem_index=modem_index).count()

    inbound_deleted = _delete_oldest_modem_rows(
        InboundSms,
        modem_index=modem_index,
        limit=per_type_limit,
        batch_size=batch_size,
        dry_run=dry_run,
    )
    outbound_deleted = _delete_oldest_modem_rows(
        OutboundSms,
        modem_index=modem_index,
        limit=per_type_limit,
        batch_size=batch_size,
        dry_run=dry_run,
    )

    if inbound_deleted or outbound_deleted:
        _LOGGER.info(
            'Modem SMS rotation modem_index=%s inbound_deleted=%s outbound_deleted=%s dry_run=%s',
            modem_index,
            inbound_deleted,
            outbound_deleted,
            dry_run,
        )

    return {
        'inbound_deleted': inbound_deleted,
        'outbound_deleted': outbound_deleted,
        'inbound_count_before': inbound_before,
        'outbound_count_before': outbound_before,
        'per_type_limit': per_type_limit,
    }


def modem_sms_storage_counts(modem_index: int) -> dict[str, int]:
    """Current modem SMS counts for admin monitoring."""
    return {
        'inbound_sms': InboundSms.objects.filter(modem_index=modem_index).count(),
        'outbound_sms': OutboundSms.objects.filter(modem_index=modem_index).count(),
    }
