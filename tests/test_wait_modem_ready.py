"""Tests for wait_modem_ready management command."""

from __future__ import annotations

from io import StringIO
from unittest.mock import patch

import pytest
from django.core.management import call_command


@pytest.mark.django_db
def test_wait_modem_ready_all_success():
    out = StringIO()
    with patch('apps.sms.management.commands.wait_modem_ready.list_present_modem_indices', return_value=[0, 1]):
        with patch('apps.sms.management.commands.wait_modem_ready.ensure_modem_ready_for_sms', return_value=True):
            call_command('wait_modem_ready', '--all', stdout=out)
    text = out.getvalue()
    assert 'Modem 0 ready' in text
    assert 'Modem 1 ready' in text


@pytest.mark.django_db
def test_wait_modem_ready_all_no_modems():
    out = StringIO()
    with patch('apps.sms.management.commands.wait_modem_ready.list_present_modem_indices', return_value=[]):
        call_command('wait_modem_ready', '--all', stdout=out)
    assert 'No modems to wait for' in out.getvalue()
