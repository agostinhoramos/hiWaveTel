"""Dedicated worker process for durable webhook delivery jobs."""

from __future__ import annotations

import logging
import os
import signal
import threading
import time

from django.core.management.base import BaseCommand
from django.db import OperationalError

from apps.sms.services import _looks_sqlite_concurrency_error
from apps.sms.webhook_outbox import claim_next_job, process_webhook_job, reset_stale_processing_jobs

_LOGGER = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        'Process WebhookDeliveryJob rows from the database (separate from SMS detection). '
        'Run one instance per container; uses WEBHOOK_WORKER_THREADS worker threads.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--workers',
            type=int,
            default=None,
            help='Worker threads (default: WEBHOOK_WORKER_THREADS or INBOUND_PROCESSOR_WORKERS).',
        )
        parser.add_argument(
            '--poll-sec',
            type=float,
            default=None,
            help='Poll interval when queue empty (default: WEBHOOK_WORKER_POLL_SEC).',
        )

    def handle(self, *args, **options):
        workers = options['workers']
        if workers is None:
            workers = int(os.environ.get(
                'WEBHOOK_WORKER_THREADS',
                os.environ.get('INBOUND_PROCESSOR_WORKERS', '2'),
            ))
        poll_sec = options['poll_sec']
        if poll_sec is None:
            poll_sec = float(os.environ.get('WEBHOOK_WORKER_POLL_SEC', '0.5'))

        if workers <= 0:
            self.stderr.write(self.style.ERROR('workers must be > 0'))
            return

        stop = threading.Event()

        def _handle_stop(signum, frame):  # noqa: ARG001, ANN001
            self.stdout.write(self.style.WARNING(f'Webhook worker stopping (signal {signum})...'))
            stop.set()

        signal.signal(signal.SIGTERM, _handle_stop)
        signal.signal(signal.SIGINT, _handle_stop)

        reset_stale_processing_jobs()

        self.stdout.write(
            self.style.NOTICE(
                f'Webhook worker started threads={workers} poll_sec={poll_sec}',
            ),
        )

        def worker_loop(worker_id: int) -> None:
            while not stop.is_set():
                try:
                    job = claim_next_job()
                except OperationalError as exc:
                    if _looks_sqlite_concurrency_error(exc):
                        _LOGGER.warning('WebhookWorker-%s: database locked claiming job; backing off', worker_id)
                        stop.wait(poll_sec)
                        continue
                    _LOGGER.exception('WebhookWorker-%s: claim failed', worker_id)
                    stop.wait(poll_sec)
                    continue
                except Exception:
                    _LOGGER.exception('WebhookWorker-%s: unexpected claim error', worker_id)
                    stop.wait(poll_sec)
                    continue

                if job is None:
                    stop.wait(poll_sec)
                    continue

                try:
                    process_webhook_job(job)
                except OperationalError as exc:
                    if _looks_sqlite_concurrency_error(exc):
                        _LOGGER.warning(
                            'WebhookWorker-%s: database locked processing job %s; will retry',
                            worker_id,
                            job.pk,
                        )
                    else:
                        _LOGGER.exception('WebhookWorker-%s: job %s DB error', worker_id, job.pk)
                except Exception:
                    _LOGGER.exception('WebhookWorker-%s: job %s failed', worker_id, job.pk)

        threads = [
            threading.Thread(target=worker_loop, args=(i,), name=f'WebhookWorker-{i}', daemon=True)
            for i in range(workers)
        ]
        for thread in threads:
            thread.start()

        try:
            while not stop.is_set():
                stop.wait(1.0)
        finally:
            stop.set()
            for thread in threads:
                thread.join(timeout=10.0)
            self.stdout.write(self.style.NOTICE('Webhook worker stopped.'))
