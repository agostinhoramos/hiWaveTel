"""Schedule Docker container recycle via SIGTERM to PID 1 (restart: unless-stopped)."""

from __future__ import annotations

import logging
import os
import signal
import threading
import time

from django.conf import settings

_LOGGER = logging.getLogger(__name__)


def container_restart_allowed() -> bool:
    return bool(getattr(settings, 'HIWAVE_ALLOW_CONTAINER_RESTART_API', True))


def schedule_container_restart(
    *,
    requested_by: str = '',
    delay_sec: float | None = None,
) -> float:
    """Send SIGTERM to PID 1 after a short delay so the HTTP response can flush."""
    if delay_sec is None:
        delay_sec = float(getattr(settings, 'HIWAVE_CONTAINER_RESTART_DELAY_SEC', 1.0))
    delay_sec = max(0.5, float(delay_sec))

    def _worker() -> None:
        time.sleep(delay_sec)
        _LOGGER.warning(
            'Container restart triggered requested_by=%s delay_sec=%s',
            requested_by or 'unknown',
            delay_sec,
        )
        os.kill(1, signal.SIGTERM)

    threading.Thread(
        target=_worker,
        daemon=True,
        name='hiwavetel-container-restart',
    ).start()
    return delay_sec
