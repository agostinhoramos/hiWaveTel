"""Thread-safe inbound SMS post-save processing queue with retry logic."""

import logging
import os
import queue
import random
import threading
import time
from typing import Optional

from django.conf import settings
from django.db import OperationalError, close_old_connections

_LOGGER = logging.getLogger(__name__)


def _fetch_inbound_sms(pk: int):
    """Load ``InboundSms`` with SQLite busy retries (worker runs outside request txn)."""
    from apps.sms.models import InboundSms
    from apps.sms.services import _looks_sqlite_concurrency_error

    retries = int(getattr(settings, 'SQLITE_LOCKED_RETRY_COUNT', 15))
    backoff_sec = float(getattr(settings, 'SQLITE_LOCKED_RETRY_BACKOFF_SEC', 0.02))

    last_exc: OperationalError | None = None
    for attempt in range(retries):
        try:
            return InboundSms.objects.get(pk=pk)
        except InboundSms.DoesNotExist:
            raise
        except OperationalError as exc:
            last_exc = exc
            if not _looks_sqlite_concurrency_error(exc) or attempt >= retries - 1:
                raise
            delay = backoff_sec * (2**attempt) + random.random() * 0.02
            _LOGGER.warning(
                'SQLite busy loading inbound pk=%s (attempt %s/%s); retry %.3fs',
                pk,
                attempt + 1,
                retries,
                delay,
            )
            close_old_connections()
            time.sleep(delay)

    if last_exc is not None:
        raise last_exc
    raise RuntimeError('_fetch_inbound_sms exhausted retries')


class InboundProcessorQueue:
    """Thread-safe queue for processing InboundSms post-save operations asynchronously.
    
    Processes mirror to devices and MQTT publishing with exponential backoff retry for failures.
    """
    
    def __init__(
        self,
        num_workers: int = 2,
        max_queue_size: int = 500,
        retry_max: int = 5,
        retry_base_sec: float = 1.0,
    ):
        """
        Initialize inbound SMS processing queue.
        
        Args:
            num_workers: Number of worker threads to spawn
            max_queue_size: Maximum number of InboundSms IDs in queue before blocking
            retry_max: Maximum number of retry attempts for MQTT publish failures
            retry_base_sec: Base delay for exponential backoff (seconds)
        """
        self.queue = queue.Queue(maxsize=max_queue_size)
        self.num_workers = num_workers
        self.retry_max = retry_max
        self.retry_base_sec = retry_base_sec
        self.workers = []
        self.running = False
        self._stop_event = threading.Event()
        
        # Metrics
        self._processed_count = 0
        self._failed_count = 0
        self._retry_count = 0
        self._metrics_lock = threading.Lock()
        
    def start(self) -> None:
        """Start worker threads."""
        if self.running:
            _LOGGER.warning('Inbound processor queue already running')
            return
            
        self.running = True
        self._stop_event.clear()
        
        for i in range(self.num_workers):
            worker = threading.Thread(
                target=self._worker_loop,
                name=f'InboundProcessor-{i}',
                daemon=True,
            )
            worker.start()
            self.workers.append(worker)
            
        _LOGGER.info('Inbound processor queue started with %s workers', self.num_workers)
    
    def stop(self, timeout: float = 10.0) -> None:
        """Stop all worker threads gracefully."""
        if not self.running:
            return
            
        _LOGGER.info('Stopping inbound processor queue...')
        self.running = False
        self._stop_event.set()
        
        # Wait for workers to finish
        for worker in self.workers:
            worker.join(timeout=timeout)
            
        self.workers.clear()
        
        with self._metrics_lock:
            _LOGGER.info(
                'Inbound processor queue stopped - processed=%s failed=%s retries=%s',
                self._processed_count,
                self._failed_count,
                self._retry_count,
            )
    
    def enqueue(self, inbound_pk: int) -> bool:
        """
        Add InboundSms to processing queue.
        
        Args:
            inbound_pk: InboundSms primary key
            
        Returns:
            True if enqueued successfully, False if queue is full
        """
        try:
            self.queue.put(inbound_pk, block=False)
            _LOGGER.info(
                'Enqueued inbound pk=%s queue_size=%s',
                inbound_pk,
                self.queue.qsize(),
            )
            return True
        except queue.Full:
            _LOGGER.error('Inbound processor queue is full! Dropping inbound_pk=%s', inbound_pk)
            return False
    
    def get_metrics(self) -> dict[str, int]:
        """Get current processing metrics."""
        with self._metrics_lock:
            return {
                'processed': self._processed_count,
                'failed': self._failed_count,
                'retries': self._retry_count,
                'queue_size': self.queue.qsize(),
            }
    
    def _worker_loop(self) -> None:
        """Worker thread main loop - processes InboundSms from queue."""
        worker_name = threading.current_thread().name
        _LOGGER.info('%s started', worker_name)
        
        while self.running and not self._stop_event.is_set():
            try:
                # Get InboundSms pk from queue with timeout to check stop_event periodically
                try:
                    inbound_pk = self.queue.get(timeout=1.0)
                except queue.Empty:
                    continue
                
                # Process InboundSms with retry logic
                self._process_inbound(inbound_pk, worker_name)
                self.queue.task_done()
                
            except Exception as exc:
                _LOGGER.exception('%s encountered error: %s', worker_name, exc)
                time.sleep(0.5)  # Brief pause before retrying
        
        _LOGGER.info('%s stopped', worker_name)
    
    def _process_inbound(self, inbound_pk: int, worker_name: str) -> None:
        """
        Process a single InboundSms: mirror to devices and publish to MQTT.
        
        Args:
            inbound_pk: InboundSms primary key
            worker_name: Name of worker thread (for logging)
        """
        from django.conf import settings
        from apps.sms.models import InboundSms
        from apps.external_device.services import (
            publish_inbound_to_remote,
            publish_inbound_to_remote_ephemeral,
            sync_single_inbound_to_all_devices,
        )
        
        start_time = time.time()
        
        try:
            _LOGGER.debug('%s processing inbound_pk=%s', worker_name, inbound_pk)
            
            # Fetch InboundSms instance (retry on SQLite lock — worker is outside HTTP txn)
            try:
                inbound = _fetch_inbound_sms(inbound_pk)
            except InboundSms.DoesNotExist:
                _LOGGER.warning('%s: InboundSms pk=%s does not exist', worker_name, inbound_pk)
                with self._metrics_lock:
                    self._failed_count += 1
                return
            
            # Mirror to local devices (non-blocking, sequential due to SQLite constraints)
            try:
                sync_single_inbound_to_all_devices(inbound)
            except Exception as exc:
                _LOGGER.exception(
                    '%s: Failed to mirror inbound_pk=%s to devices: %s',
                    worker_name,
                    inbound_pk,
                    exc,
                )
                # Continue to remote publish even if local mirror fails
            
            # Publish to remote hiDisheLink broker if bridge mode enabled (with retries)
            if getattr(settings, 'MQTT_REMOTE_BRIDGE_ENABLED', False):
                from apps.external_device import mqtt_client as mqtt_mod
                remote_client = getattr(mqtt_mod, '_global_remote_client', None)

                if remote_client is not None:
                    success = self._publish_with_retry(
                        inbound,
                        remote_client,
                        worker_name,
                    )
                else:
                    success = self._publish_ephemeral_with_retry(
                        inbound,
                        worker_name,
                    )

                if not success:
                    _LOGGER.error(
                        '%s: Failed to publish inbound_pk=%s to remote after %s retries '
                        '(local mirror may have succeeded)',
                        worker_name,
                        inbound_pk,
                        self.retry_max,
                    )
                    with self._metrics_lock:
                        self._failed_count += 1

            elapsed = time.time() - start_time
            _LOGGER.info(
                '%s completed inbound_pk=%s in %.3fs processed=%s',
                worker_name,
                inbound_pk,
                elapsed,
                self._processed_count + 1,
            )

            with self._metrics_lock:
                self._processed_count += 1
                
        except Exception as exc:
            elapsed = time.time() - start_time
            _LOGGER.exception(
                '%s failed after %.3fs: inbound_pk=%s error=%s',
                worker_name,
                elapsed,
                inbound_pk,
                exc,
            )
            with self._metrics_lock:
                self._failed_count += 1
    
    def _publish_with_retry(self, inbound, remote_client, worker_name: str) -> bool:
        """
        Publish InboundSms to remote broker with exponential backoff retry.
        
        Args:
            inbound: InboundSms instance
            remote_client: RemoteHiDishelinkClient instance
            worker_name: Name of worker thread (for logging)
            
        Returns:
            True if published successfully (within retry limit), False otherwise
        """
        from apps.external_device.services import publish_inbound_to_remote
        
        for attempt in range(self.retry_max):
            try:
                success = publish_inbound_to_remote(inbound, remote_client)
                if success:
                    if attempt > 0:
                        _LOGGER.info(
                            '%s: Published inbound_pk=%s to remote on attempt %s',
                            worker_name,
                            inbound.pk,
                            attempt + 1,
                        )
                    return True
                
                # publish_inbound_to_remote returned False (network/timeout issue)
                if attempt < self.retry_max - 1:
                    delay = self.retry_base_sec * (2 ** attempt)
                    delay = min(delay, 60.0)  # Cap at 60 seconds
                    _LOGGER.warning(
                        '%s: Remote publish failed for inbound_pk=%s (attempt %s/%s), retrying in %.1fs',
                        worker_name,
                        inbound.pk,
                        attempt + 1,
                        self.retry_max,
                        delay,
                    )
                    with self._metrics_lock:
                        self._retry_count += 1
                    time.sleep(delay)
                
            except Exception as exc:
                _LOGGER.warning(
                    '%s: Exception during remote publish for inbound_pk=%s (attempt %s/%s): %s',
                    worker_name,
                    inbound.pk,
                    attempt + 1,
                    self.retry_max,
                    exc,
                )
                if attempt < self.retry_max - 1:
                    delay = self.retry_base_sec * (2 ** attempt)
                    delay = min(delay, 60.0)
                    with self._metrics_lock:
                        self._retry_count += 1
                    time.sleep(delay)
        
        return False

    def _publish_ephemeral_with_retry(self, inbound, worker_name: str) -> bool:
        """Publish via ephemeral MQTT when persistent remote client is in another process."""
        from apps.external_device.services import publish_inbound_to_remote_ephemeral

        for attempt in range(self.retry_max):
            try:
                success = publish_inbound_to_remote_ephemeral(inbound)
                if success:
                    if attempt > 0:
                        _LOGGER.info(
                            '%s: Remote ephemeral publish inbound_pk=%s on attempt %s',
                            worker_name,
                            inbound.pk,
                            attempt + 1,
                        )
                    return True

                if attempt < self.retry_max - 1:
                    delay = min(self.retry_base_sec * (2 ** attempt), 60.0)
                    _LOGGER.warning(
                        '%s: Remote ephemeral publish failed inbound_pk=%s (attempt %s/%s), retry in %.1fs',
                        worker_name,
                        inbound.pk,
                        attempt + 1,
                        self.retry_max,
                        delay,
                    )
                    with self._metrics_lock:
                        self._retry_count += 1
                    time.sleep(delay)

            except Exception as exc:
                _LOGGER.warning(
                    '%s: Exception during remote ephemeral publish inbound_pk=%s (attempt %s/%s): %s',
                    worker_name,
                    inbound.pk,
                    attempt + 1,
                    self.retry_max,
                    exc,
                )
                if attempt < self.retry_max - 1:
                    delay = min(self.retry_base_sec * (2 ** attempt), 60.0)
                    with self._metrics_lock:
                        self._retry_count += 1
                    time.sleep(delay)

        return False


# Global singleton instance
_global_processor: Optional[InboundProcessorQueue] = None
_processor_lock = threading.Lock()


def get_inbound_processor() -> InboundProcessorQueue:
    """Get or create global inbound processor queue singleton."""
    global _global_processor
    
    if _global_processor is None:
        with _processor_lock:
            if _global_processor is None:
                num_workers = int(os.environ.get('INBOUND_PROCESSOR_WORKERS', '2'))
                max_size = int(os.environ.get('INBOUND_PROCESSOR_MAX_SIZE', '500'))
                retry_max = int(os.environ.get('INBOUND_PROCESSOR_RETRY_MAX', '5'))
                retry_base = float(os.environ.get('INBOUND_PROCESSOR_RETRY_BASE_SEC', '1.0'))
                
                # Disable processor if num_workers is 0
                if num_workers == 0:
                    _LOGGER.warning('INBOUND_PROCESSOR_WORKERS=0: processor disabled')
                    return None  # type: ignore[return-value]
                
                _global_processor = InboundProcessorQueue(
                    num_workers=num_workers,
                    max_queue_size=max_size,
                    retry_max=retry_max,
                    retry_base_sec=retry_base,
                )
                _global_processor.start()
    
    return _global_processor
