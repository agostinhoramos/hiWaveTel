"""MQTT client for external device gateway: subscribe to status/inbox, publish send requests/ACKs."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

import paho.mqtt.client as mqtt
from django.conf import settings

from .models import ExternalDevice
from .services import persist_inbox_from_mqtt, update_request_from_mqtt_status

if TYPE_CHECKING:
    from paho.mqtt.client import MQTTMessage

_LOGGER = logging.getLogger(__name__)


def sanitize_device_id(device_id: str) -> str:
    """Remove characters not allowed in MQTT topics (+ and #)."""
    return device_id.replace('+', '').replace('#', '')


class GatewayMqttClient:
    """MQTT client for hiWaveTel external device gateway.
    
    Subscribes to:
    - {prefix}/devices/+/sms/status (external devices publish status updates)
    - {prefix}/devices/+/sms/inbox (external devices publish incoming SMS)
    
    Publishes to:
    - {prefix}/devices/{device_id}/sms/send (gateway sends SMS requests to devices)
    - {prefix}/devices/{device_id}/sms/inbox/ack (gateway acknowledges inbox messages)
    """

    def __init__(self):
        """Initialize MQTT client with settings from Django config."""
        self.broker_url = settings.MQTT_BROKER_URL
        self.port = settings.MQTT_PORT
        self.username = settings.MQTT_USER
        self.password = settings.MQTT_PASS
        self.client_id = settings.MQTT_CLIENT_ID
        self.keepalive = settings.MQTT_KEEPALIVE
        self.qos = settings.MQTT_QOS
        self.clean_session = settings.MQTT_CLEAN_SESSION
        self.topic_prefix = settings.MQTT_EXTERNAL_TOPIC_PREFIX

        self.client = mqtt.Client(
            client_id=self.client_id,
            clean_session=self.clean_session,
            protocol=mqtt.MQTTv311,
        )

        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.client.on_disconnect = self._on_disconnect

        if self.username and self.password:
            self.client.username_pw_set(self.username, self.password)

        if self.port == 8883:
            self.client.tls_set()

    def connect(self) -> None:
        """Connect to MQTT broker."""
        _LOGGER.info('Connecting to MQTT broker %s:%d...', self.broker_url, self.port)
        self.client.connect(self.broker_url, self.port, self.keepalive)

    def loop_forever(self) -> None:
        """Start MQTT client loop (blocking)."""
        _LOGGER.info('Starting MQTT client loop...')
        self.client.loop_forever()

    def loop_start(self) -> None:
        """Start MQTT client loop in background thread."""
        _LOGGER.info('Starting MQTT client background loop...')
        self.client.loop_start()

    def loop_stop(self) -> None:
        """Stop MQTT client background loop."""
        _LOGGER.info('Stopping MQTT client loop...')
        self.client.loop_stop()

    def disconnect(self) -> None:
        """Disconnect from MQTT broker."""
        _LOGGER.info('Disconnecting from MQTT broker...')
        self.client.disconnect()

    def publish_send_request(self, device_id: str, payload: dict[str, Any]) -> None:
        """Publish SMS send request to external device.
        
        Topic: {prefix}/devices/{sanitized_device_id}/sms/send
        """
        sanitized_id = sanitize_device_id(device_id)
        topic = f'{self.topic_prefix}/devices/{sanitized_id}/sms/send'
        message = json.dumps(payload)
        self.client.publish(topic, message, qos=self.qos)
        _LOGGER.info('Published send request to %s: request_id=%s', topic, payload.get('request_id'))

    def publish_inbox_ack(self, device_id: str, message_id: str) -> None:
        """Publish inbox ACK to external device.
        
        Topic: {prefix}/devices/{sanitized_device_id}/sms/inbox/ack
        """
        sanitized_id = sanitize_device_id(device_id)
        topic = f'{self.topic_prefix}/devices/{sanitized_id}/sms/inbox/ack'
        payload = {'message_id': message_id}
        message = json.dumps(payload)
        self.client.publish(topic, message, qos=self.qos)
        _LOGGER.info('Published inbox ACK to %s: message_id=%s', topic, message_id)

    def _on_connect(self, client: mqtt.Client, userdata: Any, flags: dict, rc: int) -> None:
        """Callback when connected to broker."""
        if rc == 0:
            _LOGGER.info('Connected to MQTT broker successfully')
            self._subscribe_to_topics()
        else:
            _LOGGER.error('Failed to connect to MQTT broker: rc=%d', rc)

    def _on_disconnect(self, client: mqtt.Client, userdata: Any, rc: int) -> None:
        """Callback when disconnected from broker."""
        if rc == 0:
            _LOGGER.info('Disconnected from MQTT broker gracefully')
        else:
            _LOGGER.warning('Disconnected from MQTT broker unexpectedly: rc=%d', rc)

    def _subscribe_to_topics(self) -> None:
        """Subscribe to wildcard topics for all devices."""
        status_topic = f'{self.topic_prefix}/devices/+/sms/status'
        inbox_topic = f'{self.topic_prefix}/devices/+/sms/inbox'

        self.client.subscribe(status_topic, qos=self.qos)
        self.client.subscribe(inbox_topic, qos=self.qos)

        _LOGGER.info('Subscribed to: %s, %s', status_topic, inbox_topic)

    def _on_message(self, client: mqtt.Client, userdata: Any, msg: MQTTMessage) -> None:
        """Callback when message received."""
        topic = msg.topic
        payload_str = msg.payload.decode('utf-8')

        _LOGGER.debug('Received message on %s: %s', topic, payload_str)

        try:
            payload = json.loads(payload_str)
        except json.JSONDecodeError:
            _LOGGER.warning('Invalid JSON payload on %s: %s', topic, payload_str)
            return

        if topic.endswith('/sms/status'):
            self._handle_status_message(topic, payload)
        elif topic.endswith('/sms/inbox'):
            self._handle_inbox_message(topic, payload)
        else:
            _LOGGER.warning('Unknown topic pattern: %s', topic)

    def _handle_status_message(self, topic: str, payload: dict[str, Any]) -> None:
        """Handle SMS status update from external device."""
        request_id = payload.get('request_id', '').strip()
        if not request_id:
            _LOGGER.warning('Status message missing request_id: %s', payload)
            return

        try:
            update_request_from_mqtt_status(request_id, payload)
        except Exception as exc:
            _LOGGER.exception('Error handling status message for request_id=%s: %s', request_id, exc)

    def _handle_inbox_message(self, topic: str, payload: dict[str, Any]) -> None:
        """Handle incoming SMS from external device."""
        device_id_sanitized = self._extract_device_id_from_topic(topic)
        if not device_id_sanitized:
            _LOGGER.warning('Cannot extract device_id from topic: %s', topic)
            return

        device_id = self._find_device_id_by_sanitized(device_id_sanitized)
        if not device_id:
            _LOGGER.warning('No device found with sanitized ID: %s', device_id_sanitized)
            return

        try:
            device = ExternalDevice.objects.get(device_id=device_id)
        except ExternalDevice.DoesNotExist:
            _LOGGER.warning('Device %s not found', device_id)
            return

        try:
            inbox_msg = persist_inbox_from_mqtt(device, payload)
            
            if not inbox_msg.ack_sent:
                self.publish_inbox_ack(device.device_id, inbox_msg.message_id)
                inbox_msg.ack_sent = True
                inbox_msg.save(update_fields=['ack_sent'])
        except Exception as exc:
            _LOGGER.exception('Error handling inbox message from device=%s: %s', device_id, exc)

    def _extract_device_id_from_topic(self, topic: str) -> str | None:
        """Extract device_id from topic like {prefix}/devices/{device_id}/sms/inbox."""
        parts = topic.split('/')
        try:
            devices_idx = parts.index('devices')
            return parts[devices_idx + 1]
        except (ValueError, IndexError):
            return None

    def _find_device_id_by_sanitized(self, sanitized_id: str) -> str | None:
        """Find original device_id by matching sanitized version."""
        devices = ExternalDevice.objects.all()
        for device in devices:
            if sanitize_device_id(device.device_id) == sanitized_id:
                return device.device_id
        return None
