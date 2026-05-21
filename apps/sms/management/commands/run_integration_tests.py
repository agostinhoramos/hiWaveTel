"""Run integration tests for SMS system with auto-remediation."""

from __future__ import annotations

import time
from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.sms.dead_letter_queue import get_sms_dlq
from apps.sms.dbus_watch import sync_modem_sms_snapshot
from apps.sms.metrics import get_metrics_collector
from apps.sms.models import InboundSms, OutboundSms
from apps.sms.services import dispatch_outbound_mmcli, refresh_stale_inbound_sms_rows


class Command(BaseCommand):
    help = 'Run integration tests for SMS system with auto-remediation.'

    def add_arguments(self, parser):  # type: ignore[override]
        parser.add_argument(
            '--test-number',
            type=str,
            default='+351913000387',
            help='Number to send test SMS to (default: +351913000387).',
        )
        parser.add_argument(
            '--modem-index',
            type=int,
            default=None,
            help='Modem index for tests (default: MODEM_MMCLI_INDEX).',
        )
        parser.add_argument(
            '--skip-send',
            action='store_true',
            help='Skip sending test SMS (only run system checks).',
        )
        parser.add_argument(
            '--skip-remediation',
            action='store_true',
            help='Skip automatic remediation of issues.',
        )

    def handle(self, *args, **options):  # type: ignore[override]
        self.stdout.write(self.style.WARNING('\n=== SMS System Integration Tests ===\n'))

        modem_index = options['modem_index']
        if modem_index is None:
            modem_index = int(getattr(settings, 'MODEM_MMCLI_INDEX', 0))

        test_number = options['test_number']
        skip_send = options['skip_send']
        skip_remediation = options['skip_remediation']

        results = {
            'passed': 0,
            'failed': 0,
            'warnings': 0,
            'remediation_applied': 0,
        }

        # Test 1: Check metrics system
        self.stdout.write('\n[Test 1] SMS Metrics System')
        try:
            metrics = get_metrics_collector()
            stats = metrics.get_stats()
            self.stdout.write(f'  ✓ Metrics collector working: {len(stats)} counters')
            self.stdout.write(f'  - Last reset: {stats.get("last_reset", "N/A")}')
            self.stdout.write(f'  - DLQ pending: {stats.get("dlq_pending", 0)}')
            results['passed'] += 1
        except Exception as exc:
            self.stdout.write(self.style.ERROR(f'  ✗ Metrics system failed: {exc}'))
            results['failed'] += 1

        # Test 2: Check DLQ system
        self.stdout.write('\n[Test 2] Dead Letter Queue')
        try:
            dlq = get_sms_dlq()
            if dlq is None:
                self.stdout.write(self.style.WARNING('  ⚠ DLQ is disabled'))
                results['warnings'] += 1
            else:
                pending = dlq.pending_count()
                self.stdout.write(f'  ✓ DLQ operational: {pending} pending items')
                if pending > 10:
                    self.stdout.write(self.style.WARNING(f'  ⚠ High DLQ pending count: {pending}'))
                    results['warnings'] += 1
                    if not skip_remediation:
                        self.stdout.write('  → Processing DLQ batch...')
                        dlq_stats = dlq.process_batch(limit=50)
                        self.stdout.write(f'  ✓ Processed: {dlq_stats}')
                        results['remediation_applied'] += 1
                results['passed'] += 1
        except Exception as exc:
            self.stdout.write(self.style.ERROR(f'  ✗ DLQ check failed: {exc}'))
            results['failed'] += 1

        # Test 3: Check for stale inbound SMS
        self.stdout.write('\n[Test 3] Stale Inbound SMS')
        try:
            stale_count = InboundSms.objects.filter(
                text='',
                mm_state__in=['receiving', 'unknown', ''],
            ).count()
            if stale_count == 0:
                self.stdout.write('  ✓ No stale inbound SMS found')
                results['passed'] += 1
            else:
                self.stdout.write(self.style.WARNING(f'  ⚠ Found {stale_count} stale inbound SMS'))
                results['warnings'] += 1
                if not skip_remediation:
                    self.stdout.write('  → Refreshing stale rows...')
                    refresh_stats = refresh_stale_inbound_sms_rows(modem_index)
                    self.stdout.write(f'  ✓ Refresh stats: {refresh_stats}')
                    results['remediation_applied'] += 1
        except Exception as exc:
            self.stdout.write(self.style.ERROR(f'  ✗ Stale check failed: {exc}'))
            results['failed'] += 1

        # Test 4: Check whitelist configuration
        self.stdout.write('\n[Test 4] Inbound Whitelist Configuration')
        try:
            whitelist = getattr(settings, 'SMS_INBOUND_WHITELIST', [])
            if not whitelist:
                self.stdout.write(self.style.WARNING('  ⚠ Whitelist is disabled (accepting all numbers)'))
                results['warnings'] += 1
            else:
                self.stdout.write(f'  ✓ Whitelist enabled: {len(whitelist)} allowed numbers')
                for num in whitelist:
                    self.stdout.write(f'    - {num}')
                results['passed'] += 1
        except Exception as exc:
            self.stdout.write(self.style.ERROR(f'  ✗ Whitelist check failed: {exc}'))
            results['failed'] += 1

        # Test 5: Database integrity
        self.stdout.write('\n[Test 5] Database Integrity')
        try:
            inbound_count = InboundSms.objects.count()
            outbound_count = OutboundSms.objects.count()
            self.stdout.write(f'  ✓ Database accessible')
            self.stdout.write(f'    - Inbound SMS: {inbound_count}')
            self.stdout.write(f'    - Outbound SMS: {outbound_count}')
            results['passed'] += 1
        except Exception as exc:
            self.stdout.write(self.style.ERROR(f'  ✗ Database check failed: {exc}'))
            results['failed'] += 1

        # Test 6: Send test SMS
        if not skip_send:
            self.stdout.write(f'\n[Test 6] Send Test SMS to {test_number}')
            try:
                # Create test SMS
                test_text = f'Integration test SMS - {timezone.now().isoformat()}'
                outbound = OutboundSms.objects.create(
                    to_number=test_number,
                    text=test_text,
                    modem_index=modem_index,
                )
                self.stdout.write(f'  → Created OutboundSms #{outbound.pk}')

                # Dispatch
                self.stdout.write('  → Dispatching via mmcli...')
                dispatch_result = dispatch_outbound_mmcli(outbound)
                
                if outbound.status == 'sent':
                    self.stdout.write(self.style.SUCCESS('  ✓ SMS sent successfully'))
                    self.stdout.write(f'    - MM path: {outbound.mm_path or "N/A"}')
                    results['passed'] += 1
                else:
                    self.stdout.write(self.style.ERROR(f'  ✗ SMS send failed: status={outbound.status}'))
                    results['failed'] += 1
                    
            except Exception as exc:
                self.stdout.write(self.style.ERROR(f'  ✗ SMS send test failed: {exc}'))
                results['failed'] += 1
        else:
            self.stdout.write('\n[Test 6] Send Test SMS - SKIPPED')

        # Test 7: Modem snapshot sync
        self.stdout.write('\n[Test 7] Modem Snapshot Sync')
        try:
            synced = sync_modem_sms_snapshot(modem_index)
            self.stdout.write(f'  ✓ Snapshot synced: {synced} SMS paths')
            results['passed'] += 1
        except Exception as exc:
            self.stdout.write(self.style.ERROR(f'  ✗ Snapshot sync failed: {exc}'))
            results['failed'] += 1

        # Summary
        self.stdout.write('\n' + '=' * 50)
        self.stdout.write('\n=== Test Summary ===')
        self.stdout.write(f'  Passed: {results["passed"]}')
        self.stdout.write(f'  Failed: {results["failed"]}')
        self.stdout.write(f'  Warnings: {results["warnings"]}')
        if not skip_remediation:
            self.stdout.write(f'  Remediation Applied: {results["remediation_applied"]}')
        
        if results['failed'] > 0:
            self.stdout.write(self.style.ERROR('\n✗ Some tests failed'))
            return
        elif results['warnings'] > 0:
            self.stdout.write(self.style.WARNING('\n⚠ All tests passed with warnings'))
        else:
            self.stdout.write(self.style.SUCCESS('\n✓ All tests passed'))
