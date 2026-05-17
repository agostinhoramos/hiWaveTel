"""Business logic for `/api/sms/device/` sessions and registration variants."""

from __future__ import annotations

import logging
import secrets
from datetime import timedelta
from typing import Any

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .authentication import hash_api_key
from .models import DeviceSession, ExternalDevice
from .services import generate_token, hash_token, register_device

_LOGGER = logging.getLogger(__name__)


def session_ttl() -> timedelta:
    return timedelta(hours=int(getattr(settings, 'HIDISHELINK_SESSION_TTL_HOURS', 24)))


def create_device_session(device: ExternalDevice) -> DeviceSession:
    raw_sid = secrets.token_urlsafe(48)[:127]
    sid = f's_{raw_sid}'
    return DeviceSession.objects.create(
        session_id=sid,
        device=device,
        expires_at=timezone.now() + session_ttl(),
    )


def refresh_device_session(device_id: str, session_id: str) -> tuple[DeviceSession | None, str | None]:
    """Return (session, error_message)."""
    sid = session_id.strip()
    if not sid:
        return None, 'session_id is required.'
    try:
        sess = DeviceSession.objects.select_related('device').get(session_id=sid, device_id=device_id)
    except DeviceSession.DoesNotExist:
        return None, 'Invalid session.'
    if sess.revoked_at is not None:
        return None, 'Session revoked.'
    if sess.expires_at <= timezone.now():
        return None, 'Session expired.'
    sess.expires_at = timezone.now() + session_ttl()
    sess.save(update_fields=['expires_at'])
    return sess, None


def revoke_device_session(device_id: str, session_id: str) -> bool:
    sid = session_id.strip()
    updated = DeviceSession.objects.filter(
        session_id=sid,
        device_id=device_id,
        revoked_at__isnull=True,
    ).update(revoked_at=timezone.now())
    return updated > 0


def active_sessions_count(device: ExternalDevice) -> int:
    now = timezone.now()
    return DeviceSession.objects.filter(
        device=device,
        revoked_at__isnull=True,
        expires_at__gt=now,
    ).count()


def authenticate_device_login(device_id: str, api_key: str) -> tuple[ExternalDevice | None, str | None]:
    """Validate api_key for login; return (device, None) or (None, error_code).

    error_code: invalid_credentials | pending | inactive
    """
    did = device_id.strip()
    key = api_key.strip()
    if not did or not key:
        return None, 'invalid_credentials'
    try:
        device = ExternalDevice.objects.get(pk=did)
    except ExternalDevice.DoesNotExist:
        return None, 'invalid_credentials'

    if device.status == ExternalDevice.Status.PENDING:
        return None, 'pending'

    if device.status != ExternalDevice.Status.ACTIVE:
        return None, 'inactive'

    if not device.api_key_hash or hash_api_key(key) != device.api_key_hash:
        return None, 'invalid_credentials'

    return device, None


def register_device_android_payload(body: dict[str, Any]) -> tuple[ExternalDevice, str]:
    """Map Android register JSON (spec 3.3) onto ``register_device``."""
    device_model = str(body.get('device_model') or '').strip()
    android_version = str(body.get('android_version') or '').strip()
    app_version = str(body.get('app_version') or '').strip()
    metadata: dict[str, Any] = {}
    if device_model:
        metadata['device_model'] = device_model
    if android_version:
        metadata['android_version'] = android_version
    if app_version:
        metadata['app_version'] = app_version

    name = device_model or str(body.get('device_id') or '').strip()
    reg_data = {
        'device_id': body.get('device_id', ''),
        'registration_token': body.get('registration_token', ''),
        'name': name,
        'device_type': str(body.get('device_type') or 'modem').strip() or 'modem',
        'metadata': metadata,
    }
    return register_device(reg_data)


@transaction.atomic
def issue_pending_device_api_key(device_id: str) -> tuple[ExternalDevice | None, str]:
    """POST get-pending-key. Returns (device, outcome).

    outcome: ok | not_found | not_pending
    """
    did = device_id.strip()
    if not did:
        return None, 'not_found'
    try:
        device = ExternalDevice.objects.select_for_update().get(pk=did)
    except ExternalDevice.DoesNotExist:
        return None, 'not_found'

    if device.status != ExternalDevice.Status.PENDING:
        return None, 'not_pending'

    if device.api_key_hash:
        return None, 'not_pending'

    raw_api_key = generate_token(48)
    device.api_key_hash = hash_api_key(raw_api_key)
    device.registration_token_hash = ''
    device.registration_token_expires_at = None
    device.status = ExternalDevice.Status.ACTIVE
    device.save(
        update_fields=[
            'api_key_hash',
            'registration_token_hash',
            'registration_token_expires_at',
            'status',
            'updated_at',
        ]
    )

    device._pending_raw_api_key = raw_api_key  # type: ignore[attr-defined]
    return device, 'ok'
