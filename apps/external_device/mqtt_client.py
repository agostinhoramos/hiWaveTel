"""MQTT client for external device gateway: subscribe to status/inbox, publish send requests/ACKs."""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import uuid
from typing import TYPE_CHECKING, Any

import paho.mqtt.client as mqtt
from django.conf import settings

from .models import ExternalDevice
from .services import persist_inbox_from_mqtt, update_request_from_mqtt_status

if TYPE_CHECKING:
    from paho.mqtt.client import MQTTMessage

_LOGGER = logging.getLogger(__name__)


def modem_index_from_status_request_topic(topic: str) -> int | None:
    """Parse modem index from ``{prefix}/modems/{N}/status/request``."""
    parts = topic.split('/')
    try:
        i = parts.index('modems')
        return int(parts[i + 1])
    except (ValueError, IndexError, TypeError):
        return None


def build_modem_status_mqtt_payload(modem_index: int) -> dict[str, Any]:
    """Run ``mmcli`` and return a JSON-serializable modem status dict (for tests and MQTT)."""
    from django.utils import timezone

    from apps.sms.mmcli_client import MMCLIClient, MmcliError

    timeout = float(getattr(settings, 'MQTT_MODEM_STATUS_COMMAND_TIMEOUT_SEC', 45.0))
    try:
        mm = MMCLIClient(timeout_sec=timeout)
        flat = mm.show_modem(modem_index)
        return {
            'modem_index': modem_index,
            'gathered_at': timezone.now().isoformat(),
            'mmcli_flat': dict(flat),
            'success': True,
            'error': None,
        }
    except MmcliError as exc:
        return {
            'modem_index': modem_index,
            'gathered_at': timezone.now().isoformat(),
            'mmcli_flat': {},
            'success': False,
            'error': str(exc)[:2000],
        }


def modem_mmcli_flat_fingerprint(mmcli_flat: dict[str, Any]) -> str:
    """SHA-256 of canonical JSON for comparing modem flat snapshots."""
    canon = json.dumps(mmcli_flat, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(canon.encode('utf-8')).hexdigest()


def sanitize_device_id(device_id: str) -> str:
    """Remove characters not allowed in MQTT topics (+ and #)."""
    return device_id.replace('+', '').replace('#', '')


class GatewayMqttClient:
    """MQTT client for hiWaveTel external device gateway.

    Subscribes to:
    - ``{prefix}/devices/+/sms/status`` — external devices publish SMS job status
    - ``{prefix}/devices/+/sms/inbox`` — external devices publish inbound SMS
    - ``{prefix}/modems/+/status/request`` — request full modem snapshot (mmcli), optional

    Publishes:
    - ``{prefix}/devices/.../sms/send`` and ``.../inbox/ack``
    - ``{prefix}/modems/N/status/telemetry`` — unsolicited modem snapshots (bootstrap / ``state_change``)
    - ``{prefix}/modems/N/status/response`` — modem snapshot (MQTT request reply over ephemeral publisher)
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
        self.subscribe_modem_status = getattr(settings, 'MQTT_MODEM_STATUS_SUBSCRIBE', True)
        self.auto_publish_modem_status = getattr(settings, 'MQTT_MODEM_STATUS_AUTO_PUBLISH', True)
        self.modem_status_poll_interval_sec = float(
            getattr(settings, 'MQTT_MODEM_STATUS_POLL_INTERVAL_SEC', 30.0)
        )
        self._modem_push_stop = threading.Event()
        self._modem_status_fingerprints: dict[int, str] = {}
        self._modem_status_fp_lock = threading.Lock()

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
        self._modem_push_stop.set()
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
            self._modem_push_stop.clear()
            if self.auto_publish_modem_status:
                threading.Thread(
                    target=self._modem_status_auto_publish_runner,
                    daemon=True,
                    name='mqtt-modem-push',
                ).start()
        else:
            _LOGGER.error('Failed to connect to MQTT broker: rc=%d', rc)

    def _on_disconnect(self, client: mqtt.Client, userdata: Any, rc: int) -> None:
        """Callback when disconnected from broker."""
        self._modem_push_stop.set()
        if rc == 0:
            _LOGGER.info('Disconnected from MQTT broker gracefully')
        else:
            _LOGGER.warning('Disconnected from MQTT broker unexpectedly: rc=%d', rc)

    def _subscribe_to_topics(self) -> None:
        """Subscribe to wildcard topics for all devices and optional modem status."""
        status_topic = f'{self.topic_prefix}/devices/+/sms/status'
        inbox_topic = f'{self.topic_prefix}/devices/+/sms/inbox'

        self.client.subscribe(status_topic, qos=self.qos)
        self.client.subscribe(inbox_topic, qos=self.qos)

        subs = [status_topic, inbox_topic]
        if self.subscribe_modem_status:
            modem_rq = f'{self.topic_prefix}/modems/+/status/request'
            self.client.subscribe(modem_rq, qos=self.qos)
            subs.append(modem_rq)

        _LOGGER.info('Subscribed to: %s', ', '.join(subs))

    def _on_message(self, client: mqtt.Client, userdata: Any, msg: MQTTMessage) -> None:
        """Callback when message received."""
        topic = msg.topic
        payload_str = msg.payload.decode('utf-8')

        _LOGGER.debug('Received message on %s: %s', topic, payload_str)

        payload: dict[str, Any]
        if not payload_str.strip():
            payload = {}
        else:
            try:
                raw = json.loads(payload_str)
                payload = raw if isinstance(raw, dict) else {}
            except json.JSONDecodeError:
                _LOGGER.warning('Invalid JSON payload on %s: %s', topic, payload_str)
                return

        if topic.endswith('/status/request') and '/modems/' in topic:
            if self.subscribe_modem_status:
                self._schedule_modem_status_snapshot(topic)
            return

        if topic.endswith('/sms/status'):
            self._handle_status_message(topic, payload)
        elif topic.endswith('/sms/inbox'):
            self._handle_inbox_message(topic, payload)
        else:
            _LOGGER.warning('Unknown topic pattern: %s', topic)

    def _schedule_modem_status_snapshot(self, request_topic: str) -> None:
        """Run ``mmcli`` off the MQTT thread and publish ``.../status/response``."""
        modem_idx = modem_index_from_status_request_topic(request_topic)
        if modem_idx is None:
            _LOGGER.warning('Cannot parse modem index from topic %s', request_topic)
            return

        def worker() -> None:
            from django.utils import timezone

            try:
                body = build_modem_status_mqtt_payload(modem_idx)
            except Exception as exc:  # noqa: BLE001 — never fail the thread silently
                body = {
                    'modem_index': modem_idx,
                    'gathered_at': timezone.now().isoformat(),
                    'mmcli_flat': {},
                    'success': False,
                    'error': str(exc)[:2000],
                }
            resp_topic = f'{self.topic_prefix}/modems/{modem_idx}/status/response'
            _publish_json_ephemeral(resp_topic, body)

        threading.Thread(target=worker, daemon=True).start()

    def _modem_status_auto_publish_runner(self) -> None:
        """Bootstrap telemetry after connect/reconnect; optional polling for state deltas."""
        try:
            self._modem_status_publish_all(event='bootstrap')
        except Exception:
            _LOGGER.warning('modem status bootstrap aborted', exc_info=True)

        interval = self.modem_status_poll_interval_sec
        if interval <= 0:
            return

        while not self._modem_push_stop.is_set():
            if self._modem_push_stop.wait(timeout=interval):
                break
            try:
                self._modem_status_poll_tick()
            except Exception:
                _LOGGER.exception('modem status poll tick failed')

    def _modem_status_list_indices(self) -> list[int]:
        from apps.sms.mmcli_client import MMCLIClient

        timeout = float(getattr(settings, 'MQTT_MODEM_STATUS_COMMAND_TIMEOUT_SEC', 45.0))
        mm = MMCLIClient(timeout_sec=timeout)
        return mm.list_modem_indices()

    def _modem_status_publish_all(self, *, event: str) -> None:
        from apps.sms.mmcli_client import MmcliError

        try:
            indices = self._modem_status_list_indices()
        except MmcliError as exc:
            _LOGGER.warning('modem status event=%s: list_modem_indices failed: %s', event, exc)
            return

        for idx in indices:
            if self._modem_push_stop.is_set():
                return
            body = build_modem_status_mqtt_payload(idx)
            fp = modem_mmcli_flat_fingerprint(body.get('mmcli_flat') or {})
            telemetry = dict(body)
            telemetry['event'] = event
            self._modem_status_publish_telemetry(idx, telemetry)
            with self._modem_status_fp_lock:
                self._modem_status_fingerprints[idx] = fp

    def _modem_status_publish_telemetry(self, modem_idx: int, body: dict[str, Any]) -> None:
        topic = f'{self.topic_prefix}/modems/{modem_idx}/status/telemetry'
        serialized = json.dumps(body, ensure_ascii=False, default=str)
        self.client.publish(topic, serialized, qos=self.qos)
        flat = body.get('mmcli_flat') or {}
        fp = modem_mmcli_flat_fingerprint(flat) if body.get('success') else ''
        fp_log = fp[:16] if fp else 'n/a'
        _LOGGER.info(
            'MQTT modem telemetry modem_index=%s event=%s success=%s bytes=%s fingerprint=%s...',
            modem_idx,
            body.get('event'),
            body.get('success'),
            len(serialized),
            fp_log,
        )

    def _modem_status_poll_tick(self) -> None:
        from apps.sms.mmcli_client import MmcliError

        try:
            indices = self._modem_status_list_indices()
        except MmcliError as exc:
            _LOGGER.warning('modem status poll: list_modem_indices failed: %s', exc)
            return

        for idx in indices:
            if self._modem_push_stop.is_set():
                return
            body = build_modem_status_mqtt_payload(idx)
            fp = modem_mmcli_flat_fingerprint(body.get('mmcli_flat') or {})

            publish = False
            with self._modem_status_fp_lock:
                prev = self._modem_status_fingerprints.get(idx)
                if fp != prev:
                    self._modem_status_fingerprints[idx] = fp
                    publish = True

            if publish:
                telemetry = dict(body)
                telemetry['event'] = 'state_change'
                self._modem_status_publish_telemetry(idx, telemetry)

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


def _mqtt_short_client_id() -> str:
    """MQTT 3.1.1 restricts client identifiers to max 23 bytes."""
    cid = f'hw{uuid.uuid4().hex}'[:23]
    return cid


def _publish_json_ephemeral(topic: str, payload: dict[str, Any]) -> None:
    """Connect, publish JSON once, disconnect (each Gunicorn worker uses a unique client_id)."""
    broker = settings.MQTT_BROKER_URL
    port = settings.MQTT_PORT
    qos = settings.MQTT_QOS
    timeout = getattr(settings, 'MQTT_EPHEMERAL_PUBLISH_TIMEOUT_SEC', 15.0)

    client = mqtt.Client(
        client_id=_mqtt_short_client_id(),
        clean_session=True,
        protocol=mqtt.MQTTv311,
    )
    username = settings.MQTT_USER
    password = settings.MQTT_PASS
    if username and password:
        client.username_pw_set(username, password)
    if port == 8883:
        client.tls_set()

    try:
        client.connect(broker, port, settings.MQTT_KEEPALIVE)
        client.loop_start()
        body = json.dumps(payload, ensure_ascii=False)
        info = client.publish(topic, body, qos=qos)
        info.wait_for_publish(timeout=timeout)
        _LOGGER.info('MQTT ephemeral publish ok topic=%s bytes=%s', topic, len(body))
    except Exception:
        _LOGGER.warning('MQTT ephemeral publish failed topic=%s', topic, exc_info=True)
    finally:
        try:
            client.loop_stop()
        except Exception:
            pass
        try:
            client.disconnect()
        except Exception:
            pass


def publish_send_request_ephemeral(device_id: str, payload: dict[str, Any]) -> None:
    """Publish outbound SMS API request payload to `{prefix}/devices/.../sms/send`."""
    sanitized = sanitize_device_id(device_id)
    topic = f'{settings.MQTT_EXTERNAL_TOPIC_PREFIX}/devices/{sanitized}/sms/send'
    _publish_json_ephemeral(topic, payload)


def publish_modem_inbox_delivery_ephemeral(device_id: str, payload: dict[str, Any]) -> None:
    """Publish modem-mirrored inbox row to `{prefix}/devices/.../sms/inbox_delivery` (gateway → subscribers)."""
    sanitized = sanitize_device_id(device_id)
    topic = f'{settings.MQTT_EXTERNAL_TOPIC_PREFIX}/devices/{sanitized}/sms/inbox_delivery'
    _publish_json_ephemeral(topic, payload)


def publish_modem_inbox_broadcast_ephemeral(modem_index: int, payload: dict[str, Any]) -> None:
    """Publish one canonical inbox delivery per modem on ``{prefix}/modems/N/sms/inbox_delivery``."""
    topic = f'{settings.MQTT_EXTERNAL_TOPIC_PREFIX}/modems/{modem_index}/sms/inbox_delivery'
    _publish_json_ephemeral(topic, payload)
