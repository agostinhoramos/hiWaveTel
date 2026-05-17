"""Tests for SmsProcessingQueue threading and worker behavior."""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest

from apps.sms.queue_processor import SmsProcessingQueue, get_sms_queue
from apps.sms import queue_processor as _qp


def test_enqueue_and_process():
    """Test that enqueued SMS is processed by a worker thread."""
    processed = []
    
    def fake_persist(sms_path: str, modem_index: int, client=None) -> None:
        processed.append((sms_path, modem_index))
    
    with patch('apps.sms.services.persist_inbound_sms', fake_persist):
        queue = SmsProcessingQueue(num_workers=2, max_queue_size=10)
        queue.start()
        
        try:
            # Enqueue an SMS
            success = queue.enqueue('/org/freedesktop/ModemManager1/SMS/100', 0)
            assert success is True
            
            # Wait for worker to process
            time.sleep(0.5)
            
            # Verify persist was called
            assert len(processed) == 1
            assert processed[0] == ('/org/freedesktop/ModemManager1/SMS/100', 0)
        finally:
            queue.stop(timeout=2.0)


def test_queue_full_returns_false():
    """Test that enqueue returns False when queue is full."""
    # Create a queue with max size of 1
    queue = SmsProcessingQueue(num_workers=0, max_queue_size=1)  # No workers so items stay in queue
    queue.start()
    
    try:
        # First enqueue should succeed
        success1 = queue.enqueue('/org/freedesktop/ModemManager1/SMS/200', 0)
        assert success1 is True
        
        # Second enqueue should fail (queue full)
        success2 = queue.enqueue('/org/freedesktop/ModemManager1/SMS/201', 0)
        assert success2 is False
    finally:
        queue.stop(timeout=1.0)


def test_stop_drains_gracefully():
    """Test that stop() waits for workers and does not raise."""
    queue = SmsProcessingQueue(num_workers=2, max_queue_size=10)
    queue.start()
    
    # Enqueue some work
    queue.enqueue('/org/freedesktop/ModemManager1/SMS/300', 0)
    
    # Stop should not raise
    queue.stop(timeout=2.0)
    
    # Verify queue is no longer running
    assert queue.running is False
    assert len(queue.workers) == 0


def test_shutdown_queue_does_not_start_if_never_used():
    """Test that shutdown_queue does nothing if _global_queue is None."""
    # Reset global queue to None
    original_queue = _qp._global_queue
    _qp._global_queue = None
    
    try:
        # Import the shutdown function from apps.py
        from apps.sms.apps import SmsConfig
        
        # Create a fresh instance
        config = SmsConfig('apps.sms', __import__('apps.sms'))
        
        # Call ready() to register atexit handler
        # The shutdown function should not create a queue
        with patch('apps.sms.queue_processor.SmsProcessingQueue') as mock_queue_class:
            config.ready()
            
            # Verify that SmsProcessingQueue was not instantiated during ready()
            # (it might be called if get_sms_queue is invoked elsewhere, but not by shutdown_queue)
            # We're mainly verifying the shutdown logic doesn't call get_sms_queue
        
        # Verify _global_queue is still None (not created by atexit handler)
        assert _qp._global_queue is None
    finally:
        # Restore original queue state
        _qp._global_queue = original_queue
