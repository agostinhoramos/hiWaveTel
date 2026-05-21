"""Tests for modem identity probe and HiDishelinkDevice bootstrap."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from django.core.management import call_command

from apps.external_device.hidishelink_bootstrap import ensure_hidishelink_device_from_modem
from apps.external_device.models import ExternalDevice, HiDishelinkDevice
from apps.external_device.modem_identity import normalize_phone_e164, probe_modem_identity


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


@pytest.mark.django_db
@patch('apps.external_device.hidishelink_bootstrap.probe_modem_identity')
def test_ensure_hidishelink_device_creates_rows(mock_probe):
    mock_probe.return_value = {
        'modem_index': 0,
        'phone_number': '+351912329317',
        'manufacturer': 'QUECTEL',
        'model': 'EC25',
        'imei': '861585043942216',
        'firmware': 'EC25AUG',
        'sim_path': '',
    }

    stats = ensure_hidishelink_device_from_modem(0)

    assert stats['created'] is True
    assert stats['device_id'] == '+351912329317'
    hid = HiDishelinkDevice.objects.get(pk='+351912329317')
    assert hid.status == HiDishelinkDevice.Status.UNCONFIGURED
    assert 'Auto-detected modem' in hid.notes
    assert ExternalDevice.objects.filter(pk='+351912329317').exists()


@pytest.mark.django_db
@patch('apps.external_device.hidishelink_bootstrap.probe_modem_identity')
def test_ensure_hidishelink_device_idempotent_preserves_credentials(mock_probe):
    mock_probe.return_value = {
        'modem_index': 0,
        'phone_number': '+351912329317',
        'manufacturer': 'QUECTEL',
        'model': 'EC25',
        'imei': '861585043942216',
        'firmware': 'EC25AUG',
        'sim_path': '',
    }

    HiDishelinkDevice.objects.create(
        device_id='+351912329317',
        api_url='http://api.example',
        api_key='secret-key',
        status=HiDishelinkDevice.Status.ACTIVE,
        mqtt_config={'MQTT_BROKER_URL': 'mqtt.test'},
        notes='old notes',
    )

    stats = ensure_hidishelink_device_from_modem(0)

    assert stats['created'] is False
    hid = HiDishelinkDevice.objects.get(pk='+351912329317')
    assert hid.api_key == 'secret-key'
    assert hid.status == HiDishelinkDevice.Status.ACTIVE
    assert hid.mqtt_config == {'MQTT_BROKER_URL': 'mqtt.test'}
    assert 'Auto-detected modem' in hid.notes


@pytest.mark.django_db
@patch('apps.external_device.hidishelink_bootstrap.probe_modem_identity')
def test_ensure_hidishelink_device_skips_without_phone(mock_probe):
    mock_probe.return_value = {
        'modem_index': 0,
        'phone_number': '',
        'manufacturer': '',
        'model': '',
        'imei': '',
        'firmware': '',
        'sim_path': '',
    }

    stats = ensure_hidishelink_device_from_modem(0)

    assert stats['skipped'] is True
    assert HiDishelinkDevice.objects.count() == 0


@pytest.mark.django_db
@patch('apps.external_device.management.commands.ensure_hidishelink_device.ensure_hidishelink_device_from_modem')
def test_ensure_hidishelink_device_command(mock_ensure):
    mock_ensure.return_value = {
        'skipped': False,
        'created': True,
        'device_id': '+351912329317',
        'status': HiDishelinkDevice.Status.UNCONFIGURED,
    }

    call_command('ensure_hidishelink_device', modem_index=0)

    mock_ensure.assert_called_once_with(0, dry_run=False)
