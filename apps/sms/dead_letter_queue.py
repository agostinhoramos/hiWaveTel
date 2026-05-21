"""Persistent dead-letter queue for inbound SMS persist failures."""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from django.conf import settings

_LOGGER = logging.getLogger(__name__)


class SmsDeadLetterQueue:
    """SQLite-backed queue for SMS paths that failed persist attempts."""

    def __init__(
        self,
        db_path: str,
        max_size: int,
        retry_interval_sec: int,
        max_retries: int,
    ) -> None:
        self.db_path = db_path
        self.max_size = max_size
        self.retry_interval_sec = retry_interval_sec
        self.max_retries = max_retries
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._worker: threading.Thread | None = None
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                '''
                CREATE TABLE IF NOT EXISTS sms_dlq (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sms_path TEXT NOT NULL UNIQUE,
                    modem_index INTEGER NOT NULL,
                    failed_at TEXT NOT NULL,
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT '',
                    next_retry_at TEXT
                )
                '''
            )
            conn.execute(
                'CREATE INDEX IF NOT EXISTS idx_sms_dlq_next_retry ON sms_dlq(next_retry_at)'
            )
            conn.commit()

    def enqueue(self, sms_path: str, modem_index: int, error: str) -> bool:
        with self._lock:
            with self._connect() as conn:
                count = conn.execute('SELECT COUNT(*) FROM sms_dlq').fetchone()[0]
                if count >= self.max_size:
                    _LOGGER.error(
                        'SMS DLQ full (%s); cannot enqueue path=%s',
                        self.max_size,
                        sms_path,
                    )
                    return False
                now = datetime.now(timezone.utc).isoformat()
                conn.execute(
                    '''
                    INSERT INTO sms_dlq (sms_path, modem_index, failed_at, retry_count, last_error, next_retry_at)
                    VALUES (?, ?, ?, 0, ?, ?)
                    ON CONFLICT(sms_path) DO UPDATE SET
                        last_error=excluded.last_error,
                        failed_at=excluded.failed_at,
                        next_retry_at=excluded.next_retry_at
                    ''',
                    (sms_path, modem_index, now, error[:2000], now),
                )
                conn.commit()
                return True

    def dequeue_batch(self, limit: int) -> list[tuple[int, str, int]]:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            with self._connect() as conn:
                rows = conn.execute(
                    '''
                    SELECT id, sms_path, modem_index FROM sms_dlq
                    WHERE retry_count < ? AND (next_retry_at IS NULL OR next_retry_at <= ?)
                    ORDER BY failed_at ASC
                    LIMIT ?
                    ''',
                    (self.max_retries, now, limit),
                ).fetchall()
                return [(int(r['id']), str(r['sms_path']), int(r['modem_index'])) for r in rows]

    def mark_recovered(self, row_id: int) -> None:
        with self._lock:
            with self._connect() as conn:
                conn.execute('DELETE FROM sms_dlq WHERE id = ?', (row_id,))
                conn.commit()

    def remove_by_path(self, sms_path: str) -> None:
        with self._lock:
            with self._connect() as conn:
                conn.execute('DELETE FROM sms_dlq WHERE sms_path = ?', (sms_path,))
                conn.commit()

    def mark_retry_failed(self, row_id: int, error: str) -> None:
        with self._lock:
            with self._connect() as conn:
                row = conn.execute(
                    'SELECT retry_count FROM sms_dlq WHERE id = ?',
                    (row_id,),
                ).fetchone()
                if row is None:
                    return
                new_count = int(row['retry_count']) + 1
                delay = min(3600.0, self.retry_interval_sec * (2 ** min(new_count, 6)))
                next_at = datetime.fromtimestamp(
                    time.time() + delay,
                    tz=timezone.utc,
                ).isoformat()
                conn.execute(
                    '''
                    UPDATE sms_dlq
                    SET retry_count=?, last_error=?, next_retry_at=?
                    WHERE id=?
                    ''',
                    (new_count, error[:2000], next_at, row_id),
                )
                conn.commit()

    def pending_count(self) -> int:
        with self._connect() as conn:
            return int(conn.execute('SELECT COUNT(*) FROM sms_dlq').fetchone()[0])

    def process_batch(self, limit: int = 50) -> dict[str, int]:
        from .metrics import get_metrics_collector
        from .services import persist_inbound_sms

        stats = {'processed': 0, 'recovered': 0, 'failed': 0}
        metrics = get_metrics_collector()
        for row_id, sms_path, modem_index in self.dequeue_batch(limit):
            stats['processed'] += 1
            try:
                persist_inbound_sms(sms_path, modem_index, None)
                self.mark_recovered(row_id)
                stats['recovered'] += 1
                metrics.increment('dlq_recovered')
            except Exception as exc:
                self.mark_retry_failed(row_id, str(exc))
                stats['failed'] += 1
                _LOGGER.warning(
                    'DLQ retry failed id=%s path=%s: %s',
                    row_id,
                    sms_path,
                    exc,
                )
        return stats

    def _recovery_worker_loop(self) -> None:
        _LOGGER.info(
            'SMS DLQ recovery worker started (interval=%ss max_retries=%s)',
            self.retry_interval_sec,
            self.max_retries,
        )
        while not self._stop_event.wait(timeout=self.retry_interval_sec):
            try:
                stats = self.process_batch()
                if stats['processed']:
                    _LOGGER.info('SMS DLQ recovery batch: %s', stats)
            except Exception as exc:
                _LOGGER.exception('SMS DLQ recovery worker error: %s', exc)
        _LOGGER.info('SMS DLQ recovery worker stopped')

    def start_recovery_worker(self) -> None:
        if self._worker and self._worker.is_alive():
            return
        self._stop_event.clear()
        self._worker = threading.Thread(
            target=self._recovery_worker_loop,
            name='SmsDlqRecovery',
            daemon=True,
        )
        self._worker.start()

    def stop_recovery_worker(self, timeout: float = 5.0) -> None:
        self._stop_event.set()
        if self._worker:
            self._worker.join(timeout=timeout)


_global_dlq: SmsDeadLetterQueue | None = None
_dlq_lock = threading.Lock()


def get_sms_dlq() -> SmsDeadLetterQueue | None:
    global _global_dlq
    if not getattr(settings, 'SMS_DLQ_ENABLED', True):
        return None
    if _global_dlq is None:
        with _dlq_lock:
            if _global_dlq is None:
                _global_dlq = SmsDeadLetterQueue(
                    db_path=getattr(settings, 'SMS_DLQ_DB_PATH', 'dlq_sms.db'),
                    max_size=getattr(settings, 'SMS_DLQ_MAX_SIZE', 1000),
                    retry_interval_sec=getattr(settings, 'SMS_DLQ_RETRY_INTERVAL_SEC', 60),
                    max_retries=getattr(settings, 'SMS_DLQ_MAX_RETRIES', 10),
                )
                _global_dlq.start_recovery_worker()
    return _global_dlq


def enqueue_persist_failure(sms_path: str, modem_index: int, error: str) -> None:
    """Record persist failure in metrics and DLQ."""
    from .metrics import get_metrics_collector

    metrics = get_metrics_collector()
    metrics.increment('persist_failed')
    dlq = get_sms_dlq()
    if dlq and dlq.enqueue(sms_path, modem_index, error):
        metrics.increment('dlq_enqueued')
        _LOGGER.info('SMS enqueued to DLQ: path=%s modem_index=%s', sms_path, modem_index)


def get_dlq_stats() -> dict[str, Any]:
    dlq = get_sms_dlq()
    if dlq is None:
        return {'enabled': False, 'pending': 0}
    return {'enabled': True, 'pending': dlq.pending_count()}
