"""Django management command to run the MQTT gateway client."""

from django.core.management.base import BaseCommand

from apps.external_device.hidishelink_client import HiDishelinkApiError
from apps.external_device.models import HiDishelinkDevice
from apps.external_device.mqtt_client import GatewayMqttClient
from apps.external_device.mqtt_config_remote import fetch_mqtt_config_for_hidishelink_row


class Command(BaseCommand):
    help = 'Run the MQTT gateway client loop (subscribe/publish for external devices).'

    def handle(self, *args, **options):  # type: ignore[override]
        """Start MQTT gateway client."""
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
                        f'Fetched MQTT config from hiDisheLink API (device {hid_cred.device_id}).'
                    )
                )
            except HiDishelinkApiError as exc:
                self.stderr.write(
                    self.style.WARNING(f'Fresh mqtt-config failed ({exc}); trying cached snapshot.')
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
                    f'Using cached MQTT config from HiDisheLink device {hid_snap.device_id}.'
                )

        if mqtt_cfg is None:
            self.stdout.write(
                self.style.WARNING(
                    'Using MQTT settings from Django config (no hiDisheLink MQTT snapshot or fetch).'
                )
            )

        client = GatewayMqttClient(mqtt_config=mqtt_cfg)
        client.connect()
        self.stdout.write(self.style.SUCCESS('MQTT gateway client running...'))
        try:
            client.loop_forever()
        except KeyboardInterrupt:
            self.stdout.write(self.style.WARNING('Shutting down MQTT gateway...'))
            client.disconnect()
