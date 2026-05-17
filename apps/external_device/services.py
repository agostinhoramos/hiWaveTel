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

from .models import ExternalDevice, InboxMessage, SmsRecipientStatus, SmsRequest

_LOGGER = logging.getLogger(__name__)


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

        for recipient in recipients:
            try:
                outbound = OutboundSms.objects.create(
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
        mqtt_payload: dict[str, Any] = {
            'request_id': request_id,
            'recipients': list(recipients),
            'message': message,
            'priority': priority,
        }
        try:
            from apps.external_device.mqtt_client import publish_send_request_ephemeral

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

    mqtt_status = payload.get('status', '').lower()
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

    with transaction.atomic():
        sms_request.status = new_status
        sms_request.sent_count = sent
        sms_request.failed_count = failed
        sms_request.save()

        for detail in details:
            recipient = detail.get('recipient', '')
            status_str = detail.get('status', 'failed')
            message_id = detail.get('message_id', '')
            error_msg = detail.get('error_message', '')

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


def persist_inbox_from_mqtt(device: ExternalDevice, payload: dict[str, Any]) -> InboxMessage:
    """Persist an incoming SMS message from external device MQTT inbox topic.
    
    Args:
        device: The device reporting the message.
        payload: MQTT inbox payload with message_id, sender, body, timestamp.
        
    Returns:
        The created or existing InboxMessage.
    """
    message_id = payload.get('message_id', '').strip()
    sender = payload.get('sender', '').strip()
    body = payload.get('body', '')
    timestamp_str = payload.get('timestamp', '')

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
    
    for inbound in inbound_rows:
        # Include device.pk in message_id to support multiple devices (message_id is unique=True)
        message_id = f'mmcli_{inbound.pk}_dev_{device.pk}'
        _, created = InboxMessage.objects.get_or_create(
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
            'Inbox sync device=%s filter=%s inbound_total=%s scanned=%s mirrored_created=%s mirrored_existing=%s',
            device.device_id,
            filter_description,
            total_inbound,
            len(inbound_rows),
            created_count,
            existing_count,
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
    """Mirror a single InboundSms to all active ExternalDevices.

    Called by the post_save signal when InboundSms is created.
    Processes devices sequentially — SQLite does not support concurrent
    writes from multiple threads, so ThreadPoolExecutor was removed to
    prevent "database table is locked" errors.

    For each active device:
    - If device.metadata['modem_index'] is set, only mirror when it matches
      inbound.modem_index.
    - If device.metadata['modem_index'] is not set, mirror unconditionally.
    """
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
