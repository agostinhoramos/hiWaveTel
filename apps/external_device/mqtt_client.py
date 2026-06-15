"""MQTT client for external device gateway: subscribe to status/inbox, publish send requests/ACKs."""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import uuid
from datetime import timedelta
from typing import TYPE_CHECKING, Any

import paho.mqtt.client as mqtt
from django.conf import settings

from .models import DeviceHealthTelemetry, ExternalDevice, HiDishelinkDevice
from .services import persist_inbox_from_mqtt, persist_modem_catalog_from_mqtt, update_request_from_mqtt_status

if TYPE_CHECKING:
    from paho.mqtt.client import MQTTMessage

_LOGGER = logging.getLogger(__name__)


# Global remote client instance (set by run_mqtt_gateway command)
_global_remote_client = None
_global_local_client = None

# Device ID sanitization cache to avoid O(N) table scans on every MQTT message
# Maps sanitized_id -> canonical device_id
_device_id_cache: dict[str, str | None] = {}
_device_id_cache_lock = threading.Lock()


def clear_device_id_cache() -> None:
    """Clear the device ID sanitization cache.
    
    Should be called when ExternalDevice or HiDishelinkDevice instances are saved/deleted.
    """
    with _device_id_cache_lock:
        _device_id_cache.clear()
        _LOGGER.debug('Device ID sanitization cache cleared')


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
    from apps.sms.mmcli_lock import mmcli_serial

    timeout = float(getattr(settings, 'MQTT_MODEM_STATUS_COMMAND_TIMEOUT_SEC', 45.0))
    try:
        with mmcli_serial():
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
    """Body for server-originated ``health/ping`` (``source`` is ``django``).

    When ``MQTT_HEALTH_AUTO_PONG`` is true, this gateway echoes each received ``ping_id`` to
    ``health/pong`` on the MQTT loop—including when the broker delivers that same ping back onto
    the wildcard subscription—or the mobile / external MQTT integration may pong instead.

    Responses that update ``ExternalDevice`` presence ultimately come via ``health/pong`` /
    telemetry (see docs/comunicacao.md §5.6)—not by treating server-origin ``django`` ping receipt
    alone as device telemetry.
    """
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


def resolve_remote_bridge_target() -> tuple[str | None, dict[str, Any]]:
    """Resolve remote bridge ``device_id`` and cached mqtt-config (no HTTP fetch).

    Used by the SMS watcher process when ``_global_remote_client`` is not available
    (``run_mqtt_gateway`` runs in a separate OS process).
    """
    device_id = getattr(settings, 'MQTT_REMOTE_DEVICE_ID', '').strip()
    if not device_id:
        hid_row = (
            HiDishelinkDevice.objects.filter(status=HiDishelinkDevice.Status.ACTIVE)
            .order_by('-mqtt_config_fetched_at', '-updated_at')
            .first()
        )
        if hid_row:
            device_id = str(hid_row.device_id).strip()

    if not device_id:
        return None, {}

    cfg = mqtt_flat_cfg_for_device_id(device_id)
    if cfg:
        return device_id, cfg

    hid_snap = (
        HiDishelinkDevice.objects.filter(
            device_id=device_id,
            status=HiDishelinkDevice.Status.ACTIVE,
        )
        .exclude(mqtt_config=None)
        .first()
    )
    if hid_snap and isinstance(hid_snap.mqtt_config, dict):
        return device_id, dict(hid_snap.mqtt_config)

    return device_id, {}


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
    - ``{device_topic_prefix}/+/health/ping`` — device-side traffic; gateway may also see broker echo of its tipo B pings; with ``MQTT_HEALTH_AUTO_PONG``, replies on ``…/health/pong``
    - ``{device_topic_prefix}/+/health/pong`` — updates ``ExternalDevice`` when received (app or gateway pong)
    - ``{prefix}/modems/snapshot`` and ``{prefix}/modems/contacts`` — gateway catalog (optional)
    - ``{prefix}/modems/+/status/request`` — request full modem snapshot (mmcli), optional

    Publishes:
    - ``{device_topic_prefix}/…/sms/send`` and ``…/inbox/ack``
    - ``{device_topic_prefix}/…/health/ping`` — periodic Django tipo B pings when ``MQTT_HEALTH_SERVER_PING_INTERVAL_SEC`` > 0 (**consumer**: hiDisheLink app or MQTT client from mqtt-config ``TOPIC_HEALTH_PING``, not REST-only)
    - ``{device_topic_prefix}/…/health/pong`` — automatic ``ping_id`` reply to ``health/ping`` when ``MQTT_HEALTH_AUTO_PONG`` / lab django flag (see ``_handle_health_ping``)
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
        self.subscribe_health_ping = getattr(settings, 'MQTT_HEALTH_PING_SUBSCRIBE', True)
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

        self._reconnect_stop = threading.Event()
        self._reconnect_delay_ms = int(getattr(settings, 'MQTT_RECONNECT_INITIAL_DELAY_MS', 1000))
        self._reconnect_max_ms = int(getattr(settings, 'MQTT_RECONNECT_MAX_DELAY_MS', 30000))
        self._reconnect_multiplier = float(getattr(settings, 'MQTT_RECONNECT_BACKOFF_MULTIPLIER', 2.0))
        self._reconnect_jitter = float(getattr(settings, 'MQTT_RECONNECT_JITTER', 0.2))
        self._watchdog_interval_ms = int(getattr(settings, 'MQTT_CONNECTION_WATCHDOG_INTERVAL_MS', 0))

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
        self._reconnect_stop.clear()
        if self._watchdog_interval_ms > 0:
            threading.Thread(
                target=self._connection_watchdog_runner,
                daemon=True,
                name='mqtt-local-watchdog',
            ).start()

    def publish_json(self, topic: str, payload: dict[str, Any]) -> bool:
        """Publish JSON on the persistent client session."""
        if not _paho_client_connected(self.client):
            _LOGGER.warning('Persistent MQTT client disconnected; cannot publish topic=%s', topic)
            return False
        try:
            message = json.dumps(payload)
            info = self.client.publish(topic, message, qos=self.qos)
            info.wait_for_publish(timeout=float(getattr(settings, 'MQTT_EPHEMERAL_PUBLISH_TIMEOUT_SEC', 15.0)))
            return bool(info.is_published())
        except Exception:
            _LOGGER.warning('Persistent MQTT publish failed topic=%s', topic, exc_info=True)
            return False

    def _connection_watchdog_runner(self) -> None:
        interval = max(0.5, self._watchdog_interval_ms / 1000.0)
        while not self._reconnect_stop.wait(interval):
            if not _paho_client_connected(self.client):
                self._schedule_reconnect()

    def _schedule_reconnect(self) -> None:
        if not getattr(settings, 'MQTT_AUTO_RECONNECT', True):
            return
        threading.Thread(target=self._reconnect_with_backoff, daemon=True, name='mqtt-local-reconnect').start()

    def _reconnect_with_backoff(self) -> None:
        import random

        delay_ms = self._reconnect_delay_ms
        max_retries = int(getattr(settings, 'MQTT_RECONNECT_MAX_RETRIES', 0))
        attempt = 0
        while not self._reconnect_stop.is_set():
            if _paho_client_connected(self.client):
                self._reconnect_delay_ms = int(getattr(settings, 'MQTT_RECONNECT_INITIAL_DELAY_MS', 1000))
                return
            attempt += 1
            if max_retries > 0 and attempt > max_retries:
                _LOGGER.error('MQTT reconnect max retries exceeded')
                return
            jitter = 1.0 + random.uniform(-self._reconnect_jitter, self._reconnect_jitter)
            sleep_sec = max(0.1, (delay_ms / 1000.0) * jitter)
            _LOGGER.warning('MQTT reconnect attempt %s in %.2fs', attempt, sleep_sec)
            if self._reconnect_stop.wait(sleep_sec):
                return
            try:
                self.client.reconnect()
            except Exception as exc:
                _LOGGER.warning('MQTT reconnect failed: %s', exc)
            delay_ms = min(int(delay_ms * self._reconnect_multiplier), self._reconnect_max_ms)
            self._reconnect_delay_ms = delay_ms

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
        self._reconnect_stop.set()
        _LOGGER.info('Disconnecting from MQTT broker...')
        self.client.disconnect()

    def _dispatch_mqtt_handler_sync(self, handler_key: str, topic: str, payload: dict[str, Any]) -> None:
        if handler_key == 'modem_status_request':
            if self.subscribe_modem_status:
                self._schedule_modem_status_snapshot(topic)
        elif handler_key == 'catalog_snapshot':
            persist_modem_catalog_from_mqtt('snapshot', payload)
        elif handler_key == 'catalog_contacts':
            persist_modem_catalog_from_mqtt('contacts', payload)
        elif handler_key == 'health_ping':
            self._handle_health_ping(topic, payload)
        elif handler_key == 'health_pong':
            self._handle_health_pong(topic, payload)
        elif handler_key == 'sms_status':
            self._handle_status_message(topic, payload)
        elif handler_key == 'sms_inbox':
            self._handle_inbox_message(topic, payload)

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
        """Handle ``health/ping``: Android telemetry (tipo A), servidor tipo B (`source`=django), or legacy echo.

        Pings ``source=django`` do not imply device presence until ``health/pong`` is processed or telemetry
        is stored. Por defeito (**``MQTT_HEALTH_AUTO_PONG``**), este gateway volta a publicar ``health/pong``
        com o mesmo ``ping_id`` para todos os pings com ``ping_id`` (incl. tipo B servidor). Opcionalmente
        **``MQTT_HEALTH_GATEWAY_AUTO_PONG_DJANGO``** actua em conjunto (ver código). Resposta efectiva também
        pode vir da app hiDisheLink ou cliente MQTT externos (docs/comunicacao.md §5.6).
        """
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
            # Same rule as legacy pings below: MQTT_HEALTH_AUTO_PONG (default true in settings)
            # echoes ping_id to health/pong — including tipo B servidor (source=django). Optional
            # MQTT_HEALTH_GATEWAY_AUTO_PONG_DJANGO retains meaning when callers disable the global flag
            # but keep this lab toggle true (paired use is rare).
            mqtt_auto_pong = getattr(settings, 'MQTT_HEALTH_AUTO_PONG', True)
            if payload.get('ping_id') and (mqtt_auto_pong or self.gateway_auto_pong_django):
                self._publish_gateway_health_pong(sanitized, payload)
            else:
                ping_short = str(payload.get('ping_id') or '')[:48]
                _LOGGER.debug(
                    'health/ping django ping_id=%s (no gateway pong: need ping_id and '
                    'MQTT_HEALTH_AUTO_PONG or MQTT_HEALTH_GATEWAY_AUTO_PONG_DJANGO)',
                    ping_short,
                )
                _LOGGER.debug(
                    'source=django ping does not touch ExternalDevice presence from ping alone '
                    '(see docs/comunicacao.md §5.6): app/gateway MQTT may still pong with '
                    'same ping_id and/or telemetry on health/ping. ping_id=%s',
                    ping_short,
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
            self._schedule_reconnect()

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
        """Callback when message received — enqueue only (fast path)."""
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

        from apps.external_device.mqtt_handler_queue import classify_mqtt_topic, get_mqtt_handler_queue

        handler_key, rank = classify_mqtt_topic(topic)
        if handler_key is None:
            _LOGGER.warning('Unknown topic pattern: %s', topic)
            return
        if handler_key.startswith('catalog_') and not self.subscribe_modem_catalog:
            return
        if handler_key == 'health_ping' and not self.subscribe_health_ping:
            return
        if handler_key == 'health_pong' and not self.subscribe_health_pong:
            return
        if handler_key == 'modem_status_request' and not self.subscribe_modem_status:
            return

        mq = get_mqtt_handler_queue()
        if mq is not None:
            mq.enqueue(handler_key, topic, payload, client_ref=self, rank=rank)
            return

        self._dispatch_mqtt_handler_sync(handler_key, topic, payload)

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
        """Publish tipo B ``health/ping`` for each active ExternalDevice.

        Pong / telemetry replies must come from the device's MQTT client (hiDisheLink or
        mqtt-config ``TOPIC_*``), not from Django HTTP acting as the pong consumer alone.
        """
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
        """Publish Django ``health/ping`` on the persistent gateway MQTT session.

        Consumer side is the hiDisheLink app or MQTT client using ``TOPIC_HEALTH_PING`` from
        that device's ``mqtt-config``.
        """
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
        """Find canonical device_id by matching sanitized topic segment (with caching)."""

        # Check cache first
        with _device_id_cache_lock:
            if sanitized_id in _device_id_cache:
                return _device_id_cache[sanitized_id]

        # Cache miss - perform lookup
        result = None
        
        # Fast path: if sanitized_id is all digits, try +NNNN pattern
        if sanitized_id.isdigit():
            candidate = f'+{sanitized_id}'
            found_ed = ExternalDevice.objects.filter(device_id=candidate).values_list('device_id', flat=True).first()
            if found_ed:
                result = found_ed
            elif HiDishelinkDevice.objects.filter(pk=candidate).exists():
                result = candidate

        if result is None:
            found = ExternalDevice.objects.filter(sanitized_device_id=sanitized_id).values_list(
                'device_id', flat=True
            ).first()
            if found:
                result = found

        # Slow path: iterate all devices (legacy rows without sanitized_device_id)
        if result is None:
            for device in ExternalDevice.objects.all():
                if sanitize_device_id(device.device_id) == sanitized_id:
                    result = device.device_id
                    break

        if result is None:
            for hid in HiDishelinkDevice.objects.all():
                if sanitize_device_id(hid.device_id) == sanitized_id:
                    result = hid.device_id
                    break

        # Cache the result (even if None, to avoid repeated lookups for non-existent devices)
        with _device_id_cache_lock:
            _device_id_cache[sanitized_id] = result

        return result


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
    """Publish modem-mirrored inbox row to `{device_topic_prefix}/.../sms/inbox_delivery`."""
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


# Alias for backward compatibility (will be refactored to LocalGatewayClient)
LocalGatewayClient = GatewayMqttClient


class RemoteHiDishelinkClient:
    """MQTT client for hiWaveTel acting as Device/Gateway Client in hiDisheLink architecture.
    
    This client connects to the **remote hiDisheLink broker** and implements the contract from
    section 10 of the hiDisheLink MQTT architecture document:
    
    Subscribes to:
    - TOPIC_SMS_SEND - receive SMS send requests from hiDisheLink server
    - TOPIC_HEALTH_PING - receive health probes from hiDisheLink server
    - TOPIC_SMS_INBOX_ACK - receive ACKs for inbox messages
    
    Publishes to:
    - TOPIC_SMS_STATUS - report SMS send status (received ACK + final status)
    - TOPIC_SMS_INBOX - report inbound SMS from local modem
    - TOPIC_HEALTH_PONG - respond to health probes
    - TOPIC_HEALTH_PING - heartbeat telemetry (without source:django)
    
    All SMS and health messages use QoS 1 per hiDisheLink spec.
    """
    
    def __init__(self, mqtt_config: dict[str, Any], device_id: str):
        """Initialize remote hiDisheLink MQTT client.
        
        Args:
            mqtt_config: Full mqtt-config response from hiDisheLink API
            device_id: Device ID for this gateway (E.164 format, e.g. +351912329317)
        """
        self._mqtt_cfg = dict(mqtt_config)
        self.device_id = device_id
        self.sanitized_device_id = sanitize_device_id(device_id)
        
        # Extract connection params from mqtt-config
        def pick_str(key: str, fallback: str) -> str:
            v = self._mqtt_cfg.get(key)
            if v is not None and str(v).strip():
                return str(v).strip()
            return fallback
        
        def pick_int(key: str, fallback: int) -> int:
            v = self._mqtt_cfg.get(key)
            if v is None or (isinstance(v, str) and not v.strip()):
                return fallback
            try:
                return int(v)
            except (TypeError, ValueError):
                return fallback
        
        def pick_bool(key: str, fallback: bool) -> bool:
            if key not in self._mqtt_cfg:
                return fallback
            raw = str(self._mqtt_cfg[key]).strip().lower()
            if raw in ('true', '1', 'yes', 'on'):
                return True
            if raw in ('false', '0', 'no', 'off'):
                return False
            return fallback
        
        self.broker_url = pick_str('MQTT_BROKER_URL', settings.MQTT_BROKER_URL)
        self.port = pick_int('MQTT_PORT', settings.MQTT_PORT)
        self.username = pick_str('MQTT_USERNAME', '') or pick_str('MQTT_USER', '') or settings.MQTT_USER
        self.password = pick_str('MQTT_PASSWORD', '') or pick_str('MQTT_PASS', '') or settings.MQTT_PASS
        self.keepalive = pick_int('MQTT_KEEPALIVE', settings.MQTT_KEEPALIVE)
        self.qos = pick_int('MQTT_QOS', 1)  # Default QoS 1 for hiDisheLink
        self.clean_session = pick_bool('MQTT_CLEAN_SESSION', False)  # Persistent session for reliability
        
        # Client ID: hiwavetel_remote_{sanitized}_{timestamp}
        import time
        self.client_id = f'hiwavetel_remote_{self.sanitized_device_id}_{int(time.time())}'[:23]
        
        # Resolve topics from templates
        self.topic_sms_send = self._resolve_topic('TOPIC_SMS_SEND', '{prefix}/{sanitized}/sms/send')
        self.topic_sms_status = self._resolve_topic('TOPIC_SMS_STATUS', '{prefix}/{sanitized}/sms/status')
        self.topic_sms_inbox = self._resolve_topic('TOPIC_SMS_INBOX', '{prefix}/{sanitized}/sms/inbox')
        self.topic_sms_inbox_ack = self._resolve_topic('TOPIC_SMS_INBOX_ACK', '{prefix}/{sanitized}/sms/inbox/ack')
        self.topic_health_ping = self._resolve_topic('TOPIC_HEALTH_PING', '{prefix}/{sanitized}/health/ping')
        self.topic_health_pong = self._resolve_topic('TOPIC_HEALTH_PONG', '{prefix}/{sanitized}/health/pong')
        
        # Health heartbeat settings
        self.health_heartbeat_interval = float(
            getattr(settings, 'MQTT_REMOTE_HEALTH_HEARTBEAT_SEC', 60.0)
        )
        self._heartbeat_stop = threading.Event()
        
        # Chunking buffer for SMS send requests
        self._chunk_buffer: dict[str, dict[str, Any]] = {}
        self._chunk_lock = threading.Lock()
        self._chunk_ttl_sec = 300  # 5 minutes

        self._reconnect_stop = threading.Event()
        self._reconnect_delay_ms = int(getattr(settings, 'MQTT_RECONNECT_INITIAL_DELAY_MS', 1000))
        self._reconnect_max_ms = int(getattr(settings, 'MQTT_RECONNECT_MAX_DELAY_MS', 30000))
        self._reconnect_multiplier = float(getattr(settings, 'MQTT_RECONNECT_BACKOFF_MULTIPLIER', 2.0))
        self._reconnect_jitter = float(getattr(settings, 'MQTT_RECONNECT_JITTER', 0.2))
        
        # Create Paho client
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
        
        _LOGGER.info(
            'RemoteHiDishelinkClient initialized device=%s broker=%s:%d client_id=%s',
            self.device_id,
            self.broker_url,
            self.port,
            self.client_id,
        )
    
    def _resolve_topic(self, template_key: str, legacy_fmt: str) -> str:
        """Resolve MQTT topic from template or legacy format."""
        return device_topic_from_flat_config(
            self._mqtt_cfg,
            template_key,
            legacy_fmt,
            self.device_id,
        )
    
    def connect(self) -> None:
        """Connect to remote hiDisheLink MQTT broker."""
        _LOGGER.info(
            'RemoteHiDishelinkClient connecting to %s:%d device=%s',
            self.broker_url,
            self.port,
            self.device_id,
        )
        self.client.connect(self.broker_url, self.port, self.keepalive)
        self._reconnect_stop.clear()

    def _schedule_remote_reconnect(self) -> None:
        if not getattr(settings, 'MQTT_AUTO_RECONNECT', True):
            return
        threading.Thread(
            target=self._remote_reconnect_with_backoff,
            daemon=True,
            name=f'mqtt-remote-reconnect-{self.sanitized_device_id}',
        ).start()

    def _remote_reconnect_with_backoff(self) -> None:
        import random

        delay_ms = self._reconnect_delay_ms
        max_retries = int(getattr(settings, 'MQTT_RECONNECT_MAX_RETRIES', 0))
        attempt = 0
        while not self._reconnect_stop.is_set():
            if _paho_client_connected(self.client):
                self._reconnect_delay_ms = int(getattr(settings, 'MQTT_RECONNECT_INITIAL_DELAY_MS', 1000))
                return
            attempt += 1
            if max_retries > 0 and attempt > max_retries:
                return
            jitter = 1.0 + random.uniform(-self._reconnect_jitter, self._reconnect_jitter)
            sleep_sec = max(0.1, (delay_ms / 1000.0) * jitter)
            if self._reconnect_stop.wait(sleep_sec):
                return
            try:
                self.client.reconnect()
            except Exception as exc:
                _LOGGER.warning('Remote MQTT reconnect failed device=%s: %s', self.device_id, exc)
            delay_ms = min(int(delay_ms * self._reconnect_multiplier), self._reconnect_max_ms)
            self._reconnect_delay_ms = delay_ms
    
    def loop_forever(self) -> None:
        """Start MQTT client loop (blocking)."""
        _LOGGER.info('RemoteHiDishelinkClient starting loop device=%s', self.device_id)
        self.client.loop_forever()
    
    def loop_start(self) -> None:
        """Start MQTT client loop in background thread."""
        _LOGGER.info('RemoteHiDishelinkClient starting background loop device=%s', self.device_id)
        self.client.loop_start()
    
    def loop_stop(self) -> None:
        """Stop MQTT client background loop."""
        _LOGGER.info('RemoteHiDishelinkClient stopping loop device=%s', self.device_id)
        self.client.loop_stop()
    
    def disconnect(self) -> None:
        """Disconnect from remote broker."""
        self._heartbeat_stop.set()
        self._reconnect_stop.set()
        _LOGGER.info('RemoteHiDishelinkClient disconnecting device=%s', self.device_id)
        self.client.disconnect()
    
    def _on_connect(self, client: mqtt.Client, userdata: Any, flags: dict, rc: int) -> None:
        """Callback when connected to remote broker."""
        if rc == 0:
            _LOGGER.info('RemoteHiDishelinkClient connected successfully device=%s', self.device_id)
            self._subscribe_to_topics()
            self._heartbeat_stop.clear()
            
            # Start health heartbeat thread
            if self.health_heartbeat_interval > 0:
                threading.Thread(
                    target=self._health_heartbeat_runner,
                    daemon=True,
                    name=f'mqtt-remote-heartbeat-{self.sanitized_device_id}',
                ).start()
            
            # Start chunk cleanup thread
            threading.Thread(
                target=self._chunk_cleanup_runner,
                daemon=True,
                name=f'mqtt-remote-chunk-cleanup-{self.sanitized_device_id}',
            ).start()
        else:
            _LOGGER.error('RemoteHiDishelinkClient connection failed rc=%d device=%s', rc, self.device_id)
    
    def _chunk_cleanup_runner(self) -> None:
        """Periodic cleanup of expired chunk buffers."""
        cleanup_interval = 60.0  # Check every minute
        _LOGGER.debug('RemoteHiDishelinkClient chunk cleanup started device=%s', self.device_id)
        
        while not self._heartbeat_stop.wait(timeout=cleanup_interval):
            try:
                self._cleanup_expired_chunks()
            except Exception:
                _LOGGER.exception('RemoteHiDishelinkClient chunk cleanup failed')
        
        _LOGGER.debug('RemoteHiDishelinkClient chunk cleanup stopped device=%s', self.device_id)
    
    def _on_disconnect(self, client: mqtt.Client, userdata: Any, rc: int) -> None:
        """Callback when disconnected from remote broker."""
        self._heartbeat_stop.set()
        if rc == 0:
            _LOGGER.info('RemoteHiDishelinkClient disconnected gracefully device=%s', self.device_id)
        else:
            _LOGGER.warning(
                'RemoteHiDishelinkClient disconnected unexpectedly rc=%d device=%s',
                rc,
                self.device_id,
            )
            self._schedule_remote_reconnect()
    
    def _subscribe_to_topics(self) -> None:
        """Subscribe to remote hiDisheLink topics."""
        subs = [
            (self.topic_sms_send, 1),  # QoS 1 for SMS
            (self.topic_health_ping, 1),  # QoS 1 for health
            (self.topic_sms_inbox_ack, 1),  # QoS 1 for ACKs
        ]
        
        for topic, qos in subs:
            self.client.subscribe(topic, qos=qos)
        
        topics_str = ', '.join(t for t, _ in subs)
        _LOGGER.info('RemoteHiDishelinkClient subscribed to: %s device=%s', topics_str, self.device_id)
    
    def _on_message(self, client: mqtt.Client, userdata: Any, msg: MQTTMessage) -> None:
        """Callback when message received from remote broker."""
        topic = msg.topic
        payload_str = msg.payload.decode('utf-8')
        
        _LOGGER.debug('RemoteHiDishelinkClient received on %s: %s', topic, payload_str[:200])
        
        try:
            payload = json.loads(payload_str) if payload_str.strip() else {}
            if not isinstance(payload, dict):
                payload = {}
        except json.JSONDecodeError:
            _LOGGER.warning('RemoteHiDishelinkClient invalid JSON on %s: %s', topic, payload_str[:100])
            return
        
        # Route messages
        if topic == self.topic_sms_send:
            self._handle_sms_send(payload)
        elif topic == self.topic_health_ping:
            self._handle_health_ping(payload)
        elif topic == self.topic_sms_inbox_ack:
            self._handle_inbox_ack(payload)
        else:
            _LOGGER.warning('RemoteHiDishelinkClient unknown topic: %s', topic)
    
    def _handle_sms_send(self, payload: dict[str, Any]) -> None:
        """Handle SMS send request from hiDisheLink server (section 4 of spec).
        
        Implements chunking aggregation per section 10.10 (checklist item 9):
        - Buffer chunks with same request_id
        - Wait for all chunks (chunk_index 0 to chunk_total-1)
        - Process aggregated recipients as single job
        """
        request_id = payload.get('request_id', '').strip()
        if not request_id:
            _LOGGER.warning('RemoteHiDishelinkClient SMS send missing request_id')
            return
        
        chunk_index = payload.get('chunk_index')
        chunk_total = payload.get('chunk_total')
        
        # No chunking - process immediately
        if chunk_index is None or chunk_total is None:
            from .services import handle_remote_sms_send
            threading.Thread(
                target=handle_remote_sms_send,
                args=(self, payload),
                daemon=True,
                name=f'remote-sms-{request_id[:16]}',
            ).start()
            return
        
        # Chunking - buffer and aggregate
        with self._chunk_lock:
            if request_id not in self._chunk_buffer:
                self._chunk_buffer[request_id] = {
                    'chunks': {},
                    'chunk_total': chunk_total,
                    'timestamp': timezone.now(),
                    'message': payload.get('message'),
                    'priority': payload.get('priority'),
                }
            
            buffer_entry = self._chunk_buffer[request_id]
            buffer_entry['chunks'][chunk_index] = payload.get('recipients', [])
            
            _LOGGER.info(
                'RemoteHiDishelinkClient SMS chunk buffered request_id=%s chunk=%s/%s',
                request_id,
                chunk_index,
                chunk_total,
            )
            
            # Check if we have all chunks
            if len(buffer_entry['chunks']) == chunk_total:
                # Aggregate recipients from all chunks (sorted by chunk_index)
                all_recipients = []
                for idx in sorted(buffer_entry['chunks'].keys()):
                    all_recipients.extend(buffer_entry['chunks'][idx])
                
                # Create aggregated payload
                aggregated_payload = {
                    'request_id': request_id,
                    'recipients': all_recipients,
                    'message': buffer_entry['message'],
                    'priority': buffer_entry['priority'],
                }
                
                # Remove from buffer
                del self._chunk_buffer[request_id]
                
                _LOGGER.info(
                    'RemoteHiDishelinkClient SMS chunks complete request_id=%s total_recipients=%s',
                    request_id,
                    len(all_recipients),
                )
                
                # Process aggregated request
                from .services import handle_remote_sms_send
                threading.Thread(
                    target=handle_remote_sms_send,
                    args=(self, aggregated_payload),
                    daemon=True,
                    name=f'remote-sms-{request_id[:16]}',
                ).start()
            else:
                _LOGGER.debug(
                    'RemoteHiDishelinkClient SMS waiting for more chunks request_id=%s got=%s/%s',
                    request_id,
                    len(buffer_entry['chunks']),
                    chunk_total,
                )
    
    def _cleanup_expired_chunks(self) -> None:
        """Remove expired chunk buffers (TTL cleanup)."""
        from django.utils import timezone as tz
        
        with self._chunk_lock:
            expired = []
            cutoff = tz.now() - timedelta(seconds=self._chunk_ttl_sec)
            
            for request_id, buffer_entry in self._chunk_buffer.items():
                if buffer_entry['timestamp'] < cutoff:
                    expired.append(request_id)
            
            for request_id in expired:
                buffer_entry = self._chunk_buffer.pop(request_id)
                _LOGGER.warning(
                    'RemoteHiDishelinkClient SMS chunk buffer expired request_id=%s got=%s/%s',
                    request_id,
                    len(buffer_entry['chunks']),
                    buffer_entry['chunk_total'],
                )
    
    def _handle_health_ping(self, payload: dict[str, Any]) -> None:
        """Handle health ping from hiDisheLink server (section 7 of spec)."""
        source = payload.get('source')
        ping_id = payload.get('ping_id')
        
        # Respond to server probes (source=django with ping_id)
        if source == 'django' and ping_id:
            self.publish_health_pong(ping_id, payload.get('timestamp'))
    
    def _handle_inbox_ack(self, payload: dict[str, Any]) -> None:
        """Handle inbox ACK from hiDisheLink server (section 6 of spec)."""
        message_id = payload.get('message_id')
        if message_id:
            _LOGGER.info('RemoteHiDishelinkClient inbox ACK received message_id=%s', message_id)
            # Can mark local message as ACKed if tracking is needed
    
    def publish_sms_status(self, request_id: str, status: str, payload: dict[str, Any]) -> bool:
        """Publish SMS status to hiDisheLink server (section 5 of spec).
        
        Args:
            request_id: Request ID from sms/send
            status: 'received', 'success', 'partial', or 'error'
            payload: Full status payload with sent/failed counts and details
            
        Returns:
            True if publish succeeded
        """
        from django.utils import timezone
        
        msg = dict(payload)
        msg['request_id'] = request_id
        msg['status'] = status
        msg['device_id'] = self.device_id
        if 'timestamp' not in msg:
            msg['timestamp'] = timezone.now().isoformat()
        
        try:
            body = json.dumps(msg, ensure_ascii=False)
            info = self.client.publish(self.topic_sms_status, body, qos=1)  # QoS 1
            info.wait_for_publish(timeout=5.0)
            _LOGGER.info(
                'RemoteHiDishelinkClient published SMS status request_id=%s status=%s',
                request_id,
                status,
            )
            return True
        except Exception:
            _LOGGER.exception(
                'RemoteHiDishelinkClient failed to publish SMS status request_id=%s',
                request_id,
            )
            return False
    
    def publish_sms_inbox(self, message_id: str, sender: str, body_text: str, timestamp: str) -> bool:
        """Publish inbound SMS to hiDisheLink server (section 6 of spec).
        
        Args:
            message_id: Unique message ID
            sender: Sender phone number
            body_text: SMS content
            timestamp: ISO 8601 timestamp
            
        Returns:
            True if publish succeeded
        """
        payload = {
            'message_id': message_id,
            'sender': sender,
            'body': body_text,
            'timestamp': timestamp,
        }
        
        try:
            msg = json.dumps(payload, ensure_ascii=False)
            info = self.client.publish(self.topic_sms_inbox, msg, qos=1)  # QoS 1
            info.wait_for_publish(timeout=5.0)
            _LOGGER.info(
                'RemoteHiDishelinkClient published inbox message_id=%s sender=%s',
                message_id,
                sender,
            )
            return True
        except Exception:
            _LOGGER.exception(
                'RemoteHiDishelinkClient failed to publish inbox message_id=%s',
                message_id,
            )
            return False
    
    def publish_health_pong(self, ping_id: str, ping_timestamp: str | None = None) -> bool:
        """Publish health pong to hiDisheLink server (section 7 of spec).
        
        Args:
            ping_id: Ping ID from health/ping
            ping_timestamp: Original timestamp from ping (optional)
            
        Returns:
            True if publish succeeded
        """
        from django.utils import timezone
        
        payload: dict[str, Any] = {
            'ping_id': ping_id,
            'timestamp': timezone.now().isoformat(),
            'source': 'hiwavetel_gateway',
        }
        if ping_timestamp:
            payload['ping_timestamp'] = ping_timestamp
        
        try:
            body = json.dumps(payload, ensure_ascii=False)
            info = self.client.publish(self.topic_health_pong, body, qos=1)  # QoS 1
            info.wait_for_publish(timeout=5.0)
            _LOGGER.info('RemoteHiDishelinkClient published health pong ping_id=%s', ping_id)
            return True
        except Exception:
            _LOGGER.exception('RemoteHiDishelinkClient failed to publish health pong ping_id=%s', ping_id)
            return False
    
    def publish_health_heartbeat(self) -> bool:
        """Publish health heartbeat (tipo A telemetry) to hiDisheLink server.
        
        Sends health/ping WITHOUT source:django, with battery_level and/or network_type
        per section 7 of the spec.
        
        Returns:
            True if publish succeeded
        """
        from django.utils import timezone
        
        payload = {
            'timestamp': timezone.now().isoformat(),
            'battery_level': 100,  # Mock value for gateway (not actual battery)
            'network_type': 'Ethernet',  # Assumes wired connection
        }
        
        try:
            body = json.dumps(payload, ensure_ascii=False)
            info = self.client.publish(self.topic_health_ping, body, qos=1)  # QoS 1
            info.wait_for_publish(timeout=5.0)
            _LOGGER.debug('RemoteHiDishelinkClient published health heartbeat')
            return True
        except Exception:
            _LOGGER.exception('RemoteHiDishelinkClient failed to publish health heartbeat')
            return False
    
    def _health_heartbeat_runner(self) -> None:
        """Periodic health heartbeat thread."""
        _LOGGER.info(
            'RemoteHiDishelinkClient health heartbeat started interval=%ss device=%s',
            self.health_heartbeat_interval,
            self.device_id,
        )
        
        while not self._heartbeat_stop.wait(timeout=self.health_heartbeat_interval):
            try:
                self.publish_health_heartbeat()
            except Exception:
                _LOGGER.exception('RemoteHiDishelinkClient health heartbeat tick failed')
        
        _LOGGER.debug('RemoteHiDishelinkClient health heartbeat stopped device=%s', self.device_id)
