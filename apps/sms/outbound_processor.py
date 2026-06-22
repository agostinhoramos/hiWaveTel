"""Thread-safe outbound SMS dispatch queue."""

from __future__ import annotations

import logging
import os
import queue
import random
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from django.conf import settings
from django.db import OperationalError, close_old_connections

_LOGGER = logging.getLogger(__name__)

_PRIORITY_RANK = {'urgent': 0, 'high': 1, 'normal': 2}


def _priority_rank(priority: str) -> int:
    return _PRIORITY_RANK.get((priority or 'normal').strip().lower(), 2)


@dataclass(order=True)
class _OutboundJob:
    rank: int
    seq: int
    job_type: str = field(compare=False)
    reference: str = field(compare=False)
    priority: str = field(compare=False, default='normal')
    payload: dict[str, Any] = field(compare=False, default_factory=dict)


class OutboundProcessorQueue:
    """Priority queue for outbound SMS jobs (single modem worker by default)."""

    def __init__(
        self,
        num_workers: int = 1,
        max_queue_size: int = 10000,
    ):
        self.queue: queue.PriorityQueue[_OutboundJob] = queue.PriorityQueue(maxsize=max_queue_size)
        self.num_workers = num_workers
        self.workers: list[threading.Thread] = []
        self.running = False
        self._stop_event = threading.Event()
        self._seq = 0
        self._seq_lock = threading.Lock()
        self._in_flight: set[str] = set()
        self._in_flight_lock = threading.Lock()
        self._processed_count = 0
        self._failed_count = 0
        self._enqueued_count = 0
        self._metrics_lock = threading.Lock()

    def start(self) -> None:
        if self.running:
            _LOGGER.warning('Outbound processor queue already running')
            return
        self.running = True
        self._stop_event.clear()
        for i in range(self.num_workers):
            worker = threading.Thread(
                target=self._worker_loop,
                name=f'OutboundProcessor-{i}',
                daemon=True,
            )
            worker.start()
            self.workers.append(worker)
        _LOGGER.info('Outbound processor queue started with %s workers', self.num_workers)

    def stop(self, timeout: float = 10.0) -> None:
        if not self.running:
            return
        self.running = False
        self._stop_event.set()
        for worker in self.workers:
            worker.join(timeout=timeout)
        self.workers.clear()

    def _next_seq(self) -> int:
        with self._seq_lock:
            self._seq += 1
            return self._seq

    def enqueue(
        self,
        job_type: str,
        reference: str,
        *,
        priority: str = 'normal',
        payload: dict[str, Any] | None = None,
    ) -> bool:
        key = f'{job_type}:{reference}'
        with self._in_flight_lock:
            if key in self._in_flight:
                return True
            self._in_flight.add(key)
        job = _OutboundJob(
            rank=_priority_rank(priority),
            seq=self._next_seq(),
            job_type=job_type,
            reference=reference,
            priority=priority,
            payload=dict(payload or {}),
        )
        try:
            self.queue.put(job, block=False)
            with self._metrics_lock:
                self._enqueued_count += 1
            return True
        except queue.Full:
            with self._in_flight_lock:
                self._in_flight.discard(key)
            _LOGGER.error('Outbound queue full; dropping %s', key)
            return False

    def get_metrics(self) -> dict[str, int]:
        with self._metrics_lock:
            return {
                'enqueued': self._enqueued_count,
                'processed': self._processed_count,
                'failed': self._failed_count,
                'queue_size': self.queue.qsize(),
            }

    def _release_in_flight(self, job_type: str, reference: str) -> None:
        with self._in_flight_lock:
            self._in_flight.discard(f'{job_type}:{reference}')

    def _worker_loop(self) -> None:
        worker_name = threading.current_thread().name
        while self.running and not self._stop_event.is_set():
            try:
                try:
                    job = self.queue.get(timeout=1.0)
                except queue.Empty:
                    continue
                try:
                    self._process_job(job, worker_name)
                finally:
                    self.queue.task_done()
                    self._release_in_flight(job.job_type, job.reference)
            except Exception:
                _LOGGER.exception('%s worker error', worker_name)
                time.sleep(0.5)

    def _process_job(self, job: _OutboundJob, worker_name: str) -> None:
        try:
            if job.job_type == 'outbound':
                self._process_outbound_pk(int(job.reference), worker_name)
            else:
                _LOGGER.warning('Unknown outbound job type=%s', job.job_type)
                with self._metrics_lock:
                    self._failed_count += 1
        except Exception:
            _LOGGER.exception('%s failed job type=%s ref=%s', worker_name, job.job_type, job.reference)
            with self._metrics_lock:
                self._failed_count += 1

    def _process_outbound_pk(self, pk: int, worker_name: str) -> None:
        from apps.sms.models import OutboundSms
        from apps.sms.services import dispatch_outbound_mmcli

        outbound = _fetch_with_sqlite_retry(lambda: OutboundSms.objects.get(pk=pk))
        if outbound.state != OutboundSms.State.CREATED:
            return
        dispatch_outbound_mmcli(outbound)
        with self._metrics_lock:
            self._processed_count += 1
        _LOGGER.info('%s completed outbound pk=%s', worker_name, pk)


def _fetch_with_sqlite_retry(fetch_fn, max_retries: int | None = None):
    from apps.sms.services import _looks_sqlite_concurrency_error

    retries = max_retries or int(getattr(settings, 'SQLITE_LOCKED_RETRY_COUNT', 15))
    backoff = float(getattr(settings, 'SQLITE_LOCKED_RETRY_BACKOFF_SEC', 0.02))
    last_exc: OperationalError | None = None
    for attempt in range(retries):
        try:
            return fetch_fn()
        except OperationalError as exc:
            last_exc = exc
            if not _looks_sqlite_concurrency_error(exc) or attempt >= retries - 1:
                raise
            close_old_connections()
            time.sleep(backoff * (2**attempt) + random.random() * 0.02)
    if last_exc is not None:
        raise last_exc
    raise RuntimeError('_fetch_with_sqlite_retry exhausted')


_global_outbound: Optional[OutboundProcessorQueue] = None
_outbound_lock = threading.Lock()


def outbound_async_enabled() -> bool:
    return bool(getattr(settings, 'OUTBOUND_ASYNC_ENABLED', False))


def queues_enabled_in_process() -> bool:
    return os.environ.get('HIWAVETEL_QUEUE_ENABLED', '').lower() == 'true'


def get_outbound_processor() -> OutboundProcessorQueue | None:
    global _global_outbound
    if _global_outbound is not None:
        return _global_outbound
    if not queues_enabled_in_process():
        return None
    with _outbound_lock:
        if _global_outbound is not None:
            return _global_outbound
        num_workers = int(os.environ.get('OUTBOUND_PROCESSOR_WORKERS', '1'))
        if num_workers <= 0:
            return None
        max_size = int(os.environ.get('OUTBOUND_PROCESSOR_MAX_SIZE', '10000'))
        _global_outbound = OutboundProcessorQueue(
            num_workers=num_workers,
            max_queue_size=max_size,
        )
        _global_outbound.start()
    return _global_outbound


def enqueue_outbound_job(
    job_type: str,
    reference: str,
    *,
    priority: str = 'normal',
    payload: dict[str, Any] | None = None,
) -> bool:
    """Enqueue locally or dispatch synchronously when no worker process is active."""
    processor = get_outbound_processor()
    if processor is not None:
        return processor.enqueue(job_type, reference, priority=priority, payload=payload)
    if job_type == 'outbound':
        from apps.sms.models import OutboundSms
        from apps.sms.services import dispatch_outbound_mmcli

        outbound = OutboundSms.objects.get(pk=int(reference))
        dispatch_outbound_mmcli(outbound)
        return True
    _LOGGER.warning('enqueue_outbound_job: no processor and unsupported job_type=%s', job_type)
    return False
