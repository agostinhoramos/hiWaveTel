"""Offload MQTT message handlers from the Paho callback thread."""

from __future__ import annotations

import logging
import os
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from django.conf import settings

_LOGGER = logging.getLogger(__name__)

_HANDLER_SUFFIXES: dict[str, tuple[str, int]] = {
    '/sms/inbox': ('sms_inbox', 0),
    '/sms/status': ('sms_status', 0),
    '/health/ping': ('health_ping', 2),
    '/health/pong': ('health_pong', 2),
    '/modems/snapshot': ('catalog_snapshot', 3),
    '/modems/contacts': ('catalog_contacts', 3),
}


def classify_mqtt_topic(topic: str) -> tuple[str | None, int]:
    """Return (handler_key, priority_rank) for a topic suffix."""
    if topic.endswith('/status/request') and '/modems/' in topic:
        return ('modem_status_request', 1)
    for suffix, (key, rank) in _HANDLER_SUFFIXES.items():
        if topic.endswith(suffix):
            return (key, rank)
    return (None, 3)


@dataclass(order=True)
class _MqttHandlerJob:
    rank: int
    seq: int
    handler_key: str = field(compare=False)
    topic: str = field(compare=False)
    payload: dict[str, Any] = field(compare=False)
    client_ref: Any = field(compare=False, default=None)


class MqttHandlerQueue:
    """Priority queue processing MQTT-derived work off the network thread."""

    def __init__(self, num_workers: int = 3, max_queue_size: int = 5000):
        self.queue: queue.PriorityQueue[_MqttHandlerJob] = queue.PriorityQueue(maxsize=max_queue_size)
        self.num_workers = num_workers
        self.workers: list[threading.Thread] = []
        self.running = False
        self._stop_event = threading.Event()
        self._seq = 0
        self._seq_lock = threading.Lock()
        self._processed = 0
        self._dropped = 0
        self._shed = 0
        self._metrics_lock = threading.Lock()

    def start(self) -> None:
        if self.running:
            return
        self.running = True
        self._stop_event.clear()
        for i in range(self.num_workers):
            t = threading.Thread(target=self._worker_loop, name=f'MqttHandler-{i}', daemon=True)
            t.start()
            self.workers.append(t)
        _LOGGER.info('MqttHandlerQueue started workers=%s', self.num_workers)

    def stop(self, timeout: float = 10.0) -> None:
        if not self.running:
            return
        self.running = False
        self._stop_event.set()
        for w in self.workers:
            w.join(timeout=timeout)
        self.workers.clear()

    def _next_seq(self) -> int:
        with self._seq_lock:
            self._seq += 1
            return self._seq

    def should_load_shed(self, handler_key: str) -> bool:
        if not getattr(settings, 'MQTT_LOAD_SHED_HEALTH', True):
            return False
        threshold = int(getattr(settings, 'MQTT_HANDLER_LOAD_SHED_THRESHOLD', 100))
        if self.queue.qsize() < threshold:
            return False
        return handler_key in ('health_ping', 'health_pong', 'catalog_snapshot', 'catalog_contacts')

    def enqueue(self, handler_key: str, topic: str, payload: dict[str, Any], *, client_ref: Any = None, rank: int = 2) -> bool:
        if self.should_load_shed(handler_key):
            with self._metrics_lock:
                self._shed += 1
            _LOGGER.debug('Load-shed MQTT handler=%s topic=%s', handler_key, topic)
            return True
        job = _MqttHandlerJob(
            rank=rank,
            seq=self._next_seq(),
            handler_key=handler_key,
            topic=topic,
            payload=payload,
            client_ref=client_ref,
        )
        try:
            self.queue.put(job, block=False)
            return True
        except queue.Full:
            with self._metrics_lock:
                self._dropped += 1
            _LOGGER.error('MqttHandlerQueue full; dropping topic=%s', topic)
            return False

    def get_metrics(self) -> dict[str, int]:
        with self._metrics_lock:
            return {
                'processed': self._processed,
                'dropped': self._dropped,
                'load_shed': self._shed,
                'queue_size': self.queue.qsize(),
            }

    def _worker_loop(self) -> None:
        while self.running and not self._stop_event.is_set():
            try:
                try:
                    job = self.queue.get(timeout=1.0)
                except queue.Empty:
                    continue
                try:
                    self._dispatch(job)
                    with self._metrics_lock:
                        self._processed += 1
                except Exception:
                    _LOGGER.exception('Mqtt handler failed key=%s topic=%s', job.handler_key, job.topic)
                finally:
                    self.queue.task_done()
            except Exception:
                _LOGGER.exception('MqttHandler worker error')
                time.sleep(0.5)

    def _dispatch(self, job: _MqttHandlerJob) -> None:
        from apps.external_device import mqtt_client as mqtt_mod

        client = job.client_ref
        if client is None:
            return

        key = job.handler_key
        if key == 'sms_inbox':
            client._handle_inbox_message(job.topic, job.payload)
        elif key == 'sms_status':
            client._handle_status_message(job.topic, job.payload)
        elif key == 'health_ping':
            client._handle_health_ping(job.topic, job.payload)
        elif key == 'health_pong':
            client._handle_health_pong(job.topic, job.payload)
        elif key == 'catalog_snapshot':
            from apps.external_device.services import persist_modem_catalog_from_mqtt
            persist_modem_catalog_from_mqtt('snapshot', job.payload)
        elif key == 'catalog_contacts':
            from apps.external_device.services import persist_modem_catalog_from_mqtt
            persist_modem_catalog_from_mqtt('contacts', job.payload)
        elif key == 'modem_status_request':
            client._schedule_modem_status_snapshot(job.topic)
        else:
            _LOGGER.warning('Unknown mqtt handler key=%s', key)


_global_handler: Optional[MqttHandlerQueue] = None
_handler_lock = threading.Lock()


def mqtt_handler_queue_enabled() -> bool:
    return getattr(settings, 'MQTT_HANDLER_QUEUE_ENABLED', True)


def get_mqtt_handler_queue() -> MqttHandlerQueue | None:
    global _global_handler
    if not mqtt_handler_queue_enabled():
        return None
    if os.environ.get('HIWAVETEL_QUEUE_ENABLED', '').lower() != 'true':
        return None
    if _global_handler is not None:
        return _global_handler
    with _handler_lock:
        if _global_handler is not None:
            return _global_handler
        workers = int(os.environ.get('MQTT_HANDLER_WORKERS', '3'))
        max_size = int(os.environ.get('MQTT_HANDLER_MAX_SIZE', '5000'))
        if workers <= 0:
            return None
        _global_handler = MqttHandlerQueue(num_workers=workers, max_queue_size=max_size)
        _global_handler.start()
    return _global_handler
