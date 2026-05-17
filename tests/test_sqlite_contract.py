"""Smoke tests for SQLite tuning and inbound persist resilience."""

from __future__ import annotations

import pytest
from django.conf import settings
from django.db.utils import OperationalError

from apps.sms.models import InboundSms
from apps.sms.services import _looks_sqlite_concurrency_error, persist_inbound_sms


def test_sqlite_busy_timeout_configured():
    opts = settings.DATABASES['default']['OPTIONS']
    assert opts.get('timeout', 0) >= 5.0


def test_concurrency_predicate():
    assert _looks_sqlite_concurrency_error(OperationalError('database is locked'))
    assert _looks_sqlite_concurrency_error(OperationalError('database table is locked'))
    assert _looks_sqlite_concurrency_error(Exception('SQLite busy'))
    assert not _looks_sqlite_concurrency_error(OperationalError('no such table: foo'))


@pytest.mark.django_db
def test_persist_inbound_retries_after_database_locked(monkeypatch):
    """First get_or_create can fail transiently under SQLite load; retries must succeed."""

    class StubMM:
        def show_sms(self, path: str) -> dict[str, str]:
            return {}

    wrapped = InboundSms.objects.get_or_create
    attempts = {'n': 0}

    def flaky_get_or_create(*args, **kwargs):
        attempts['n'] += 1
        if attempts['n'] == 1:
            raise OperationalError('database is locked')
        return wrapped(*args, **kwargs)

    monkeypatch.setattr(InboundSms.objects, 'get_or_create', flaky_get_or_create)

    path = '/org/freedesktop/ModemManager1/SMS/retry_contract'
    inbound = persist_inbound_sms(path, 0, client=StubMM())  # type: ignore[arg-type]
    assert inbound.mm_path == path
    assert attempts['n'] == 2

