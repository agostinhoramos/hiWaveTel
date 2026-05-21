"""Tests for inbound SMS reliability (DLQ, metrics, recovery, retries)."""

from __future__ import annotations

import asyncio
import io
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import Client, override_settings
from django.urls import reverse
from rest_framework.test import APIClient

from apps.sms.dead_letter_queue import SmsDeadLetterQueue, enqueue_persist_failure, get_sms_dlq
from apps.sms.dbus_watch import _make_on_added_callback, _periodic_recovery_loop, sync_modem_sms_snapshot
from apps.sms.metrics import get_metrics_collector
from apps.sms.mmcli_client import MMCLIClient, MmcliError
from apps.sms.models import InboundSms
from apps.sms.queue_processor import SmsProcessingQueue
from apps.sms.services import _should_retry_empty_mmcli_snapshot, persist_inbound_sms


@pytest.fixture
def dlq_db_path(tmp_path):
    return str(tmp_path / 'test_dlq.db')


@pytest.fixture
def sms_dlq(dlq_db_path):
    return SmsDeadLetterQueue(
        db_path=dlq_db_path,
        max_size=100,
        retry_interval_sec=1,
        max_retries=3,
    )


@pytest.mark.django_db
class TestSmsDeadLetterQueue:
    def test_enqueue_and_process_batch_recovers(self, sms_dlq):
        path = '/org/freedesktop/ModemManager1/SMS/dlq1'
        assert sms_dlq.enqueue(path, 0, 'test error')

        client = MMCLIClient()
        client.show_sms = MagicMock(
            return_value={
                'number': '+351900000001',
                'text': 'recovered',
                'state': 'received',
            },
        )

        with patch('apps.sms.services.persist_inbound_sms') as mock_persist:
            mock_persist.return_value = InboundSms.objects.create(
                mm_path=path,
                modem_index=0,
                from_number='+351900000001',
                text='recovered',
            )
            stats = sms_dlq.process_batch(limit=10)

        assert stats['recovered'] == 1
        assert sms_dlq.pending_count() == 0

    def test_mark_retry_failed_increments_count(self, sms_dlq):
        path = '/org/freedesktop/ModemManager1/SMS/dlq2'
        sms_dlq.enqueue(path, 0, 'fail')

        with sms_dlq._connect() as conn:
            row = conn.execute('SELECT id FROM sms_dlq WHERE sms_path=?', (path,)).fetchone()
            row_id = int(row['id'])

        with patch('apps.sms.services.persist_inbound_sms', side_effect=RuntimeError('nope')):
            stats = sms_dlq.process_batch(limit=10)

        assert stats['failed'] == 1
        with sms_dlq._connect() as conn:
            retry_count = conn.execute(
                'SELECT retry_count FROM sms_dlq WHERE id=?',
                (row_id,),
            ).fetchone()['retry_count']
        assert retry_count == 1


@pytest.mark.django_db
class TestSmsMetricsCollector:
    def test_increment_and_get_stats(self):
        collector = get_metrics_collector()
        before = collector.get_stats()['persist_success']
        collector.increment('persist_success')
        after = collector.get_stats()['persist_success']
        assert after == before + 1


@pytest.mark.django_db
class TestEmptyTextRetryLogic:
    def test_retries_when_sender_present_in_terminal_state(self):
        raw = {'number': '+351900000001'}
        assert _should_retry_empty_mmcli_snapshot(raw, 'received') is True

    def test_no_retry_for_failed_state(self):
        raw = {'number': '+351900000001'}
        assert _should_retry_empty_mmcli_snapshot(raw, 'failed') is False


@pytest.mark.django_db
class TestQueueDlqIntegration:
    @override_settings(SMS_DLQ_ENABLED=True, SMS_DLQ_DB_PATH='/tmp/test_queue_dlq.db')
    def test_worker_failure_enqueues_dlq(self, tmp_path, settings):
        settings.SMS_DLQ_DB_PATH = str(tmp_path / 'queue_dlq.db')
        queue = SmsProcessingQueue(num_workers=1, max_queue_size=10)

        with patch('apps.sms.services.persist_inbound_sms', side_effect=RuntimeError('boom')):
            with patch('apps.sms.dead_letter_queue.get_sms_dlq') as mock_get_dlq:
                dlq = SmsDeadLetterQueue(
                    db_path=str(tmp_path / 'queue_dlq.db'),
                    max_size=50,
                    retry_interval_sec=60,
                    max_retries=5,
                )
                mock_get_dlq.return_value = dlq
                queue._process_sms('/org/freedesktop/ModemManager1/SMS/q1', 0, 'TestWorker')

        assert dlq.pending_count() == 1


@pytest.mark.django_db
class TestDbusWatchReliability:
    def test_on_added_fallback_to_dlq_on_queue_full(self, tmp_path, settings):
        settings.SMS_DLQ_ENABLED = True
        settings.SMS_DLQ_DB_PATH = str(tmp_path / 'dbus_dlq.db')

        callback = _make_on_added_callback(0)
        mock_queue = MagicMock()
        mock_queue.enqueue.return_value = False

        with patch('apps.sms.dbus_watch.get_sms_queue', return_value=mock_queue):
            with patch('apps.sms.dbus_watch.persist_inbound_sms', side_effect=RuntimeError('persist fail')):
                with patch('apps.sms.dead_letter_queue.get_sms_dlq') as mock_get_dlq:
                    dlq = SmsDeadLetterQueue(
                        db_path=str(tmp_path / 'dbus_dlq.db'),
                        max_size=50,
                        retry_interval_sec=60,
                        max_retries=5,
                    )
                    mock_get_dlq.return_value = dlq

                    callback('/org/freedesktop/ModemManager1/SMS/dbus1', received=True)

        assert dlq.pending_count() == 1
        metrics = get_metrics_collector().get_stats()
        assert metrics['dbus_signals_received'] >= 1

    def test_periodic_recovery_calls_snapshot(self):
        async def _run() -> None:
            with patch('apps.sms.dbus_watch.sync_modem_sms_snapshot', return_value=2) as mock_sync:
                task = asyncio.create_task(_periodic_recovery_loop(0, 0.05))
                await asyncio.sleep(0.12)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            assert mock_sync.call_count >= 1

        asyncio.run(_run())


@pytest.mark.django_db
class TestMmcliShowRetries:
    def test_show_sms_uses_retrying_run(self):
        client = MMCLIClient()
        kv_cp = MagicMock()
        kv_cp.stdout = 'sms.content.text: hello\nsms.content.number: +351900000001\n'
        kv_cp.stderr = ''
        kv_cp.returncode = 0
        json_cp = MagicMock(stdout='{}', stderr='', returncode=0)

        with patch.object(client, '_retrying_run', side_effect=[kv_cp, json_cp]) as mock_retry:
            with patch.object(client, '_ensure_ok', return_value=None):
                result = client.show_sms('/org/freedesktop/ModemManager1/SMS/retry1')
        assert mock_retry.call_count >= 1
        assert result.get('smscontenttext') == 'hello' or result.get('text') == 'hello'


@pytest.mark.django_db
class TestRecoverMissingSmsCommand:
    @patch('apps.sms.management.commands.recover_missing_sms.sync_modem_sms_snapshot', return_value=3)
    def test_command_runs_snapshot(self, mock_sync):
        out = io.StringIO()
        call_command('recover_missing_sms', stdout=out)
        mock_sync.assert_called_once()
        assert 'Modem snapshot synced 3 path(s)' in out.getvalue()

    @patch('apps.sms.management.commands.recover_missing_sms.refresh_stale_inbound_sms_rows')
    def test_command_refresh_stale(self, mock_refresh):
        mock_refresh.return_value = {
            'checked': 2,
            'text_filled': 1,
            'state_updated': 1,
            'still_stale': 0,
        }
        out = io.StringIO()
        call_command('recover_missing_sms', '--refresh-stale', stdout=out)
        mock_refresh.assert_called_once_with(0)
        assert 'Stale InboundSms refresh' in out.getvalue()
        assert 'text_filled' in out.getvalue()

    @override_settings(SMS_DLQ_ENABLED=True)
    def test_command_processes_dlq(self, tmp_path, settings):
        settings.SMS_DLQ_DB_PATH = str(tmp_path / 'cmd_dlq.db')
        dlq = SmsDeadLetterQueue(
            db_path=str(tmp_path / 'cmd_dlq.db'),
            max_size=50,
            retry_interval_sec=60,
            max_retries=5,
        )
        dlq.enqueue('/org/freedesktop/ModemManager1/SMS/cmd1', 0, 'err')

        with patch('apps.sms.management.commands.recover_missing_sms.get_sms_dlq', return_value=dlq):
            with patch('apps.sms.services.persist_inbound_sms') as mock_persist:
                mock_persist.return_value = InboundSms.objects.create(
                    mm_path='/org/freedesktop/ModemManager1/SMS/cmd1',
                    modem_index=0,
                    text='ok',
                )
                out = io.StringIO()
                call_command('recover_missing_sms', '--check-dlq', stdout=out)

        assert 'DLQ processed' in out.getvalue()


@pytest.mark.django_db
class TestSmsMetricsEndpoint:
    def test_metrics_endpoint_requires_admin(self):
        client = APIClient()
        url = reverse('sms-metrics')
        response = client.get(url)
        assert response.status_code in (401, 403)

    def test_metrics_endpoint_returns_stats(self):
        User = get_user_model()
        admin = User.objects.create_superuser('smsmetrics', 'm@test.invalid', 'pw-metrics-admin')
        client = APIClient()
        client.force_authenticate(user=admin)
        url = reverse('sms-metrics')
        response = client.get(url)
        assert response.status_code == 200
        assert 'persist_success' in response.data
        assert 'dlq_pending' in response.data
