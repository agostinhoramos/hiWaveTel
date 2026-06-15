"""Cross-process and persistent MQTT publish helpers."""

from __future__ import annotations

import logging
from typing import Any

from django.conf import settings

_LOGGER = logging.getLogger(__name__)


def enqueue_mqtt_publish(topic: str, payload: dict[str, Any]) -> None:
    """Persist publish intent for the mqtt gateway daemon to drain."""
    from .models import MqttPublishOutbox

    MqttPublishOutbox.objects.create(topic=topic, payload_json=payload)


def publish_json(topic: str, payload: dict[str, Any]) -> bool:
    """Publish via persistent local client, outbox, or ephemeral fallback."""
    if getattr(settings, 'MQTT_PERSISTENT_PUBLISH', True):
        from . import mqtt_client as mqtt_mod

        local = getattr(mqtt_mod, '_global_local_client', None)
        if local is not None:
            return local.publish_json(topic, payload)
        if not queues_enabled_in_process():
            enqueue_mqtt_publish(topic, payload)
            return True
    from .mqtt_client import _publish_json_ephemeral

    return _publish_json_ephemeral(topic, payload)


def publish_send_request(device_id: str, payload: dict[str, Any]) -> bool:
    from .mqtt_client import resolved_mqtt_device_topic_prefix, sanitize_device_id

    sanitized = sanitize_device_id(device_id)
    topic = f'{resolved_mqtt_device_topic_prefix()}/{sanitized}/sms/send'
    return publish_json(topic, payload)


def publish_modem_inbox_delivery(device_id: str, payload: dict[str, Any]) -> bool:
    from .mqtt_client import resolved_mqtt_device_topic_prefix, sanitize_device_id

    sanitized = sanitize_device_id(device_id)
    topic = f'{resolved_mqtt_device_topic_prefix()}/{sanitized}/sms/inbox_delivery'
    return publish_json(topic, payload)


def publish_modem_inbox_broadcast(modem_index: int, payload: dict[str, Any]) -> bool:
    from .mqtt_client import resolved_mqtt_modem_topic_prefix

    mp = resolved_mqtt_modem_topic_prefix()
    topic = f'{mp}/modems/{modem_index}/sms/inbox_delivery'
    return publish_json(topic, payload)


def publish_outbox_row(row) -> bool:
    return publish_json(row.topic, row.payload_json or {})


def queues_enabled_in_process() -> bool:
    import os
    return os.environ.get('HIWAVETEL_QUEUE_ENABLED', '').lower() == 'true'
