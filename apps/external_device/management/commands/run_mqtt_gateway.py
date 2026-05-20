"""Django management command to run the MQTT gateway client."""

import threading

from django.conf import settings
from django.core.management.base import BaseCommand

from apps.external_device.hidishelink_client import HiDishelinkApiError
from apps.external_device.models import HiDishelinkDevice
from apps.external_device.mqtt_client import LocalGatewayClient, RemoteHiDishelinkClient
from apps.external_device.mqtt_config_remote import fetch_mqtt_config_for_hidishelink_row


class Command(BaseCommand):
    help = 'Run the MQTT gateway client loop (dual-client: remote hiDisheLink + local broker).'

    def handle(self, *args, **options):  # type: ignore[override]
        """Start MQTT gateway clients (remote bridge + local gateway)."""
        remote_enabled = getattr(settings, 'MQTT_REMOTE_BRIDGE_ENABLED', True)
        local_enabled = getattr(settings, 'MQTT_LOCAL_BROKER_ENABLED', True)
        
        if not remote_enabled and not local_enabled:
            self.stdout.write(
                self.style.WARNING(
                    'Both MQTT_REMOTE_BRIDGE_ENABLED and MQTT_LOCAL_BROKER_ENABLED are disabled. '
                    'Nothing to do.'
                )
            )
            return
        
        # Start remote hiDisheLink bridge client
        remote_thread = None
        if remote_enabled:
            remote_thread = self._start_remote_client()
        
        # Start local gateway client (for Android devices)
        local_thread = None
        if local_enabled:
            local_thread = self._start_local_client()
        
        # Wait for threads
        self.stdout.write(self.style.SUCCESS('MQTT gateway clients running...'))
        
        if remote_thread:
            remote_thread.join()
        if local_thread:
            local_thread.join()
    
    def _start_remote_client(self):
        """Start RemoteHiDishelinkClient thread."""
        # Determine device_id
        device_id = getattr(settings, 'MQTT_REMOTE_DEVICE_ID', '').strip()
        
        if not device_id:
            # Try to get from first active HiDishelinkDevice
            hid_row = (
                HiDishelinkDevice.objects.filter(status=HiDishelinkDevice.Status.ACTIVE)
                .order_by('-mqtt_config_fetched_at', '-updated_at')
                .first()
            )
            if hid_row:
                device_id = hid_row.device_id
            else:
                self.stdout.write(
                    self.style.WARNING(
                        'Remote bridge: no MQTT_REMOTE_DEVICE_ID and no active HiDishelinkDevice. '
                        'Remote client disabled.'
                    )
                )
                return None
        
        # Fetch mqtt-config
        mqtt_cfg = None
        hid_cred = (
            HiDishelinkDevice.objects.filter(
                device_id=device_id,
                status=HiDishelinkDevice.Status.ACTIVE,
            )
            .exclude(api_url='')
            .exclude(api_key='')
            .first()
        )
        
        if hid_cred:
            try:
                mqtt_cfg = fetch_mqtt_config_for_hidishelink_row(hid_cred)
                self.stdout.write(
                    self.style.SUCCESS(
                        f'Remote bridge: fetched MQTT config from hiDisheLink API (device {device_id}).'
                    )
                )
            except HiDishelinkApiError as exc:
                self.stderr.write(
                    self.style.WARNING(f'Remote bridge: mqtt-config fetch failed ({exc}); trying cached.')
                )
                mqtt_cfg = hid_cred.mqtt_config if isinstance(hid_cred.mqtt_config, dict) else None
        
        if mqtt_cfg is None:
            # Try cached config from any active device with same device_id
            hid_snap = (
                HiDishelinkDevice.objects.filter(
                    device_id=device_id,
                    status=HiDishelinkDevice.Status.ACTIVE,
                )
                .exclude(mqtt_config=None)
                .first()
            )
            if hid_snap and isinstance(hid_snap.mqtt_config, dict):
                mqtt_cfg = hid_snap.mqtt_config
                self.stdout.write(
                    f'Remote bridge: using cached MQTT config for device {device_id}.'
                )
        
        if mqtt_cfg is None:
            self.stdout.write(
                self.style.WARNING(
                    f'Remote bridge: no mqtt-config available for device {device_id}. '
                    'Remote client disabled. Configure HiDishelinkDevice in admin.'
                )
            )
            return None
        
        # Create and start remote client
        remote_client = RemoteHiDishelinkClient(mqtt_cfg, device_id)
        remote_client.connect()
        
        # Set global instance for signal handlers
        import apps.external_device.mqtt_client as mqtt_mod
        mqtt_mod._global_remote_client = remote_client
        
        def remote_loop():
            try:
                remote_client.loop_forever()
            except KeyboardInterrupt:
                self.stdout.write(self.style.WARNING('Remote bridge shutting down...'))
                remote_client.disconnect()
                mqtt_mod._global_remote_client = None
        
        thread = threading.Thread(target=remote_loop, daemon=False, name='mqtt-remote-bridge')
        thread.start()
        self.stdout.write(
            self.style.SUCCESS(f'Remote hiDisheLink bridge started (device {device_id})')
        )
        return thread
    
    def _start_local_client(self):
        """Start LocalGatewayClient thread (legacy GatewayMqttClient)."""
        # Try to get mqtt-config from any active HiDishelinkDevice for local broker settings
        mqtt_cfg = None
        hid_cred = (
            HiDishelinkDevice.objects.filter(status=HiDishelinkDevice.Status.ACTIVE)
            .exclude(api_url='')
            .exclude(api_key='')
            .order_by('-mqtt_config_fetched_at', '-updated_at')
            .first()
        )
        
        if hid_cred:
            try:
                mqtt_cfg = fetch_mqtt_config_for_hidishelink_row(hid_cred)
                self.stdout.write(
                    self.style.SUCCESS(
                        f'Local gateway: fetched MQTT config from hiDisheLink API (device {hid_cred.device_id}).'
                    )
                )
            except HiDishelinkApiError as exc:
                self.stderr.write(
                    self.style.WARNING(f'Local gateway: mqtt-config fetch failed ({exc}); trying cached.')
                )
                mqtt_cfg = hid_cred.mqtt_config if isinstance(hid_cred.mqtt_config, dict) else None
        
        if mqtt_cfg is None:
            hid_snap = (
                HiDishelinkDevice.objects.filter(status=HiDishelinkDevice.Status.ACTIVE)
                .exclude(mqtt_config=None)
                .order_by('-mqtt_config_fetched_at', '-updated_at')
                .first()
            )
            if hid_snap and isinstance(hid_snap.mqtt_config, dict):
                mqtt_cfg = hid_snap.mqtt_config
                self.stdout.write(
                    f'Local gateway: using cached MQTT config from device {hid_snap.device_id}.'
                )
        
        if mqtt_cfg is None:
            self.stdout.write(
                self.style.WARNING(
                    'Local gateway: using MQTT settings from Django config (no hiDisheLink snapshot).'
                )
            )
        
        # Create and start local client
        client = LocalGatewayClient(mqtt_config=mqtt_cfg)
        client.connect()
        
        def local_loop():
            try:
                client.loop_forever()
            except KeyboardInterrupt:
                self.stdout.write(self.style.WARNING('Local gateway shutting down...'))
                client.disconnect()
        
        thread = threading.Thread(target=local_loop, daemon=False, name='mqtt-local-gateway')
        thread.start()
        self.stdout.write(self.style.SUCCESS('Local MQTT gateway started'))
        return thread
