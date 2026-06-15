"""Global mutex serializing ModemManager/mmcli access across threads and queues."""

from __future__ import annotations

import contextlib
import threading
import time
from typing import Iterator

_mmcli_lock = threading.Lock()
_wait_ms_total = 0
_wait_count = 0
_metrics_lock = threading.Lock()


@contextlib.contextmanager
def mmcli_serial() -> Iterator[None]:
    """Acquire the process-wide mmcli lock (FIFO via threading.Lock)."""
    global _wait_ms_total, _wait_count
    t0 = time.monotonic()
    with _mmcli_lock:
        waited_ms = int((time.monotonic() - t0) * 1000)
        if waited_ms > 0:
            with _metrics_lock:
                _wait_ms_total += waited_ms
                _wait_count += 1
        yield


def get_mmcli_lock_metrics() -> dict[str, int]:
    with _metrics_lock:
        return {'mmcli_wait_count': _wait_count, 'mmcli_wait_ms_total': _wait_ms_total}
