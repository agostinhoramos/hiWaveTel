"""Tests for modem identity probe (mmcli)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from apps.sms.modem_identity import normalize_phone_e164, probe_modem_identity


@pytest.mark.parametrize(
    ('raw', 'expected'),
    [
        ('351912329317', '+351912329317'),
        ('+351912329317', '+351912329317'),
        ("'+351912329317'", '+351912329317'),
        ('', ''),
        ('  ', ''),
    ],
)
def test_normalize_phone_e164(raw, expected):
    assert normalize_phone_e164(raw) == expected


def test_probe_modem_identity_from_mmcli():
    mock_client = MagicMock()
    mock_client.show_modem.return_value = {
        'genericmanufacturer': 'QUALCOMM INCORPORATED',
        'genericmodel': 'QUECTEL EC25',
        'genericequipmentidentifier': '861585043942216',
        'genericrevision': 'EC25AUGCR06A02M1G',
        'ownnumbers': '+351912329317',
        'modemsim': '/org/freedesktop/ModemManager1/SIM/0',
    }
    mock_client.mmcli_path = 'mmcli'
    mock_client._run.return_value = MagicMock(returncode=0, stdout='')

    identity = probe_modem_identity(0, client=mock_client, phone_override='')

    assert identity['phone_number'] == '+351912329317'
    assert identity['manufacturer'] == 'QUALCOMM INCORPORATED'
    assert identity['model'] == 'QUECTEL EC25'
    assert identity['imei'] == '861585043942216'


def test_probe_modem_identity_env_fallback(monkeypatch):
    mock_client = MagicMock()
    mock_client.show_modem.return_value = {}
    mock_client.mmcli_path = 'mmcli'

    monkeypatch.setenv('DEVICE_PHONE_NUMBER', '351913000387')
    identity = probe_modem_identity(0, client=mock_client)

    assert identity['phone_number'] == '+351913000387'
