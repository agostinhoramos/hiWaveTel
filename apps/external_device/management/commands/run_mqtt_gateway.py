"""Django management command to run the MQTT gateway client."""

from django.core.management.base import BaseCommand

from apps.external_device.mqtt_client import GatewayMqttClient


class Command(BaseCommand):
    help = 'Run the MQTT gateway client loop (subscribe/publish for external devices).'

    def handle(self, *args, **options):  # type: ignore[override]
        """Start MQTT gateway client."""
        client = GatewayMqttClient()
        client.connect()
        self.stdout.write(self.style.SUCCESS('MQTT gateway client running...'))
        try:
            client.loop_forever()
        except KeyboardInterrupt:
            self.stdout.write(self.style.WARNING('Shutting down MQTT gateway...'))
            client.disconnect()
