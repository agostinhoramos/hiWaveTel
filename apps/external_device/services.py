"""Service layer for external device gateway: registration, SMS dispatch, inbox handling."""

from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import timedelta
from typing import Any

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from apps.sms.models import OutboundSms
from apps.sms.services import dispatch_outbound_mmcli

from .models import (
    DeviceHealthTelemetry,
    DeviceSession,
    ExternalDevice,
    HiDishelinkDevice,
    InboxMessage,
    MqttGatewayCatalogEntry,
    SmsRecipientStatus,
    SmsRequest,
)

_LOGGER = logging.getLogger(__name__)


_MANUAL_INBOX_DEDUP_MINUTES = 15


def _recent_manual_inbox_matches_inbound(inbound) -> bool:
    """True when any device has a recent manual admin inbox row for this sender/body."""
    sender = (inbound.from_number or '').strip()
    body = (inbound.text or '').strip()
    if not sender or not body:
        return False
    cutoff = timezone.now() - timedelta(minutes=_MANUAL_INBOX_DEDUP_MINUTES)
    return InboxMessage.objects.filter(
        message_id__startswith='inbox_manual_',
        sender=sender,
        body=body,
        received_at__gte=cutoff,
    ).exists()


def _inbound_likely_outbound_echo(inbound) -> bool:
    """True when modem inbox likely echoes a recent outbound send (admin «also send via modem»)."""
    body = (inbound.text or '').strip()
    if not body:
        return False
    cutoff = timezone.now() - timedelta(minutes=_MANUAL_INBOX_DEDUP_MINUTES)
    qs = OutboundSms.objects.filter(
        state=OutboundSms.State.SENT,
        text=body,
        created_at__gte=cutoff,
    )
    sender = (inbound.from_number or '').strip()
    if sender:
        qs = qs.filter(to_number=sender)
    return qs.exists()


def inbound_should_skip_modem_mirror(inbound) -> bool:
    """Whether ``sync_single_inbound_to_all_devices`` should ignore this InboundSms."""
    if _inbound_likely_outbound_echo(inbound):
        return True
    return _recent_manual_inbox_matches_inbound(inbound)


def inbound_ready_for_inbox_mirror(inbound) -> bool:
    """Wait until mmcli snapshot has sender or body before mirroring (avoids empty duplicates)."""
    return bool((inbound.text or '').strip()) or bool((inbound.from_number or '').strip())


def modem_index_for_external_device(device: ExternalDevice) -> int:
    """Modem index for ``mmcli -m N`` when sending on behalf of an external device.

    Uses ``device.metadata['modem_index']`` when it is a valid int (same convention as
    inbox sync); otherwise ``settings.MODEM_MMCLI_INDEX`` — matching the JWT outbound
    SMS API behaviour.
    """
    meta = device.metadata or {}
    raw = meta.get('modem_index')
    if raw is not None:
        try:
            return int(raw)
        except (TypeError, ValueError):
            _LOGGER.warning(
                'Device %s metadata.modem_index=%r invalid; using MODEM_MMCLI_INDEX',
                device.device_id,
                raw,
            )
    from apps.sms.mmcli_client import resolve_modem_mmcli_index

    return resolve_modem_mmcli_index(int(getattr(settings, 'MODEM_MMCLI_INDEX', 0)))


def generate_token(length: int = 48) -> str:
    """Generate a URL-safe random token."""
    return secrets.token_urlsafe(length)


def hash_token(raw_token: str) -> str:
    """Return SHA-256 hex hash of a token."""
    return hashlib.sha256(raw_token.encode('utf-8')).hexdigest()


def register_device(data: dict[str, Any]) -> tuple[ExternalDevice, str]:
    """Register an external device and generate its API key.
    
    Args:
        data: Registration data with device_id, registration_token, name, device_type, etc.
        
    Returns:
        (device, raw_api_key): The device instance and the raw API key (shown once).
        
    Raises:
        ValidationError: If token is invalid, expired, or device not pending.
    """
    device_id = data.get('device_id', '').strip()
    registration_token = data.get('registration_token', '').strip()
    name = data.get('name', '').strip()
    device_type = data.get('device_type', 'modem').strip()
    mqtt_client_id = data.get('mqtt_client_id', '').strip()
    metadata = data.get('metadata', {})

    if not device_id or not registration_token or not name:
        raise ValidationError({'error': 'device_id, registration_token, and name are required.'})

    token_hash = hash_token(registration_token)
    device, created = ExternalDevice.objects.get_or_create(
        device_id=device_id,
        defaults={
            'name': name,
            'device_type': device_type,
            'mqtt_client_id': mqtt_client_id,
            'metadata': metadata,
            'status': ExternalDevice.Status.PENDING,
            'registration_token_hash': token_hash,
            'registration_token_expires_at': timezone.now() + timedelta(hours=24),
        },
    )

    if created:
        _LOGGER.info('Auto-created device %s in pending state for first registration', device_id)

    if device.status != ExternalDevice.Status.PENDING:
        raise ValidationError({'error': f'Device {device_id} is not in pending state.'})

    if device.registration_token_hash != token_hash:
        raise ValidationError({'error': 'Invalid registration token.'})

    if device.registration_token_expires_at and device.registration_token_expires_at < timezone.now():
        raise ValidationError({'error': 'Registration token has expired.'})

    raw_api_key = generate_token(48)
    api_key_hash = hash_token(raw_api_key)

    device.name = name
    device.device_type = device_type
    device.mqtt_client_id = mqtt_client_id
    device.metadata = metadata
    device.api_key_hash = api_key_hash
    device.registration_token_hash = ''
    device.registration_token_expires_at = None
    device.status = ExternalDevice.Status.ACTIVE
    device.save()

    _LOGGER.info('Device %s registered successfully', device_id)
    return (device, raw_api_key)


def process_sms_request(
    device: ExternalDevice,
    recipients: list[str],
    message: str,
    priority: str = 'normal',
) -> SmsRequest:
    """Process an SMS send request from an external device.
    
    Creates SmsRequest, dispatches to the modem via apps.sms.services, and tracks per-recipient status.
    
    Args:
        device: The requesting device.
        recipients: List of phone numbers.
        message: SMS text.
        priority: Priority level (normal/high/urgent).
        
    Returns:
        The created SmsRequest instance.
    """
    if not recipients:
        raise ValidationError({'recipients': 'At least one recipient is required.'})
    
    if len(recipients) > device.max_recipients_per_request:
        raise ValidationError({
            'recipients': f'Too many recipients (max {device.max_recipients_per_request}).'
        })

    if not message.strip():
        raise ValidationError({'message': 'Message cannot be empty.'})

    request_id = f'sms_{secrets.token_urlsafe(8)}'

    with transaction.atomic():
        sms_request = SmsRequest.objects.create(
            request_id=request_id,
            device=device,
            recipients=recipients,
            message=message,
            priority=priority,
            status=SmsRequest.Status.PROCESSING,
        )

        sent_count = 0
        failed_count = 0
        modem_ix = modem_index_for_external_device(device)

        for recipient in recipients:
            try:
                outbound = OutboundSms.objects.create(
                    modem_index=modem_ix,
                    to_number=recipient.strip(),
                    text=message,
                    state=OutboundSms.State.CREATED,
                )
                dispatch_outbound_mmcli(outbound)

                if outbound.state == OutboundSms.State.SENT:
                    status = SmsRecipientStatus.Status.SENT
                    sent_count += 1
                else:
                    status = SmsRecipientStatus.Status.FAILED
                    failed_count += 1

                SmsRecipientStatus.objects.create(
                    request=sms_request,
                    phone_number=recipient,
                    status=status,
                    message_id=outbound.mm_path or '',
                    error_message=outbound.error_message or '',
                )
            except Exception as exc:
                _LOGGER.warning('Failed to send SMS to %s: %s', recipient, exc)
                failed_count += 1
                SmsRecipientStatus.objects.create(
                    request=sms_request,
                    phone_number=recipient,
                    status=SmsRecipientStatus.Status.FAILED,
                    error_message=str(exc)[:500],
                )

        sms_request.sent_count = sent_count
        sms_request.failed_count = failed_count

        if sent_count == len(recipients):
            sms_request.status = SmsRequest.Status.COMPLETED
        elif sent_count > 0:
            sms_request.status = SmsRequest.Status.PARTIAL
        else:
            sms_request.status = SmsRequest.Status.FAILED

        sms_request.save()

    _LOGGER.info('SMS request %s processed: %d sent, %d failed', request_id, sent_count, failed_count)

    if getattr(settings, 'MQTT_PUBLISH_SEND_REQUEST', False):
        chunk_sz = max(1, int(getattr(settings, 'MQTT_SEND_RECIPIENTS_CHUNK_SIZE', 50)))
        recips = list(recipients)
        total_chunks = max(1, (len(recips) + chunk_sz - 1) // chunk_sz)
        try:
            from apps.external_device.mqtt_client import publish_send_request_ephemeral

            for i in range(0, len(recips), chunk_sz):
                chunk = recips[i : i + chunk_sz]
                chunk_index = i // chunk_sz  # 0-based per hiDisheLink spec §7.3
                mqtt_payload: dict[str, Any] = {
                    'request_id': request_id,
                    'recipients': chunk,
                    'message': message,
                    'priority': priority,
                    'timestamp': timezone.now().strftime('%Y-%m-%dT%H:%M:%SZ'),
                }
                if total_chunks > 1:
                    mqtt_payload['chunk_index'] = chunk_index
                    mqtt_payload['chunk_total'] = total_chunks
                publish_send_request_ephemeral(device.device_id, mqtt_payload)
        except Exception:
            _LOGGER.warning(
                'MQTT publish_send_request_ephemeral failed for request_id=%s device=%s',
                request_id,
                device.device_id,
                exc_info=True,
            )

    return sms_request


def update_request_from_mqtt_status(request_id: str, payload: dict[str, Any]) -> None:
    """Update SmsRequest status from external device MQTT status message.
    
    Args:
        request_id: The request ID to update.
        payload: MQTT status payload with status, sent, failed, details.
    """
    try:
        sms_request = SmsRequest.objects.get(request_id=request_id)
    except SmsRequest.DoesNotExist:
        _LOGGER.warning('SmsRequest %s not found for MQTT status update', request_id)
        return

    mqtt_status_raw = (payload.get('status') or payload.get('result') or '')
    mqtt_status = str(mqtt_status_raw).strip().lower()
    if not mqtt_status:
        _LOGGER.warning('MQTT status missing status/result for request_id=%s payload_keys=%s', request_id, list(payload))
        return

    sent = payload.get('sent', 0)
    failed = payload.get('failed', 0)
    details = payload.get('details', [])

    status_map = {
        'received': SmsRequest.Status.PROCESSING,
        'success': SmsRequest.Status.COMPLETED,
        'partial': SmsRequest.Status.PARTIAL,
        'error': SmsRequest.Status.FAILED,
    }

    new_status = status_map.get(mqtt_status, SmsRequest.Status.PROCESSING)

    terminal = {
        SmsRequest.Status.COMPLETED,
        SmsRequest.Status.PARTIAL,
        SmsRequest.Status.FAILED,
    }
    incoming_terminal = mqtt_status in ('success', 'partial', 'error')
    if sms_request.status in terminal and incoming_terminal:
        _LOGGER.debug('Skipping duplicate terminal MQTT status for request_id=%s', request_id)
        return

    with transaction.atomic():
        sms_request.status = new_status
        sms_request.sent_count = sent
        sms_request.failed_count = failed
        sms_request.save()

        for detail in details:
            recipient = detail.get('recipient', '')
            status_str = detail.get('status', 'failed')
            message_id = detail.get('message_id', '')
            error_msg = detail.get('error') or detail.get('error_message', '')

            if not recipient:
                continue

            recipient_status = SmsRecipientStatus.Status.SENT if status_str == 'sent' else SmsRecipientStatus.Status.FAILED

            SmsRecipientStatus.objects.update_or_create(
                request=sms_request,
                phone_number=recipient,
                defaults={
                    'status': recipient_status,
                    'message_id': message_id,
                    'error_message': error_msg,
                },
            )

    _LOGGER.info('Updated SMS request %s from MQTT: status=%s, sent=%d, failed=%d', request_id, new_status, sent, failed)


def persist_modem_catalog_from_mqtt(kind: str, payload: dict[str, Any]) -> MqttGatewayCatalogEntry:
    """Store gateway-published modem snapshot or contacts JSON for operator review."""
    return MqttGatewayCatalogEntry.objects.create(kind=kind, payload=payload)


def _mqtt_first_nonempty_str(payload: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        val = payload.get(key)
        if val is None:
            continue
        s = val.strip() if isinstance(val, str) else str(val).strip()
        if s:
            return s
    return ''


def persist_inbox_from_mqtt(device: ExternalDevice, payload: dict[str, Any]) -> InboxMessage:
    """Persist an incoming SMS message from external device MQTT inbox topic.
    
    Args:
        device: The device reporting the message.
        payload: MQTT inbox payload with message_id, sender, body, timestamp.
        Aliases (hiDisheLink): id, from, text|message, received_at.
        
    Returns:
        The created or existing InboxMessage.
    """
    message_id = _mqtt_first_nonempty_str(payload, ('message_id', 'id'))
    sender = _mqtt_first_nonempty_str(payload, ('sender', 'from'))
    if payload.get('body') is not None:
        body = payload.get('body')
    elif payload.get('text') is not None:
        body = payload.get('text')
    elif payload.get('message') is not None:
        body = payload.get('message')
    else:
        body = ''
    body = '' if body is None else str(body)

    timestamp_raw = payload.get('timestamp')
    if timestamp_raw is None or (isinstance(timestamp_raw, str) and not timestamp_raw.strip()):
        timestamp_raw = payload.get('received_at')
    timestamp_str = '' if timestamp_raw is None else str(timestamp_raw).strip()

    if not message_id or not sender:
        _LOGGER.warning('Invalid inbox payload: missing message_id or sender')
        raise ValidationError({'error': 'message_id and sender are required.'})

    try:
        from django.utils.dateparse import parse_datetime
        received_at = parse_datetime(timestamp_str) if timestamp_str else timezone.now()
        if not received_at:
            received_at = timezone.now()
    except Exception:
        received_at = timezone.now()

    inbox_msg, created = InboxMessage.objects.get_or_create(
        message_id=message_id,
        defaults={
            'device': device,
            'sender': sender,
            'body': body,
            'received_at': received_at,
        },
    )

    if created:
        _LOGGER.info('Inbox message %s persisted from device %s', message_id, device.device_id)
    else:
        _LOGGER.info('Inbox message %s already exists', message_id)

    return inbox_msg


def sync_inbox_from_modem_store(device: ExternalDevice) -> None:
    """Mirror mmcli inbound rows (apps.sms.InboundSms) into external device inbox.

    This keeps `/api/v1/sms/inbox/` useful even when inbound SMS arrive via the
    internal modem watcher instead of external-device MQTT topics.
    
    If device.metadata['modem_index'] is explicitly set, filters InboundSms by that index.
    Otherwise, includes all InboundSms rows regardless of modem_index.
    """
    from apps.sms.models import InboundSms

    # Check if modem_index is explicitly set in metadata
    metadata = device.metadata or {}
    modem_index_from_meta = metadata.get('modem_index')
    
    if modem_index_from_meta is not None:
        # Filter by specific modem_index
        try:
            modem_index = int(modem_index_from_meta)
        except (TypeError, ValueError):
            _LOGGER.warning(
                'Inbox sync device=%s metadata.modem_index=%r invalid; falling back to all InboundSms',
                device.device_id,
                modem_index_from_meta,
            )
            qs = InboundSms.objects.all()
            filter_description = 'all'
        else:
            qs = InboundSms.objects.filter(modem_index=modem_index)
            filter_description = f'modem_index={modem_index}'
    else:
        # No modem_index in metadata → include all InboundSms
        qs = InboundSms.objects.all()
        filter_description = 'all (no metadata.modem_index)'
        _LOGGER.info(
            'Inbox sync device=%s has no metadata.modem_index; including all InboundSms',
            device.device_id,
        )

    total_inbound = qs.count()
    inbound_rows = qs.order_by('-created_at')[:500]
    created_count = 0
    existing_count = 0
    patched_count = 0

    for inbound in inbound_rows:
        # Include device.pk in message_id to support multiple devices (message_id is unique=True)
        message_id = f'mmcli_{inbound.pk}_dev_{device.pk}'
        inbox_msg, created = InboxMessage.objects.get_or_create(
            message_id=message_id,
            defaults={
                'device': device,
                'sender': inbound.from_number,
                'body': inbound.text,
                'received_at': inbound.created_at,
                # No MQTT ACK exists for mirrored modem rows.
                'ack_sent': True,
            },
        )
        if created:
            created_count += 1
        else:
            existing_count += 1
            patched: dict[str, str] = {}
            if not inbox_msg.sender and inbound.from_number:
                patched['sender'] = inbound.from_number
            if not inbox_msg.body and inbound.text:
                patched['body'] = inbound.text
            if patched:
                for key, val in patched.items():
                    setattr(inbox_msg, key, val)
                inbox_msg.save(update_fields=list(patched.keys()))
                patched_count += 1

    if total_inbound == 0:
        available_modem_indexes = sorted(
            set(InboundSms.objects.values_list('modem_index', flat=True).distinct())
        )
        _LOGGER.warning(
            'Inbox sync device=%s filter=%s found no InboundSms. '
            'Available modem indexes in store=%s',
            device.device_id,
            filter_description,
            available_modem_indexes,
        )
    else:
        _LOGGER.info(
            'Inbox sync device=%s filter=%s inbound_total=%s scanned=%s mirrored_created=%s mirrored_existing=%s patched_body=%s',
            device.device_id,
            filter_description,
            total_inbound,
            len(inbound_rows),
            created_count,
            existing_count,
            patched_count,
        )


def _mirror_to_device(inbound, device: ExternalDevice) -> tuple[bool, bool, bool]:
    """
    Mirror InboundSms to a single device.

    Returns:
        (created, skipped, emit_mqtt_delivery): ``emit_mqtt_delivery`` is True when the mirror
        transitioned to content worth notifying (new row with sender/body or patch filling them).
    """
    _LOGGER.debug(
        'sync_single_inbound_to_all_devices: processing device=%s pk=%s status=%s',
        device.device_id,
        device.pk,
        device.status,
    )

    metadata = device.metadata or {}

    if metadata.get('modem_inbox_mirror') is False:
        _LOGGER.debug(
            'sync_single_inbound_to_all_devices: skipping device=%s (metadata.modem_inbox_mirror=false)',
            device.device_id,
        )
        return (False, True, False)

    # Note: inbound_should_skip_modem_mirror check moved to caller
    # to avoid N×2 database queries (now checked once before device loop)

    device_modem_index = metadata.get('modem_index')

    if device_modem_index is not None:
        try:
            if int(device_modem_index) != inbound.modem_index:
                _LOGGER.debug(
                    'sync_single_inbound_to_all_devices: skipping device=%s (modem_index mismatch: %s != %s)',
                    device.device_id,
                    device_modem_index,
                    inbound.modem_index,
                )
                return (False, True, False)  # not created, skipped
        except (TypeError, ValueError):
            _LOGGER.warning(
                'sync_single_inbound_to_all_devices: device=%s has invalid modem_index=%r, treating as no filter',
                device.device_id,
                device_modem_index,
            )

    message_id = f'mmcli_{inbound.pk}_dev_{device.pk}'

    _LOGGER.debug(
        'sync_single_inbound_to_all_devices: attempting get_or_create for device=%s message_id=%s',
        device.device_id,
        message_id,
    )

    inbox_msg, created = InboxMessage.objects.get_or_create(
        message_id=message_id,
        defaults={
            'device': device,
            'sender': inbound.from_number,
            'body': inbound.text,
            'received_at': inbound.created_at,
            'ack_sent': True,
        },
    )

    patched_fields: list[str] = []
    if created:
        _LOGGER.debug(
            'sync_single_inbound_to_all_devices: created InboxMessage for device=%s',
            device.device_id,
        )
    else:
        # Patch blank fields in case InboxMessage was created before mmcli parsed the content.
        patched = {}
        if not inbox_msg.sender and inbound.from_number:
            patched['sender'] = inbound.from_number
        if not inbox_msg.body and inbound.text:
            patched['body'] = inbound.text
        if patched:
            for k, v in patched.items():
                setattr(inbox_msg, k, v)
            inbox_msg.save(update_fields=list(patched.keys()))
            patched_fields = list(patched.keys())
            _LOGGER.debug(
                'sync_single_inbound_to_all_devices: patched InboxMessage for device=%s fields=%s',
                device.device_id, patched_fields,
            )
        else:
            _LOGGER.debug(
                'sync_single_inbound_to_all_devices: InboxMessage already exists for device=%s',
                device.device_id,
            )

    emit_mqtt_delivery = False
    has_sender = bool((inbox_msg.sender or '').strip())
    has_body = bool((inbox_msg.body or '').strip())
    if has_sender or has_body:
        if created and (has_body or has_sender):
            emit_mqtt_delivery = True
        elif patched_fields and ('body' in patched_fields or 'sender' in patched_fields):
            emit_mqtt_delivery = True

    return (created, False, emit_mqtt_delivery)  # created, not skipped


def sync_single_inbound_to_all_devices(inbound) -> None:
    """Mirror a single InboundSms to active ExternalDevices.

    Called by the post_save signal when InboundSms is created.
    Processes devices sequentially — SQLite does not support concurrent
    writes from multiple threads, so ThreadPoolExecutor was removed to
    prevent "database table is locked" errors.

    For each active device:

    - Skips when ``device.metadata['modem_inbox_mirror'] is False`` (use on
      secondary registrations that should not receive the same mmcli mirror).
    - Skips when any recent ``inbox_manual_*`` row has the same sender/body, or when
      the inbound likely echoes a recent outbound send (admin manual + modem send).
    - If ``device.metadata['modem_index']`` is set, only mirror when it matches
      ``inbound.modem_index``.
    - If ``modem_index`` is not set, mirror unconditionally (subject to the rules above).
    """
    # Check dedup rules once upfront (eliminates N queries per device)
    skip_all_due_to_dedup = inbound_should_skip_modem_mirror(inbound)
    if skip_all_due_to_dedup:
        _LOGGER.info(
            'sync_single_inbound_to_all_devices: skipping all devices for inbound_pk=%s '
            '(recent manual inbox or outbound echo)',
            inbound.pk,
        )
        return
    
    active_devices = list(ExternalDevice.objects.filter(status=ExternalDevice.Status.ACTIVE))
    active_count = len(active_devices)

    _LOGGER.debug(
        'sync_single_inbound_to_all_devices: inbound pk=%s modem_index=%s, found %s active devices',
        inbound.pk,
        inbound.modem_index,
        active_count,
    )

    if active_count == 0:
        return

    created_count = 0
    skipped_count = 0
    mirrored_for_delivery: list[tuple[str, int]] = []
    mqtt_mode = getattr(settings, 'MQTT_MODEM_INBOX_DELIVERY_MODE', 'broadcast')

    for device in active_devices:
        try:
            created, skipped, emit_delivery = _mirror_to_device(inbound, device)
            if created:
                created_count += 1
            if skipped:
                skipped_count += 1
            if emit_delivery:
                mirrored_for_delivery.append((device.device_id, device.pk))
            if (
                getattr(settings, 'MQTT_PUBLISH_MODEM_INBOX', False)
                and emit_delivery
                and mqtt_mode == 'per_device'
            ):
                try:
                    from apps.external_device.mqtt_client import publish_modem_inbox_delivery_ephemeral

                    publish_modem_inbox_delivery_ephemeral(
                        device.device_id,
                        {
                            'message_id': f'mmcli_{inbound.pk}_dev_{device.pk}',
                            'sender': inbound.from_number or '',
                            'body': inbound.text or '',
                            'received_at': inbound.created_at.isoformat(),
                        },
                    )
                    _LOGGER.info(
                        'MQTT modem inbox_delivery (per_device) published device=%s inbound_pk=%s',
                        device.device_id,
                        inbound.pk,
                    )
                except Exception:
                    _LOGGER.warning(
                        'MQTT publish_modem_inbox_delivery_ephemeral failed device=%s inbound_pk=%s',
                        device.device_id,
                        inbound.pk,
                        exc_info=True,
                    )
        except Exception as exc:
            _LOGGER.exception(
                'Mirror to device=%s failed for InboundSms pk=%s: %s',
                device.device_id, inbound.pk, exc,
            )

    if (
        getattr(settings, 'MQTT_PUBLISH_MODEM_INBOX', False)
        and mirrored_for_delivery
        and mqtt_mode == 'broadcast'
    ):
        try:
            from apps.external_device.mqtt_client import publish_modem_inbox_broadcast_ephemeral

            mirrored_device_ids = [did for did, _pk in mirrored_for_delivery]
            device_message_ids = {
                did: f'mmcli_{inbound.pk}_dev_{pk}' for did, pk in mirrored_for_delivery
            }
            publish_modem_inbox_broadcast_ephemeral(
                inbound.modem_index,
                {
                    'message_id': f'mmcli_{inbound.pk}',
                    'sender': inbound.from_number or '',
                    'body': inbound.text or '',
                    'received_at': inbound.created_at.isoformat(),
                    'modem_index': inbound.modem_index,
                    'mirrored_device_ids': mirrored_device_ids,
                    'device_message_ids': device_message_ids,
                },
            )
            _LOGGER.info(
                'MQTT modem inbox_delivery (broadcast) published modem_index=%s inbound_pk=%s devices=%s',
                inbound.modem_index,
                inbound.pk,
                mirrored_device_ids,
            )
        except Exception:
            _LOGGER.warning(
                'MQTT publish_modem_inbox_broadcast_ephemeral failed inbound_pk=%s',
                inbound.pk,
                exc_info=True,
            )

    _LOGGER.info(
        'InboundSms pk=%s modem_index=%s mirrored_to=%s skipped=%s (total_active=%s)',
        inbound.pk,
        inbound.modem_index,
        created_count,
        skipped_count,
        active_count,
    )


def external_device_delete_preview(device: ExternalDevice) -> dict[str, int]:
    """Counts of rows removed by :func:`delete_external_device_and_dependencies` (read-only)."""
    pk = device.device_id
    return {
        'sms_requests': SmsRequest.objects.filter(device=device).count(),
        'inbox_messages': InboxMessage.objects.filter(device=device).count(),
        'device_sessions': DeviceSession.objects.filter(device=device).count(),
        'health_telemetry': DeviceHealthTelemetry.objects.filter(device=device).count(),
        'hidishelink_devices': HiDishelinkDevice.objects.filter(pk=pk).count(),
        'external_devices': 1,
    }


def delete_external_device_and_dependencies(device: ExternalDevice) -> dict[str, int]:
    """Remove ``ExternalDevice`` and rows blocked by ``PROTECT`` (SMS/inbox), sessions, telemetry, HiDisheLink row."""
    pk = device.device_id
    stats: dict[str, int] = {}
    with transaction.atomic():
        n, _ = SmsRequest.objects.filter(device=device).delete()
        stats['sms_requests'] = n

        n, _ = InboxMessage.objects.filter(device=device).delete()
        stats['inbox_messages'] = n

        n, _ = DeviceSession.objects.filter(device=device).delete()
        stats['device_sessions'] = n

        n, _ = DeviceHealthTelemetry.objects.filter(device=device).delete()
        stats['health_telemetry'] = n

        n, _ = HiDishelinkDevice.objects.filter(pk=pk).delete()
        stats['hidishelink_devices'] = n

        device.delete()
        stats['external_devices'] = 1

    _LOGGER.info(
        'ExternalDevice cascade delete device_id=%s stats=%s',
        pk,
        stats,
    )
    return stats


# Remote hiDisheLink bridge handlers (section 10 of hiDisheLink architecture doc)


def handle_remote_sms_send(remote_client, payload: dict[str, Any]) -> None:
    """Handle SMS send request from remote hiDisheLink broker (section 10 handler).
    
    Implements the contract from section 10.10 (checklist item 3-4):
    1. Validate payload
    2. Publish ACK imediato: status=received
    3. Agregar chunks se chunk_index/chunk_total presentes
    4. Dispatch via mmcli para cada recipient
    5. Publicar status final: success/partial/error com details[]
    
    Args:
        remote_client: RemoteHiDishelinkClient instance
        payload: SMS send payload from hiDisheLink server
    """
    from typing import TYPE_CHECKING
    if TYPE_CHECKING:
        from .mqtt_client import RemoteHiDishelinkClient
        remote_client: RemoteHiDishelinkClient
    
    request_id = payload.get('request_id', '').strip()
    if not request_id:
        _LOGGER.warning('Remote SMS send missing request_id: %s', payload)
        return
    
    _LOGGER.info('Remote SMS send received request_id=%s', request_id)
    
    # Step 1: Validate payload
    recipients = payload.get('recipients')
    if not isinstance(recipients, list) or not recipients:
        _LOGGER.warning('Remote SMS send invalid/empty recipients request_id=%s', request_id)
        remote_client.publish_sms_status(
            request_id,
            'error',
            {
                'sent': 0,
                'failed': 0,
                'details': [{'status': 'failed', 'error': 'Invalid or empty recipients list'}],
            },
        )
        return
    
    message = payload.get('message', '').strip()
    if not message:
        _LOGGER.warning('Remote SMS send missing message request_id=%s', request_id)
        remote_client.publish_sms_status(
            request_id,
            'error',
            {
                'sent': 0,
                'failed': 0,
                'details': [{'status': 'failed', 'error': 'Missing message text'}],
            },
        )
        return
    
    # Step 2: Publish ACK imediato (status=received)
    remote_client.publish_sms_status(
        request_id,
        'received',
        {},  # Minimal payload for received ACK
    )
    
    # Step 3: Handle chunking (if present)
    # For now, process immediately. Chunking aggregation will be implemented in to-do #4
    chunk_index = payload.get('chunk_index')
    chunk_total = payload.get('chunk_total')
    if chunk_index is not None and chunk_total is not None:
        _LOGGER.info(
            'Remote SMS send chunked request_id=%s chunk=%s/%s recipients=%s',
            request_id,
            chunk_index,
            chunk_total,
            len(recipients),
        )
        # TODO: Implement buffering and aggregation (to-do #4: chunking-aggregation)
        # For now, process each chunk immediately
    
    # Step 4: Dispatch via mmcli para cada recipient
    details = []
    sent_count = 0
    failed_count = 0
    
    # Determine modem index (use default from settings)
    from apps.sms.mmcli_client import resolve_modem_mmcli_index
    modem_idx = resolve_modem_mmcli_index(int(getattr(settings, 'MODEM_MMCLI_INDEX', 0)))
    
    for recipient in recipients:
        recipient_num = str(recipient).strip()
        if not recipient_num:
            details.append({
                'recipient': recipient_num,
                'status': 'failed',
                'error': 'Empty recipient number',
            })
            failed_count += 1
            continue
        
        try:
            # Create OutboundSms
            outbound = OutboundSms.objects.create(
                modem_index=modem_idx,
                to_number=recipient_num,
                text=message,
                state=OutboundSms.State.CREATED,
            )
            
            # Dispatch via mmcli
            dispatch_outbound_mmcli(outbound)
            
            # Reload to get updated state
            outbound.refresh_from_db()
            
            if outbound.state == OutboundSms.State.SENT:
                details.append({
                    'recipient': recipient_num,
                    'status': 'sent',
                    'message_id': outbound.mm_path or f'outbound_{outbound.pk}',
                })
                sent_count += 1
            else:
                details.append({
                    'recipient': recipient_num,
                    'status': 'failed',
                    'error': outbound.error_message or 'Unknown error',
                })
                failed_count += 1
        
        except Exception as exc:
            _LOGGER.exception('Remote SMS dispatch failed recipient=%s request_id=%s', recipient_num, request_id)
            details.append({
                'recipient': recipient_num,
                'status': 'failed',
                'error': str(exc)[:200],
            })
            failed_count += 1
    
    # Step 5: Publicar status final
    if failed_count == 0:
        final_status = 'success'
    elif sent_count == 0:
        final_status = 'error'
    else:
        final_status = 'partial'
    
    remote_client.publish_sms_status(
        request_id,
        final_status,
        {
            'sent': sent_count,
            'failed': failed_count,
            'details': details,
        },
    )
    
    _LOGGER.info(
        'Remote SMS send completed request_id=%s status=%s sent=%s failed=%s',
        request_id,
        final_status,
        sent_count,
        failed_count,
    )


def publish_inbound_to_remote(inbound, remote_client) -> bool:
    """Publish InboundSms to remote hiDisheLink broker (section 6 of spec).
    
    Called from post_save signal when bridge mode is enabled.
    Implements inbox publishing per section 10.10 (checklist item 7).
    
    Args:
        inbound: InboundSms instance
        remote_client: RemoteHiDishelinkClient instance
        
    Returns:
        True if published successfully
    """
    from typing import TYPE_CHECKING
    if TYPE_CHECKING:
        from .mqtt_client import RemoteHiDishelinkClient
        remote_client: RemoteHiDishelinkClient
    
    # Skip if not ready (no sender or body yet)
    if not inbound_ready_for_inbox_mirror(inbound):
        _LOGGER.debug('Inbound SMS not ready for remote publish pk=%s', inbound.pk)
        return False
    
    # Skip if should be filtered (echo or manual duplicate)
    if inbound_should_skip_modem_mirror(inbound):
        _LOGGER.debug('Inbound SMS skipped for remote publish pk=%s (dedup filter)', inbound.pk)
        return False
    
    # Generate message_id
    message_id = f'hidw_inbound_{inbound.pk}_{inbound.modem_index}'
    sender = inbound.from_number or ''
    body = inbound.text or ''
    timestamp = inbound.created_at.isoformat()
    
    success = remote_client.publish_sms_inbox(message_id, sender, body, timestamp)
    
    if success:
        _LOGGER.info(
            'Published InboundSms to remote broker pk=%s message_id=%s sender=%s',
            inbound.pk,
            message_id,
            sender,
        )
    else:
        _LOGGER.warning(
            'Failed to publish InboundSms to remote broker pk=%s message_id=%s',
            inbound.pk,
            message_id,
        )
    
    return success


def publish_inbound_to_remote_ephemeral(inbound) -> bool:
    """Publish InboundSms to remote hiDisheLink broker via one-off MQTT connection.

    Used when ``_global_remote_client`` is unavailable (e.g. SMS watcher process separate
    from ``run_mqtt_gateway``). Uses cached ``HiDishelinkDevice.mqtt_config``.
    """
    from .mqtt_client import (
        _publish_json_ephemeral,
        device_topic_from_flat_config,
        ephemeral_connection_from_flat,
        resolve_remote_bridge_target,
    )

    if not inbound_ready_for_inbox_mirror(inbound):
        _LOGGER.debug('Inbound SMS not ready for remote ephemeral publish pk=%s', inbound.pk)
        return False

    if inbound_should_skip_modem_mirror(inbound):
        _LOGGER.debug('Inbound SMS skipped for remote ephemeral publish pk=%s', inbound.pk)
        return False

    device_id, mqtt_cfg = resolve_remote_bridge_target()
    if not device_id or not mqtt_cfg:
        _LOGGER.warning(
            'Remote inbox ephemeral: no cached mqtt-config for bridge device (inbound_pk=%s)',
            inbound.pk,
        )
        return False

    topic = device_topic_from_flat_config(
        mqtt_cfg,
        'TOPIC_SMS_INBOX',
        '{prefix}/{sanitized}/sms/inbox',
        device_id,
    )
    conn = ephemeral_connection_from_flat(mqtt_cfg)
    conn['MQTT_QOS'] = 1

    message_id = f'hidw_inbound_{inbound.pk}_{inbound.modem_index}'
    payload = {
        'message_id': message_id,
        'sender': inbound.from_number or '',
        'body': inbound.text or '',
        'timestamp': inbound.created_at.isoformat(),
    }

    success = _publish_json_ephemeral(topic, payload, conn)
    if success:
        _LOGGER.info(
            'Published InboundSms to remote broker (ephemeral) pk=%s message_id=%s device=%s',
            inbound.pk,
            message_id,
            device_id,
        )
    else:
        _LOGGER.warning(
            'Failed remote ephemeral inbox publish pk=%s message_id=%s topic=%s',
            inbound.pk,
            message_id,
            topic,
        )
    return success
