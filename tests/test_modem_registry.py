"""Tests for modem registry sync and helpers."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from apps.sms.modem_registry import (
    ModemNotEnumeratedError,
    assert_modem_enumerated,
    sync_detected_modems,
)
from apps.sms.models import ModemDevice


@pytest.mark.django_db
def test_sync_detected_modems_upserts_and_marks_absent():
    ModemDevice.objects.create(modem_index=9, dbus_path='/old', is_present=True)

    list_cp = SimpleNamespace(
        returncode=0,
        stdout='/org/freedesktop/ModemManager1/Modem/0\n/org/freedesktop/ModemManager1/Modem/1\n',
        stderr='',
    )
    client = MagicMock()
    client.mmcli_path = 'mmcli'
    client._run.return_value = list_cp
    client._ensure_ok.return_value = None

    devices = sync_detected_modems(client=client)

    assert [d.modem_index for d in devices] == [0, 1, 9]
    assert ModemDevice.objects.get(modem_index=0).is_present is True
    assert ModemDevice.objects.get(modem_index=0).dbus_path.endswith('/Modem/0')
    assert ModemDevice.objects.get(modem_index=9).is_present is False


@pytest.mark.django_db
def test_assert_modem_enumerated_raises_when_missing():
    client = MagicMock()
    client.list_modem_indices.return_value = [0]

    assert_modem_enumerated(0, client=client)

    with pytest.raises(ModemNotEnumeratedError, match='not enumerated'):
        assert_modem_enumerated(2, client=client)


@pytest.mark.django_db
def test_sync_detected_modems_handles_mmcli_failure():
    ModemDevice.objects.create(modem_index=0, is_present=True)
    client = MagicMock()
    client.mmcli_path = 'mmcli'
    from apps.sms.mmcli_client import MmcliError

    client._run.side_effect = MmcliError('mmcli -L failed')

    with patch('apps.sms.modem_registry._LOGGER'):
        devices = sync_detected_modems(client=client)

    assert len(devices) == 1
    assert devices[0].modem_index == 0
