"""Thread-safe SMS processing queue with worker pool."""

import logging
import queue
import threading
import time
from typing import Optional

_LOGGER = logging.getLogger(__name__)


class SmsProcessingQueue:
    """Thread-safe queue for processing SMS messages asynchronously."""
    
    def __init__(self, num_workers: int = 3, max_queue_size: int = 1000):
        """
        Initialize SMS processing queue.
        
        Args:
            num_workers: Number of worker threads to spawn
            max_queue_size: Maximum number of SMS in queue before blocking
        """
        self.queue = queue.Queue(maxsize=max_queue_size)
        self.num_workers = num_workers
        self.workers = []
        self.running = False
        self._stop_event = threading.Event()
        
    def start(self) -> None:
        """Start worker threads."""
        if self.running:
            _LOGGER.warning('SMS queue already running')
            return
            
        self.running = True
        self._stop_event.clear()
        
        for i in range(self.num_workers):
            worker = threading.Thread(
                target=self._worker_loop,
                name=f'SMSWorker-{i}',
                daemon=True,
            )
            worker.start()
            self.workers.append(worker)
            
        _LOGGER.info('SMS processing queue started with %s workers', self.num_workers)
    
    def stop(self, timeout: float = 10.0) -> None:
        """Stop all worker threads gracefully."""
        if not self.running:
            return
            
        _LOGGER.info('Stopping SMS processing queue...')
        self.running = False
        self._stop_event.set()
        
        # Wait for workers to finish
        for worker in self.workers:
            worker.join(timeout=timeout)
            
        self.workers.clear()
        _LOGGER.info('SMS processing queue stopped')
    
    def enqueue(self, sms_path: str, modem_index: int) -> bool:
        """
        Add SMS to processing queue.
        
        Args:
            sms_path: ModemManager SMS path
            modem_index: Modem index
            
        Returns:
            True if enqueued successfully, False if queue is full
        """
        try:
            self.queue.put((sms_path, modem_index), block=False)
            _LOGGER.debug('Enqueued SMS: path=%s modem=%s queue_size=%s', 
                         sms_path, modem_index, self.queue.qsize())
            return True
        except queue.Full:
            _LOGGER.error('SMS queue is full! Dropping message: %s', sms_path)
            return False
    
    def _worker_loop(self) -> None:
        """Worker thread main loop - processes SMS from queue."""
        worker_name = threading.current_thread().name
        _LOGGER.info('%s started', worker_name)
        
        while self.running and not self._stop_event.is_set():
            try:
                # Get SMS from queue with timeout to check stop_event periodically
                try:
                    sms_path, modem_index = self.queue.get(timeout=1.0)
                except queue.Empty:
                    continue
                
                # Process SMS
                self._process_sms(sms_path, modem_index, worker_name)
                self.queue.task_done()
                
            except Exception as exc:
                _LOGGER.exception('%s encountered error: %s', worker_name, exc)
                time.sleep(0.5)  # Brief pause before retrying
        
        _LOGGER.info('%s stopped', worker_name)
    
    def _process_sms(self, sms_path: str, modem_index: int, worker_name: str) -> None:
        """
        Process a single SMS message.
        
        Args:
            sms_path: ModemManager SMS path
            modem_index: Modem index
            worker_name: Name of worker thread (for logging)
        """
        from .services import persist_inbound_sms
        
        start_time = time.time()
        
        try:
            _LOGGER.debug('%s processing: path=%s modem=%s', 
                         worker_name, sms_path, modem_index)
            
            persist_inbound_sms(sms_path, modem_index)
            
            elapsed = time.time() - start_time
            _LOGGER.debug('%s completed in %.3fs: path=%s', 
                         worker_name, elapsed, sms_path)
            
        except Exception as exc:
            elapsed = time.time() - start_time
            _LOGGER.exception('%s failed after %.3fs: path=%s error=%s', 
                            worker_name, elapsed, sms_path, exc)


# Global singleton instance
_global_queue: Optional[SmsProcessingQueue] = None
_queue_lock = threading.Lock()


def get_sms_queue() -> SmsProcessingQueue:
    """Get or create global SMS processing queue singleton."""
    global _global_queue
    
    if _global_queue is None:
        with _queue_lock:
            if _global_queue is None:
                import os
                num_workers = int(os.environ.get('SMS_QUEUE_WORKERS', '3'))
                max_size = int(os.environ.get('SMS_QUEUE_MAX_SIZE', '1000'))
                
                _global_queue = SmsProcessingQueue(
                    num_workers=num_workers,
                    max_queue_size=max_size,
                )
                _global_queue.start()
    
    return _global_queue
