"""Tests for run_sms_watcher --all-modems."""

from __future__ import annotations

from io import StringIO
from unittest.mock import MagicMock, patch

import pytest
from django.core.management import call_command


@pytest.mark.django_db
def test_run_sms_watcher_all_modems_spawns_subprocesses():
    out = StringIO()
    mock_popen = MagicMock()
    mock_proc = MagicMock()
    mock_proc.pid = 12345
    mock_popen.return_value = mock_proc

    with patch('apps.sms.management.commands.run_sms_watcher.list_watcher_modem_indices', return_value=[0, 1]):
        with patch('apps.sms.management.commands.run_sms_watcher.subprocess.Popen', mock_popen):
            call_command('run_sms_watcher', '--all-modems', stdout=out)

    assert mock_popen.call_count == 2
    text = out.getvalue()
    assert 'modem_index=0' in text
    assert 'modem_index=1' in text
