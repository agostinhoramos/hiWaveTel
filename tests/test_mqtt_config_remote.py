"""Tests for mqtt_config_remote (cache-first resolver and fetch functions)."""

from unittest.mock import MagicMock, patch

import pytest
import requests
from django.utils import timezone

from apps.external_device.hidishelink_client import HiDishelinkApiError
from apps.external_device.models import HiDishelinkDevice
from apps.external_device.mqtt_config_remote import (
    fetch_mqtt_config_for_hidishelink_row,
    resolve_mqtt_config_for_hidishelink_row,
)


@pytest.fixture
def hid_device(db):
    """Create a HiDishelinkDevice with cached config."""
    hid = HiDishelinkDevice.objects.create(
        device_id='+351912329317',
        api_url='http://192.168.1.77:5201',
        api_key='test_api_key_123',
        status=HiDishelinkDevice.Status.ACTIVE,
        mqtt_config={
            'MQTT_BROKER_URL': 'mqtt.hidishe.com',
            'MQTT_PORT': 43827,
            'MQTT_USERNAME': 'test_user',
            'MQTT_PASSWORD': 'test_pass',
            'TOPIC_SMS_SEND': 'hidishelink_dev/devices/{device_id}/sms/send',
        },
        mqtt_config_fetched_at=timezone.now(),
    )
    return hid


@pytest.fixture
def hid_device_no_cache(db):
    """Create a HiDishelinkDevice without cached config."""
    hid = HiDishelinkDevice.objects.create(
        device_id='+351913000100',
        api_url='http://192.168.1.77:5201',
        api_key='test_api_key_456',
        status=HiDishelinkDevice.Status.ACTIVE,
        mqtt_config=None,
        mqtt_config_fetched_at=None,
    )
    return hid


class TestResolveMqttConfig:
    """Tests for resolve_mqtt_config_for_hidishelink_row cache-first resolver."""

    def test_cache_hit_when_refresh_false(self, hid_device, caplog):
        """With refresh=False and valid cache, returns cache without HTTP call."""
        with patch('apps.external_device.mqtt_config_remote.fetch_mqtt_config_for_hidishelink_row') as mock_fetch:
            cfg, source = resolve_mqtt_config_for_hidishelink_row(hid_device, refresh=False)
            
            assert source == 'cache'
            assert cfg['MQTT_BROKER_URL'] == 'mqtt.hidishe.com'
            mock_fetch.assert_not_called()
            assert 'Using cached mqtt-config' in caplog.text
            assert hid_device.device_id in caplog.text

    def test_remote_fetch_when_refresh_true(self, hid_device):
        """With refresh=True, attempts remote fetch even with cache."""
        mock_client = MagicMock()
        mock_client.get_mqtt_config.return_value = {
            'success': True,
            'data': {
                'MQTT_BROKER_URL': 'new.broker.com',
                'MQTT_PORT': 1883,
            },
        }
        
        with patch('apps.external_device.mqtt_config_remote.HiDishelinkApiClient', return_value=mock_client):
            cfg, source = resolve_mqtt_config_for_hidishelink_row(hid_device, refresh=True)
            
            assert source == 'remote'
            assert cfg['MQTT_BROKER_URL'] == 'new.broker.com'
            mock_client.get_mqtt_config.assert_called_once()

    def test_remote_fetch_when_no_cache(self, hid_device_no_cache):
        """Without cache, attempts remote fetch regardless of refresh flag."""
        mock_client = MagicMock()
        mock_client.get_mqtt_config.return_value = {
            'success': True,
            'data': {
                'MQTT_BROKER_URL': 'mqtt.hidishe.com',
                'MQTT_PORT': 1883,
            },
        }
        
        with patch('apps.external_device.mqtt_config_remote.HiDishelinkApiClient', return_value=mock_client):
            cfg, source = resolve_mqtt_config_for_hidishelink_row(hid_device_no_cache, refresh=False)
            
            assert source == 'remote'
            assert cfg['MQTT_BROKER_URL'] == 'mqtt.hidishe.com'
            mock_client.get_mqtt_config.assert_called_once()

    def test_fallback_to_cache_on_transport_error(self, hid_device, caplog):
        """When remote fetch fails and cache exists, returns cache with INFO log (no traceback)."""
        mock_client = MagicMock()
        mock_client.get_mqtt_config.side_effect = requests.ConnectionError('Connection refused')
        
        with patch('apps.external_device.mqtt_config_remote.HiDishelinkApiClient', return_value=mock_client):
            cfg, source = resolve_mqtt_config_for_hidishelink_row(hid_device, refresh=True)
            
            assert source == 'cache'
            assert cfg['MQTT_BROKER_URL'] == 'mqtt.hidishe.com'
            assert 'Remote mqtt-config fetch failed' in caplog.text
            assert 'using cached snapshot' in caplog.text
            # Verify no WARNING with exc_info (traceback)
            assert 'transport error' not in caplog.text

    def test_raises_when_no_cache_and_transport_error(self, hid_device_no_cache):
        """When remote fetch fails and no cache exists, raises HiDishelinkApiError."""
        mock_client = MagicMock()
        mock_client.get_mqtt_config.side_effect = requests.ConnectionError('Connection refused')
        
        with patch('apps.external_device.mqtt_config_remote.HiDishelinkApiClient', return_value=mock_client):
            with pytest.raises(HiDishelinkApiError, match='hiDisheLink unreachable'):
                resolve_mqtt_config_for_hidishelink_row(hid_device_no_cache, refresh=False)


class TestFetchMqttConfig:
    """Tests for fetch_mqtt_config_for_hidishelink_row direct fetch function."""

    def test_successful_fetch_updates_cache(self, hid_device):
        """Successful fetch persists mqtt_config and mqtt_config_fetched_at."""
        mock_client = MagicMock()
        mock_client.get_mqtt_config.return_value = {
            'success': True,
            'data': {
                'MQTT_BROKER_URL': 'updated.broker.com',
                'MQTT_PORT': 8883,
            },
        }
        
        with patch('apps.external_device.mqtt_config_remote.HiDishelinkApiClient', return_value=mock_client):
            cfg = fetch_mqtt_config_for_hidishelink_row(hid_device)
            
            assert cfg['MQTT_BROKER_URL'] == 'updated.broker.com'
            hid_device.refresh_from_db()
            assert hid_device.mqtt_config['MQTT_BROKER_URL'] == 'updated.broker.com'
            assert hid_device.mqtt_config_fetched_at is not None

    def test_transport_error_with_logging_enabled(self, hid_device, caplog):
        """Transport error with log_transport_error=True logs WARNING with traceback."""
        mock_client = MagicMock()
        mock_client.get_mqtt_config.side_effect = requests.ConnectionError('Connection refused')
        
        with patch('apps.external_device.mqtt_config_remote.HiDishelinkApiClient', return_value=mock_client):
            with pytest.raises(HiDishelinkApiError):
                fetch_mqtt_config_for_hidishelink_row(hid_device, log_transport_error=True)
            
            assert 'transport error' in caplog.text

    def test_transport_error_with_logging_disabled(self, hid_device, caplog):
        """Transport error with log_transport_error=False suppresses detailed logging."""
        mock_client = MagicMock()
        mock_client.get_mqtt_config.side_effect = requests.ConnectionError('Connection refused')
        
        with patch('apps.external_device.mqtt_config_remote.HiDishelinkApiClient', return_value=mock_client):
            with pytest.raises(HiDishelinkApiError):
                fetch_mqtt_config_for_hidishelink_row(hid_device, log_transport_error=False)
            
            # Verify no WARNING log from fetch function
            assert 'transport error' not in caplog.text

    def test_raises_when_missing_credentials(self, db):
        """Raises error when api_url or api_key is missing."""
        hid_no_creds = HiDishelinkDevice.objects.create(
            device_id='+351913000200',
            api_url='',
            api_key='',
            status=HiDishelinkDevice.Status.ACTIVE,
        )
        
        with pytest.raises(HiDishelinkApiError, match='missing api_url or api_key'):
            fetch_mqtt_config_for_hidishelink_row(hid_no_creds)

    def test_raises_when_empty_data(self, hid_device):
        """Raises error when remote returns empty data dict."""
        mock_client = MagicMock()
        mock_client.get_mqtt_config.return_value = {'success': True, 'data': {}}
        
        with patch('apps.external_device.mqtt_config_remote.HiDishelinkApiClient', return_value=mock_client):
            with pytest.raises(HiDishelinkApiError, match='returned empty data'):
                fetch_mqtt_config_for_hidishelink_row(hid_device)
