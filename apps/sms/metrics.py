"""SMS pipeline metrics collector."""

from __future__ import annotations

import threading
from typing import Any

from django.utils import timezone


class SmsMetricsCollector:
    """Collect and expose SMS processing metrics (thread-safe counters)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.counters: dict[str, int] = {
            'dbus_signals_received': 0,
            'enqueue_success': 0,
            'enqueue_failed_queue_full': 0,
            'persist_success': 0,
            'persist_failed': 0,
            'dlq_enqueued': 0,
            'dlq_recovered': 0,
            'mmcli_show_retries': 0,
            'empty_text_persisted': 0,
            'multipart_detected': 0,
            'periodic_recovery_runs': 0,
            'inbound_whitelist_rejected': 0,
        }
        self.last_reset = timezone.now()

    def increment(self, metric: str, value: int = 1) -> None:
        with self._lock:
            if metric in self.counters:
                self.counters[metric] += value

    def get_stats(self) -> dict[str, Any]:
        from .dead_letter_queue import get_sms_dlq

        with self._lock:
            stats: dict[str, Any] = dict(self.counters)
            stats['last_reset'] = self.last_reset.isoformat()
        dlq = get_sms_dlq()
        stats['dlq_pending'] = dlq.pending_count() if dlq else 0
        return stats

    def reset_hourly_counters(self) -> None:
        with self._lock:
            for key in self.counters:
                self.counters[key] = 0
            self.last_reset = timezone.now()


_collector = SmsMetricsCollector()


def get_metrics_collector() -> SmsMetricsCollector:
    return _collector
