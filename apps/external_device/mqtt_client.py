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

from .models import DeviceHealthTelemetry, ExternalDevice, HiDishelinkDevice
from .services import persist_inbox_from_mqtt, persist_modem_catalog_from_mqtt, update_request_from_mqtt_status

if TYPE_CHECKING:
    from paho.mqtt.client import MQTTMessage

_LOGGER = logging.getLogger(__name__)


def _paho_client_connected(client: mqtt.Client) -> bool:
    """Return True if the persistent client believes it has a broker session."""
    fn = getattr(client, 'is_connected', None)
    if callable(fn):
        try:
            return bool(fn())
        except Exception:
            return False
    return False


def resolved_mqtt_modem_topic_prefix() -> str:
    """Broker segment for modem/catalog paths (``…/modems/…``). Uses MQTT_BASE_TOPIC_PREFIX then MQTT_EXTERNAL_TOPIC_PREFIX."""
    base = str(getattr(settings, 'MQTT_BASE_TOPIC_PREFIX', '') or '').strip()
    if base:
        return base.rstrip('/')
    return str(getattr(settings, 'MQTT_EXTERNAL_TOPIC_PREFIX', '') or '').strip().rstrip('/')


def resolved_mqtt_device_topic_prefix() -> str:
    """Full prefix before ``/<sanitized_device_id>/…`` (hiDisheLink ``MQTT_DEVICE_TOPIC_PREFIX``).

    When ``MQTT_DEVICE_TOPIC_PREFIX`` is unset in env, derives ``{MQTT_BASE_TOPIC_PREFIX}/devices`` so
    ``override_settings(MQTT_EXTERNAL_TOPIC_PREFIX=…)`` stays coherent in tests.
    """
    explicit = str(getattr(settings, 'MQTT_DEVICE_TOPIC_PREFIX', '') or '').strip()
    if explicit:
        return explicit.rstrip('/')
    base = str(getattr(settings, 'MQTT_BASE_TOPIC_PREFIX', '') or '').strip()
    if not base:
        base = str(getattr(settings, 'MQTT_EXTERNAL_TOPIC_PREFIX', '') or '').strip()
    return f'{base.rstrip("/")}/devices'


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


def topic_wildcard_from_template(template: str | None) -> str | None:
    """Turn hiDisheLink TOPIC_* template into MQTT wildcard subscription."""
    if template and '{device_id}' in template:
        return template.replace('{device_id}', '+')
    return None


def mqtt_topic_prefix_from_flat(cfg: dict[str, Any]) -> str:
    """Device-topic prefix from flat mqtt-config (segment before ``/<sanitized>/…``)."""
    dev = str(cfg.get('MQTT_DEVICE_TOPIC_PREFIX') or '').strip()
    if dev:
        return dev.rstrip('/')
    base_only = str(cfg.get('MQTT_BASE_TOPIC_PREFIX') or '').strip()
    if base_only:
        return f'{base_only.rstrip("/")}/devices'
    return resolved_mqtt_device_topic_prefix()


def device_topic_from_flat_config(
    cfg: dict[str, Any],
    template_key: str,
    legacy_fmt: str,
    device_id: str,
) -> str:
    """Resolve per-device MQTT topic using hiDisheLink ``TOPIC_*`` template or legacy ``legacy_fmt``."""
    sanitized = sanitize_device_id(device_id)
    tpl = cfg.get(template_key)
    if isinstance(tpl, str) and tpl.strip() and '{device_id}' in tpl:
        return tpl.replace('{device_id}', sanitized)
    prefix = mqtt_topic_prefix_from_flat(cfg)
    return legacy_fmt.format(prefix=prefix, sanitized=sanitized)


def build_django_health_ping_payload() -> dict[str, Any]:
    """Active server ping body for gateways/apps subscribed on ``TOPIC_HEALTH_PING`` (hiDisheLink)."""
    from django.utils import timezone

    return {
        'ping_id': f'ping_{uuid.uuid4().hex[:12]}',
        'timestamp': timezone.now().isoformat(),
        'source': 'django',
    }


def mqtt_flat_cfg_for_device_id(device_id: str) -> dict[str, Any]:
    """Load per-device mqtt-config snapshot from ``HiDishelinkDevice`` when present."""
    raw = HiDishelinkDevice.objects.filter(pk=device_id).values_list('mqtt_config', flat=True).first()
    if isinstance(raw, dict):
        return dict(raw)
    return {}


def ephemeral_connection_from_flat(cfg: dict[str, Any] | None) -> dict[str, Any]:
    """Subset of mqtt-config for `_publish_json_ephemeral` one-off publishes."""
    if not cfg:
        return {}
    user = cfg.get('MQTT_USERNAME') or cfg.get('MQTT_USER')
    password = cfg.get('MQTT_PASSWORD') or cfg.get('MQTT_PASS')
    parts = {
        'MQTT_BROKER_URL': cfg.get('MQTT_BROKER_URL'),
        'MQTT_PORT': cfg.get('MQTT_PORT'),
        'MQTT_KEEPALIVE': cfg.get('MQTT_KEEPALIVE'),
        'MQTT_QOS': cfg.get('MQTT_QOS'),
        'MQTT_USER': str(user).strip() if user is not None and str(user).strip() else None,
        'MQTT_PASS': str(password).strip() if password is not None and str(password).strip() else None,
    }
    return {k: v for k, v in parts.items() if v is not None}


class GatewayMqttClient:
    """MQTT client for hiWaveTel external device gateway.

    Subscribes to:
    - ``{device_topic_prefix}/+/sms/status`` — external devices publish SMS job status
    - ``{device_topic_prefix}/+/sms/inbox`` — external devices publish inbound SMS
    - ``{device_topic_prefix}/+/health/ping`` — hiDisheLink health ping (optional); gateway replies on ``…/health/pong``
    - ``{device_topic_prefix}/+/health/pong`` — gateway logs device online when apps reply to Django pings (optional)
    - ``{prefix}/modems/snapshot`` and ``{prefix}/modems/contacts`` — gateway catalog (optional)
    - ``{prefix}/modems/+/status/request`` — request full modem snapshot (mmcli), optional

    Publishes:
    - ``{device_topic_prefix}/…/sms/send`` and ``…/inbox/ack``
    - ``{device_topic_prefix}/…/health/ping`` — periodic Django tipo B pings when ``MQTT_HEALTH_SERVER_PING_INTERVAL_SEC`` > 0
    - ``{device_topic_prefix}/…/health/pong`` — automatic reply to ``health/ping`` when enabled
    - ``{prefix}/modems/N/status/telemetry`` — unsolicited modem snapshots (bootstrap / ``state_change``)
    - ``{prefix}/modems/N/status/response`` — modem snapshot (MQTT request reply over ephemeral publisher)
    """

    def __init__(self, mqtt_config: dict[str, Any] | None = None):
        """Initialize MQTT client from optional flat hiDisheLink ``mqtt-config`` or Django settings."""
        cfg = dict(mqtt_config) if mqtt_config else {}
        self._mqtt_cfg = cfg

        def pick_str(key: str, fallback: str) -> str:
            v = cfg.get(key)
            if v is not None and str(v).strip():
                return str(v).strip()
            return fallback

        def pick_int(key: str, fallback: int) -> int:
            v = cfg.get(key)
            if v is None or (isinstance(v, str) and not v.strip()):
                return fallback
            try:
                return int(v)
            except (TypeError, ValueError):
                return fallback

        def pick_bool(key: str, fallback: bool) -> bool:
            if key not in cfg:
                return fallback
            raw = str(cfg[key]).strip().lower()
            if raw in ('true', '1', 'yes', 'on'):
                return True
            if raw in ('false', '0', 'no', 'off'):
                return False
            return fallback

        self.broker_url = pick_str('MQTT_BROKER_URL', settings.MQTT_BROKER_URL)
        self.port = pick_int('MQTT_PORT', settings.MQTT_PORT)
        self.username = (
            pick_str('MQTT_USERNAME', '')
            or pick_str('MQTT_USER', '')
            or settings.MQTT_USER
        )
        self.password = (
            pick_str('MQTT_PASSWORD', '')
            or pick_str('MQTT_PASS', '')
            or settings.MQTT_PASS
        )
        self.client_id = pick_str('MQTT_CLIENT_ID', settings.MQTT_CLIENT_ID)
        self.keepalive = pick_int('MQTT_KEEPALIVE', settings.MQTT_KEEPALIVE)
        self.qos = pick_int('MQTT_QOS', settings.MQTT_QOS)
        self.clean_session = pick_bool('MQTT_CLEAN_SESSION', settings.MQTT_CLEAN_SESSION)

        dev_cfg = pick_str('MQTT_DEVICE_TOPIC_PREFIX', '')
        self.topic_prefix = dev_cfg.rstrip('/') if dev_cfg else resolved_mqtt_device_topic_prefix()

        base_modem = cfg.get('MQTT_BASE_TOPIC_PREFIX')
        if base_modem is not None and str(base_modem).strip():
            self.modem_topic_prefix = str(base_modem).strip().rstrip('/')
        else:
            self.modem_topic_prefix = resolved_mqtt_modem_topic_prefix()

        self.subscribe_modem_status = getattr(settings, 'MQTT_MODEM_STATUS_SUBSCRIBE', True)
        self.subscribe_modem_catalog = getattr(settings, 'MQTT_SUBSCRIBE_MODEM_CATALOG', True)
        self.subscribe_health_ping = getattr(settings, 'MQTT_HEALTH_AUTO_PONG', True)
        self.subscribe_health_pong = getattr(settings, 'MQTT_HEALTH_SUBSCRIBE_PONG', True)
        self.gateway_auto_pong_django = getattr(settings, 'MQTT_HEALTH_GATEWAY_AUTO_PONG_DJANGO', False)
        hp_qos = int(getattr(settings, 'MQTT_HEALTH_PING_SUBSCRIBE_QOS', 0))
        self.health_ping_subscribe_qos = hp_qos if 0 <= hp_qos <= 2 else 0
        self.auto_publish_modem_status = getattr(settings, 'MQTT_MODEM_STATUS_AUTO_PUBLISH', True)
        self.modem_status_poll_interval_sec = float(
            getattr(settings, 'MQTT_MODEM_STATUS_POLL_INTERVAL_SEC', 30.0)
        )
        _ping_iv = float(getattr(settings, 'MQTT_HEALTH_SERVER_PING_INTERVAL_SEC', 60.0))
        self.health_server_ping_interval_sec = _ping_iv if _ping_iv > 0 else 0.0
        self._mqtt_ping_stop = threading.Event()
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

    def _device_topic_from_template(self, template_key: str, legacy_topic_fmt: str, device_id: str) -> str:
        sanitized = sanitize_device_id(device_id)
        tpl = self._mqtt_cfg.get(template_key)
        if isinstance(tpl, str) and '{device_id}' in tpl:
            return tpl.replace('{device_id}', sanitized)
        return legacy_topic_fmt.format(prefix=self.topic_prefix, sanitized=sanitized)

    def _wildcard_subscribe_topic(self, template_key: str, legacy_wildcard: str) -> str:
        tpl = self._mqtt_cfg.get(template_key)
        w = topic_wildcard_from_template(tpl if isinstance(tpl, str) else None)
        return w if w else legacy_wildcard

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
        self._mqtt_ping_stop.set()
        _LOGGER.info('Disconnecting from MQTT broker...')
        self.client.disconnect()

    def publish_send_request(self, device_id: str, payload: dict[str, Any]) -> None:
        """Publish SMS send request to external device."""
        topic = self._device_topic_from_template(
            'TOPIC_SMS_SEND',
            '{prefix}/{sanitized}/sms/send',
            device_id,
        )
        message = json.dumps(payload)
        self.client.publish(topic, message, qos=self.qos)
        _LOGGER.info('Published send request to %s: request_id=%s', topic, payload.get('request_id'))

    def publish_inbox_ack(self, device_id: str, message_id: str) -> None:
        """Publish inbox ACK to external device."""
        topic = self._device_topic_from_template(
            'TOPIC_SMS_INBOX_ACK',
            '{prefix}/{sanitized}/sms/inbox/ack',
            device_id,
        )
        payload = {'message_id': message_id}
        message = json.dumps(payload)
        self.client.publish(topic, message, qos=self.qos)
        _LOGGER.info('Published inbox ACK to %s: message_id=%s', topic, message_id)

    def _health_ping_subscribe_pattern(self) -> str | None:
        """Wildcard MQTT subscription for inbound health pings (hiDisheLink)."""
        if not self.subscribe_health_ping:
            return None
        return self._wildcard_subscribe_topic(
            'TOPIC_HEALTH_PING',
            f'{self.topic_prefix}/+/health/ping',
        )

    def _health_pong_subscribe_pattern(self) -> str | None:
        """Wildcard subscription for ``health/pong`` (external app replies → ``ExternalDevice.last_seen``)."""
        if not self.subscribe_health_pong:
            return None
        tpl = self._mqtt_cfg.get('TOPIC_HEALTH_PONG')
        wc = topic_wildcard_from_template(tpl if isinstance(tpl, str) else None)
        if wc:
            return wc
        return f'{self.topic_prefix}/+/health/pong'

    def _sanitized_from_health_ping_topic(self, topic: str) -> str | None:
        if not topic.endswith('/health/ping'):
            return None
        base = topic[: -len('/health/ping')]
        seg = base.split('/')[-1]
        return seg if seg else None

    def _sanitized_from_health_pong_topic(self, topic: str) -> str | None:
        if not topic.endswith('/health/pong'):
            return None
        base = topic[: -len('/health/pong')]
        seg = base.split('/')[-1]
        return seg if seg else None

    def _handle_health_pong(self, topic: str, payload: dict[str, Any]) -> None:
        """Mark external device reachable when an app/gateway publishes ``health/pong``."""
        sanitized = self._sanitized_from_health_pong_topic(topic)
        if not sanitized:
            _LOGGER.warning('Cannot parse device segment from health/pong topic: %s', topic)
            return
        device_id = self._find_device_id_by_sanitized(sanitized)
        if not device_id:
            _LOGGER.debug('health/pong ignored unknown sanitized=%s', sanitized)
            return
        try:
            device = ExternalDevice.objects.get(pk=device_id)
        except ExternalDevice.DoesNotExist:
            return
        device.mark_seen()
        _LOGGER.info(
            'MQTT health pong device=%s ping_id=%s',
            device_id,
            str(payload.get('ping_id') or '')[:48],
        )

    def _publish_gateway_health_pong(self, sanitized_segment: str, payload: dict[str, Any]) -> None:
        """Publish gateway-originated ``health/pong`` (legacy echo or lab Django ping handling)."""
        from django.utils import timezone

        pong_topic = self._device_topic_from_template(
            'TOPIC_HEALTH_PONG',
            '{prefix}/{sanitized}/health/pong',
            sanitized_segment,
        )
        reply: dict[str, Any] = {
            'ping_id': str(payload.get('ping_id') or ''),
            'timestamp': timezone.now().isoformat(),
            'source': 'hiwavetel_gateway',
        }
        if payload.get('timestamp') is not None:
            reply['ping_timestamp'] = payload.get('timestamp')

        body = json.dumps(reply, ensure_ascii=False)
        if not _paho_client_connected(self.client):
            _LOGGER.debug('health/pong publish skipped (client disconnected) topic=%s', pong_topic)
            return
        info = self.client.publish(pong_topic, body, qos=self.qos)
        rc_raw = getattr(info, 'rc', mqtt.MQTT_ERR_SUCCESS)
        if isinstance(rc_raw, int) and rc_raw != mqtt.MQTT_ERR_SUCCESS:
            _LOGGER.warning(
                'health/pong publish rejected rc=%s topic=%s',
                rc_raw,
                pong_topic,
            )
            return
        try:
            info.wait_for_publish(timeout=5.0)
        except RuntimeError as exc:
            if 'not currently connected' in str(exc).lower():
                _LOGGER.debug('health/pong publish wait aborted (disconnected) topic=%s', pong_topic)
                return
            raise
        except Exception:
            _LOGGER.debug('health/pong publish wait timeout topic=%s', pong_topic)
            return
        _LOGGER.info('MQTT health pong topic=%s ping_id=%s', pong_topic, reply.get('ping_id'))

    def _persist_health_ping_telemetry(self, sanitized_segment: str, payload: dict[str, Any]) -> None:
        """Store tipo A telemetry from Android ``health/ping`` (battery/network/version)."""
        device_id = self._find_device_id_by_sanitized(sanitized_segment)
        if not device_id:
            _LOGGER.debug('health/ping telemetry ignored unknown sanitized=%s', sanitized_segment)
            return
        try:
            device = ExternalDevice.objects.get(pk=device_id)
        except ExternalDevice.DoesNotExist:
            return

        bl_raw = payload.get('battery_level')
        battery: int | None
        try:
            battery = int(bl_raw) if bl_raw is not None else None
        except (TypeError, ValueError):
            battery = None

        DeviceHealthTelemetry.objects.create(
            device=device,
            timestamp_app=str(payload.get('timestamp') or '')[:64],
            app_version=str(payload.get('app_version') or '')[:128],
            battery_level=battery,
            network_type=str(payload.get('network_type') or '')[:32],
            raw_payload=dict(payload),
        )
        device.mark_seen()

    def _handle_health_ping(self, topic: str, payload: dict[str, Any]) -> None:
        """Handle ``health/ping``: Android telemetry (tipo A), Django latency ping (tipo B), or legacy echo."""
        src = str(payload.get('source') or '').strip().lower()
        if src == 'hiwavetel_gateway':
            return

        sanitized = self._sanitized_from_health_ping_topic(topic)
        if not sanitized:
            _LOGGER.warning('Cannot parse device segment from health/ping topic: %s', topic)
            return

        src_django_exact = payload.get('source') == 'django'

        is_telemetry = not src_django_exact and (
            'battery_level' in payload or 'network_type' in payload
        )
        if is_telemetry:
            try:
                self._persist_health_ping_telemetry(sanitized, payload)
            except Exception:
                _LOGGER.exception('health/ping telemetry persist failed sanitized=%s', sanitized)
            return

        if src_django_exact:
            if self.gateway_auto_pong_django:
                self._publish_gateway_health_pong(sanitized, payload)
            else:
                _LOGGER.debug(
                    'health/ping django ping_id=%s (gateway auto-pong disabled; Android responds)',
                    str(payload.get('ping_id') or '')[:48],
                )
            return

        if payload.get('ping_id') and getattr(settings, 'MQTT_HEALTH_AUTO_PONG', True):
            self._publish_gateway_health_pong(sanitized, payload)

    def _on_connect(self, client: mqtt.Client, userdata: Any, flags: dict, rc: int) -> None:
        """Callback when connected to broker."""
        if rc == 0:
            _LOGGER.info('Connected to MQTT broker successfully')
            self._subscribe_to_topics()
            self._modem_push_stop.clear()
            self._mqtt_ping_stop.clear()
            if self.auto_publish_modem_status:
                threading.Thread(
                    target=self._modem_status_auto_publish_runner,
                    daemon=True,
                    name='mqtt-modem-push',
                ).start()
            if self.health_server_ping_interval_sec > 0:
                threading.Thread(
                    target=self._mqtt_server_ping_runner,
                    daemon=True,
                    name='mqtt-server-ping',
                ).start()
        else:
            _LOGGER.error('Failed to connect to MQTT broker: rc=%d', rc)

    def _on_disconnect(self, client: mqtt.Client, userdata: Any, rc: int) -> None:
        """Callback when disconnected from broker."""
        self._modem_push_stop.set()
        self._mqtt_ping_stop.set()
        if rc == 0:
            _LOGGER.info('Disconnected from MQTT broker gracefully')
        else:
            _LOGGER.warning('Disconnected from MQTT broker unexpectedly: rc=%d', rc)

    def _subscribe_to_topics(self) -> None:
        """Subscribe to wildcard topics for all devices and optional modem status."""
        status_topic = self._wildcard_subscribe_topic(
            'TOPIC_SMS_STATUS',
            f'{self.topic_prefix}/+/sms/status',
        )
        inbox_topic = self._wildcard_subscribe_topic(
            'TOPIC_SMS_INBOX',
            f'{self.topic_prefix}/+/sms/inbox',
        )

        self.client.subscribe(status_topic, qos=self.qos)
        self.client.subscribe(inbox_topic, qos=self.qos)

        subs = [status_topic, inbox_topic]
        health_ping = self._health_ping_subscribe_pattern()
        if health_ping:
            q_hp = self.health_ping_subscribe_qos
            self.client.subscribe(health_ping, qos=q_hp)
            subs.append(health_ping)

        health_pong = self._health_pong_subscribe_pattern()
        if health_pong:
            self.client.subscribe(health_pong, qos=self.qos)
            subs.append(health_pong)

        mp = self.modem_topic_prefix
        if self.subscribe_modem_catalog:
            snap = f'{mp}/modems/snapshot'
            contacts = f'{mp}/modems/contacts'
            self.client.subscribe(snap, qos=self.qos)
            self.client.subscribe(contacts, qos=self.qos)
            subs.extend([snap, contacts])
        if self.subscribe_modem_status:
            modem_rq = f'{mp}/modems/+/status/request'
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

        if self.subscribe_modem_catalog and topic.endswith('/modems/snapshot'):
            try:
                persist_modem_catalog_from_mqtt('snapshot', payload)
            except Exception as exc:
                _LOGGER.exception('Error persisting modem snapshot catalog: %s', exc)
            return

        if self.subscribe_modem_catalog and topic.endswith('/modems/contacts'):
            try:
                persist_modem_catalog_from_mqtt('contacts', payload)
            except Exception as exc:
                _LOGGER.exception('Error persisting modem contacts catalog: %s', exc)
            return

        if topic.endswith('/health/ping'):
            if self.subscribe_health_ping:
                self._handle_health_ping(topic, payload)
            return

        if topic.endswith('/health/pong'):
            if self.subscribe_health_pong:
                self._handle_health_pong(topic, payload)
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
            resp_topic = f'{self.modem_topic_prefix}/modems/{modem_idx}/status/response'
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
        topic = f'{self.modem_topic_prefix}/modems/{modem_idx}/status/telemetry'
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

    def _mqtt_publish_scheduled_health_pings(self) -> None:
        """Publish tipo B ``health/ping`` for each active external device (connected client)."""
        for device_id in ExternalDevice.objects.filter(status=ExternalDevice.Status.ACTIVE).values_list(
            'device_id', flat=True
        ):
            try:
                self._publish_server_health_ping_connected(device_id)
            except Exception:
                _LOGGER.exception('MQTT server health ping failed device_id=%s', device_id)

    def _mqtt_server_ping_runner(self) -> None:
        interval = self.health_server_ping_interval_sec
        if interval <= 0:
            return
        _LOGGER.info('MQTT server health ping runner started (interval=%ss)', interval)
        while not self._mqtt_ping_stop.wait(timeout=interval):
            try:
                self._mqtt_publish_scheduled_health_pings()
            except Exception:
                _LOGGER.exception('MQTT server health ping tick failed')
        _LOGGER.debug('MQTT server health ping runner stopped')

    def _publish_server_health_ping_connected(self, device_id: str) -> None:
        """Publish Django-originated health ping using the persistent gateway MQTT session."""
        cfg: dict[str, Any] = mqtt_flat_cfg_for_device_id(device_id)
        topic = device_topic_from_flat_config(
            cfg,
            'TOPIC_HEALTH_PING',
            '{prefix}/{sanitized}/health/ping',
            device_id,
        )
        body = build_django_health_ping_payload()
        serialized = json.dumps(body, ensure_ascii=False)
        ping_short = str(body.get('ping_id') or '')[:48]
        if not _paho_client_connected(self.client):
            _LOGGER.debug(
                'MQTT server health ping skipped (client disconnected) device_id=%s ping_id=%s',
                device_id,
                ping_short,
            )
            return
        info = self.client.publish(topic, serialized, qos=self.qos)
        rc_raw = getattr(info, 'rc', mqtt.MQTT_ERR_SUCCESS)
        if isinstance(rc_raw, int) and rc_raw != mqtt.MQTT_ERR_SUCCESS:
            _LOGGER.warning(
                'MQTT server health ping publish rejected rc=%s topic=%s ping_id=%s',
                rc_raw,
                topic,
                ping_short,
            )
            return
        try:
            info.wait_for_publish(timeout=5.0)
        except RuntimeError as exc:
            err = str(exc).lower()
            if 'not currently connected' in err or 'not connected' in err:
                _LOGGER.warning(
                    'MQTT server health ping aborted (broker disconnected) topic=%s ping_id=%s',
                    topic,
                    ping_short,
                )
                return
            _LOGGER.warning(
                'MQTT server health ping publish wait failed topic=%s ping_id=%s',
                topic,
                ping_short,
                exc_info=True,
            )
            return
        except Exception:
            _LOGGER.warning(
                'MQTT server health ping publish wait failed topic=%s ping_id=%s',
                topic,
                ping_short,
                exc_info=True,
            )
            return
        else:
            _LOGGER.debug(
                'MQTT server health ping topic=%s ping_id=%s',
                topic,
                ping_short,
            )

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

        device_id_resolved = self._find_device_id_by_sanitized(device_id_sanitized)
        if not device_id_resolved:
            _LOGGER.warning('No device found with sanitized ID: %s', device_id_sanitized)
            return

        if HiDishelinkDevice.objects.filter(device_id=device_id_resolved, sync_external_device=True).exists():
            ExternalDevice.objects.get_or_create(
                device_id=device_id_resolved,
                defaults={
                    'name': device_id_resolved,
                    'status': ExternalDevice.Status.ACTIVE,
                    'device_type': 'hidishelink',
                },
            )

        try:
            device = ExternalDevice.objects.get(device_id=device_id_resolved)
        except ExternalDevice.DoesNotExist:
            _LOGGER.warning('Device %s not found', device_id_resolved)
            return

        try:
            inbox_msg = persist_inbox_from_mqtt(device, payload)

            if not inbox_msg.ack_sent:
                self.publish_inbox_ack(device.device_id, inbox_msg.message_id)
                inbox_msg.ack_sent = True
                inbox_msg.save(update_fields=['ack_sent'])
        except Exception as exc:
            _LOGGER.exception('Error handling inbox message from device=%s: %s', device_id_resolved, exc)

    def _extract_device_id_from_topic(self, topic: str) -> str | None:
        """Extract sanitized device segment from inbox/status topics.

        Legacy hiWaveTel paths include ``.../devices/{id}/sms/...``. hiDisheLink templates may omit the
        ``devices`` segment; then extraction uses the path segment immediately before ``sms/*``.
        """
        uses_templates = bool(self._mqtt_cfg.get('TOPIC_SMS_INBOX') or self._mqtt_cfg.get('TOPIC_SMS_STATUS'))

        if '/devices/' in topic:
            parts = topic.split('/')
            try:
                devices_idx = parts.index('devices')
                return parts[devices_idx + 1]
            except (ValueError, IndexError):
                return None

        if uses_templates:
            for suffix in ('/sms/inbox', '/sms/status'):
                if topic.endswith(suffix):
                    base = topic[: -len(suffix)]
                    seg = base.split('/')[-1]
                    return seg if seg else None
        return None

    def _find_device_id_by_sanitized(self, sanitized_id: str) -> str | None:
        """Find canonical device_id by matching sanitized topic segment."""

        if sanitized_id.isdigit():
            candidate = f'+{sanitized_id}'
            found_ed = ExternalDevice.objects.filter(device_id=candidate).values_list('device_id', flat=True).first()
            if found_ed:
                return found_ed
            if HiDishelinkDevice.objects.filter(pk=candidate).exists():
                return candidate

        for device in ExternalDevice.objects.all():
            if sanitize_device_id(device.device_id) == sanitized_id:
                return device.device_id

        for hid in HiDishelinkDevice.objects.all():
            if sanitize_device_id(hid.device_id) == sanitized_id:
                return hid.device_id

        return None


def _mqtt_short_client_id() -> str:
    """MQTT 3.1.1 restricts client identifiers to max 23 bytes."""
    cid = f'hw{uuid.uuid4().hex}'[:23]
    return cid


def _publish_json_ephemeral(
    topic: str,
    payload: dict[str, Any],
    mqtt_connection: dict[str, Any] | None = None,
) -> bool:
    """Connect, publish JSON once, disconnect (each Gunicorn worker uses a unique client_id).

    Returns ``True`` if the publish handshake completed within the timeout.
    """
    mc = mqtt_connection or {}
    broker = mc.get('MQTT_BROKER_URL') or settings.MQTT_BROKER_URL
    port = int(mc['MQTT_PORT']) if mc.get('MQTT_PORT') is not None else settings.MQTT_PORT
    qos = int(mc['MQTT_QOS']) if mc.get('MQTT_QOS') is not None else settings.MQTT_QOS
    keepalive = int(mc['MQTT_KEEPALIVE']) if mc.get('MQTT_KEEPALIVE') is not None else settings.MQTT_KEEPALIVE
    username = mc.get('MQTT_USER') or settings.MQTT_USER
    password = mc.get('MQTT_PASS') or settings.MQTT_PASS
    timeout = getattr(settings, 'MQTT_EPHEMERAL_PUBLISH_TIMEOUT_SEC', 15.0)

    client = mqtt.Client(
        client_id=_mqtt_short_client_id(),
        clean_session=True,
        protocol=mqtt.MQTTv311,
    )
    if username and password:
        client.username_pw_set(username, password)
    if port == 8883:
        client.tls_set()

    try:
        client.connect(broker, port, keepalive)
        client.loop_start()
        body = json.dumps(payload, ensure_ascii=False)
        info = client.publish(topic, body, qos=qos)
        info.wait_for_publish(timeout=timeout)
        _LOGGER.info('MQTT ephemeral publish ok topic=%s bytes=%s', topic, len(body))
        return True
    except Exception:
        _LOGGER.warning('MQTT ephemeral publish failed topic=%s', topic, exc_info=True)
        return False
    finally:
        try:
            client.loop_stop()
        except Exception:
            pass
        try:
            client.disconnect()
        except Exception:
            pass


def publish_json_ephemeral(
    topic: str,
    payload: dict[str, Any],
    mqtt_connection: dict[str, Any] | None = None,
) -> bool:
    """Public helper for one-off JSON publishes (admin / scripts)."""
    return _publish_json_ephemeral(topic, payload, mqtt_connection)


def publish_send_request_ephemeral(device_id: str, payload: dict[str, Any]) -> bool:
    """Publish outbound SMS API request payload to `{device_topic_prefix}/.../sms/send`."""
    sanitized = sanitize_device_id(device_id)
    topic = f'{resolved_mqtt_device_topic_prefix()}/{sanitized}/sms/send'
    return _publish_json_ephemeral(topic, payload)


def publish_modem_inbox_delivery_ephemeral(device_id: str, payload: dict[str, Any]) -> bool:
    """Publish modem-mirrored inbox row to `{device_topic_prefix}/.../sms/inbox_delivery` (gateway → subscribers)."""
    sanitized = sanitize_device_id(device_id)
    topic = f'{resolved_mqtt_device_topic_prefix()}/{sanitized}/sms/inbox_delivery'
    return _publish_json_ephemeral(topic, payload)


def publish_modem_inbox_broadcast_ephemeral(modem_index: int, payload: dict[str, Any]) -> bool:
    """Publish one canonical inbox delivery per modem on ``{modem_prefix}/modems/N/sms/inbox_delivery``."""
    mp = resolved_mqtt_modem_topic_prefix()
    topic = f'{mp}/modems/{modem_index}/sms/inbox_delivery'
    return _publish_json_ephemeral(topic, payload)


def publish_health_ping_ephemeral(
    device_id: str,
    mqtt_cfg: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], bool, str]:
    """Publish hiDisheLink active health ping on ``TOPIC_HEALTH_PING`` (server → gateway/app).

    Uses ``mqtt_cfg`` when provided; otherwise loads ``HiDishelinkDevice.mqtt_config`` for the
    same ``device_id``. Broker credentials fall back to Django settings inside
    :func:`_publish_json_ephemeral` when absent from config.

    Returns ``(payload, success, mqtt_topic)``.
    """
    cfg: dict[str, Any] = dict(mqtt_cfg) if mqtt_cfg else {}
    if not cfg:
        cfg = mqtt_flat_cfg_for_device_id(device_id)
    topic = device_topic_from_flat_config(
        cfg,
        'TOPIC_HEALTH_PING',
        '{prefix}/{sanitized}/health/ping',
        device_id,
    )
    body = build_django_health_ping_payload()
    conn = ephemeral_connection_from_flat(cfg)
    ok = _publish_json_ephemeral(topic, body, conn or None)
    return body, ok, topic
