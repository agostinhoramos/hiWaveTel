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
_STALE_MM_STATES = frozenset({'receiving', 'unknown', ''})
_TERMINAL_MM_STATES = frozenset({'received', 'stored', 'sent', 'delivered'})


def _normalize_mm_state(state: str) -> str:
    return (state or '').strip().lower()


def _inbound_field_should_update(field_name: str, current: str, desired: str) -> bool:
    """Return True when an existing InboundSms row should be patched from a fresh mmcli snapshot."""
    cur = (current or '').strip()
    des = (desired or '').strip()
    if field_name == 'text':
        if not des:
            return False
        return not cur or len(des) > len(cur)
    if field_name == 'mm_state':
        if not des:
            return False
        cur_norm = _normalize_mm_state(cur)
        des_norm = _normalize_mm_state(des)
        if cur_norm in _STALE_MM_STATES and des_norm not in _STALE_MM_STATES:
            return True
        if not cur_norm and des_norm:
            return True
        return False
    if field_name in ('from_number', 'smsc', 'modem_timestamp_raw'):
        return not cur and bool(des)
    return False


def _should_retry_empty_mmcli_snapshot(raw: dict[str, str], state_norm: str) -> bool:
    """True when texto may still populate after subsequent mmcli snapshots."""
    sender = extract_from_number(raw).strip()
    timestamp = extract_timestamp(raw).strip()
    if (sender or timestamp) and state_norm not in {'failed', 'unknown'}:
        return True
    if state_norm in _RETRY_MMCLI_SHOW_WHILE_STATES:
        return True
    return False


def _fetch_mmcli_snapshot_with_retries(
    mm: MMCLIClient,
    mm_path: str,
    *,
    metrics,
) -> dict[str, str]:
    """Poll mmcli until texto appears, timeout elapses, or state is terminal without texto."""
    empty_retries_raw = os.environ.get('MMCLI_EMPTY_TEXT_RETRIES', '8').strip()
    empty_backoff_raw = os.environ.get('MMCLI_EMPTY_TEXT_BACKOFF_SEC', '1.5').strip()
    receiving_max_wait_raw = os.environ.get(
        'MMCLI_RECEIVING_MAX_WAIT_SEC',
        str(getattr(settings, 'MMCLI_RECEIVING_MAX_WAIT_SEC', 60)),
    ).strip()

    try:
        max_snapshot_tries = max(1, int(empty_retries_raw) if empty_retries_raw else 8)
    except ValueError:
        max_snapshot_tries = 8
    try:
        empty_backoff = float(empty_backoff_raw) if empty_backoff_raw else 1.5
    except ValueError:
        empty_backoff = 1.5
    if empty_backoff <= 0:
        empty_backoff = 1.5
    try:
        receiving_max_wait_sec = float(receiving_max_wait_raw) if receiving_max_wait_raw else 60.0
    except ValueError:
        receiving_max_wait_sec = 60.0
    if receiving_max_wait_sec <= 0:
        receiving_max_wait_sec = 60.0

    raw: dict[str, str] = {}
    poll_started = time.monotonic()
    snap_attempt = 0
    while True:
        try:
            raw = mm.show_sms(mm_path)
        except MmcliError as exc:
            _LOGGER.warning('mmcli show failed for %s: %s', mm_path, exc)
            raw = {}

        body = (extract_text(raw) or '').strip()
        mm_state_norm = _normalize_mm_state(extract_state(raw))
        if body:
            break

        elapsed = time.monotonic() - poll_started
        keep_trying_because_state = _should_retry_empty_mmcli_snapshot(raw, mm_state_norm)
        receiving_still_assembling = mm_state_norm == 'receiving' and elapsed < receiving_max_wait_sec

        if receiving_still_assembling or (
            keep_trying_because_state and snap_attempt < max_snapshot_tries - 1
        ):
            metrics.increment('mmcli_show_retries')
            _LOGGER.info(
                'Inbound SMS snapshot empty texto (multipart/race?): mm_path=%s state=%s '
                'attempt=%s elapsed=%.1fs max_wait=%.1fs; retry in %.2fs',
                mm_path,
                mm_state_norm or '(unset)',
                snap_attempt + 1,
                elapsed,
                receiving_max_wait_sec,
                empty_backoff,
            )
            time.sleep(empty_backoff)
            snap_attempt += 1
            continue

        if mm_state_norm in _TERMINAL_MM_STATES:
            break
        if not keep_trying_because_state:
            break
        if snap_attempt >= max_snapshot_tries - 1 and elapsed >= receiving_max_wait_sec:
            break
        if snap_attempt >= max_snapshot_tries - 1:
            break

        metrics.increment('mmcli_show_retries')
        time.sleep(empty_backoff)
        snap_attempt += 1

    return raw


def persist_inbound_sms(mm_path: str, modem_index: int, client: MMCLIClient | None = None) -> InboundSms:
    from .dead_letter_queue import get_sms_dlq
    from .metrics import get_metrics_collector
    from .models import InboundSms

    mm = client or MMCLIClient()
    metrics = get_metrics_collector()
    debug = getattr(settings, 'SMS_DEBUG_LOGGING', False)
    log_ctx = {'mm_path': mm_path, 'modem_index': modem_index}
    if debug:
        _LOGGER.info('persist_inbound_sms start mm_path=%s modem_index=%s', mm_path, modem_index)

    raw = _fetch_mmcli_snapshot_with_retries(mm, mm_path, metrics=metrics)

    sms_class = (raw.get('smspropertiesclass') or raw.get('class') or '').lower()
    if 'multipart' in sms_class:
        metrics.increment('multipart_detected')
        _LOGGER.warning(
            'Multipart SMS detected at %s - check for other segments. '
            'Application does not reassemble multipart automatically.',
            mm_path,
        )

    if debug:
        body_preview_len = len((extract_text(raw) or '').strip())
        _LOGGER.debug(
            'mmcli snapshot complete mm_path=%s body_length=%s has_sender=%s',
            mm_path,
            body_preview_len,
            bool(extract_from_number(raw).strip()),
            extra={**log_ctx, 'body_length': body_preview_len},
        )

    defaults = {
        'modem_index': modem_index,
        'from_number': extract_from_number(raw),
        'text': extract_text(raw),
        'mm_state': extract_state(raw),
        'smsc': extract_smsc(raw),
        'modem_timestamp_raw': extract_timestamp(raw),
    }

    # Inbound whitelist check
    whitelist = getattr(settings, 'SMS_INBOUND_WHITELIST', [])
    if whitelist:
        from_num = defaults['from_number']
        if from_num not in whitelist:
            metrics.increment('inbound_whitelist_rejected')
            _LOGGER.warning(
                'SMS rejected by inbound whitelist: from=%s mm_path=%s (allowed: %s)',
                from_num,
                mm_path,
                ', '.join(whitelist),
                extra={**log_ctx, 'from_number': from_num, 'whitelist': whitelist},
            )
            # Create a dummy object that won't be saved but satisfies caller expectations
            dummy = InboundSms(
                mm_path=mm_path,
                modem_index=modem_index,
                from_number=from_num,
                text='[REJECTED BY WHITELIST]',
                mm_state='rejected',
            )
            return dummy

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
            if field_name == 'modem_index':
                continue
            current = getattr(obj, field_name)
            if not isinstance(current, str) or not isinstance(desired, str):
                continue
            if _inbound_field_should_update(field_name, current, desired):
                patched[field_name] = desired
        if patched:
            _LOGGER.info(
                'InboundSms pk=%s refreshed fields from modem: %s',
                obj.pk,
                list(patched.keys()),
            )
            for key, val in patched.items():
                setattr(obj, key, val)
            obj.save(update_fields=list(patched.keys()))

    metrics.increment('persist_success')
    if not (obj.text or '').strip():
        metrics.increment('empty_text_persisted')

    dlq = get_sms_dlq()
    if dlq is not None:
        dlq.remove_by_path(mm_path)

    return obj


def refresh_stale_inbound_sms_rows(modem_index: int | None = None) -> dict[str, int]:
    """Re-fetch mmcli snapshots for rows stuck with empty text or non-terminal mm_state."""
    from django.db.models import Q

    qs = InboundSms.objects.filter(
        Q(text='') | Q(text__isnull=True) | Q(mm_state__iexact='receiving') | Q(mm_state__iexact='unknown') | Q(mm_state='')
    )
    if modem_index is not None:
        qs = qs.filter(modem_index=modem_index)

    stats = {'checked': 0, 'text_filled': 0, 'state_updated': 0, 'still_stale': 0}
    for row in qs.order_by('pk').iterator(chunk_size=100):
        stats['checked'] += 1
        before_text = (row.text or '').strip()
        before_state = _normalize_mm_state(row.mm_state)
        try:
            updated = persist_inbound_sms(row.mm_path, row.modem_index, None)
        except Exception as exc:
            _LOGGER.warning(
                'refresh_stale_inbound_sms_rows failed pk=%s path=%s: %s',
                row.pk,
                row.mm_path,
                exc,
            )
            stats['still_stale'] += 1
            continue

        after_text = (updated.text or '').strip()
        after_state = _normalize_mm_state(updated.mm_state)
        if not before_text and after_text:
            stats['text_filled'] += 1
        if before_state in _STALE_MM_STATES and after_state not in _STALE_MM_STATES:
            stats['state_updated'] += 1
        if not after_text or after_state in _STALE_MM_STATES:
            stats['still_stale'] += 1

    if stats['checked']:
        _LOGGER.info('refresh_stale_inbound_sms_rows stats=%s modem_index=%s', stats, modem_index)
    return stats


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
