"""Idempotent HiDishelinkDevice bootstrap from detected modem identity."""

from __future__ import annotations

import logging
from typing import Any

from django.conf import settings
from django.utils import timezone

from apps.sms.mmcli_client import MMCLIClient

from .modem_identity import format_modem_notes, probe_modem_identity
from .models import ExternalDevice, HiDishelinkDevice

_LOGGER = logging.getLogger(__name__)


def ensure_hidishelink_device_from_modem(
    modem_index: int,
    *,
    dry_run: bool = False,
    client: MMCLIClient | None = None,
) -> dict[str, Any]:
    """Create or update HiDishelinkDevice (+ ExternalDevice) from mmcli probe."""
    identity = probe_modem_identity(modem_index, client=client)
    device_id = identity.get('phone_number') or ''
    if not device_id:
        _LOGGER.warning(
            'ensure_hidishelink_device: no phone number from modem_index=%s; skipping',
            modem_index,
        )
        return {'skipped': True, 'reason': 'no_device_id', 'identity': identity}

    api_url = getattr(settings, 'HIDISHELINK_API_URL', '').strip().rstrip('/')
    probed_at = timezone.now().isoformat()
    notes = format_modem_notes(identity, probed_at=probed_at)
    metadata = {
        'manufacturer': identity.get('manufacturer') or '',
        'model': identity.get('model') or '',
        'imei': identity.get('imei') or '',
        'firmware': identity.get('firmware') or '',
        'modem_index': modem_index,
        'probed_at': probed_at,
    }

    if dry_run:
        return {
            'skipped': False,
            'dry_run': True,
            'device_id': device_id,
            'notes': notes,
            'identity': identity,
        }

    hid, created = HiDishelinkDevice.objects.get_or_create(
        device_id=device_id,
        defaults={
            'api_url': api_url,
            'status': HiDishelinkDevice.Status.UNCONFIGURED,
            'sync_external_device': True,
            'notes': notes,
        },
    )

    update_fields: list[str] = []
    if not created:
        if notes != hid.notes:
            hid.notes = notes
            update_fields.append('notes')
        if not (hid.api_url or '').strip() and api_url:
            hid.api_url = api_url
            update_fields.append('api_url')
        if update_fields:
            hid.save(update_fields=update_fields + ['updated_at'])

    external_created = False
    if hid.sync_external_device:
        ext, external_created = ExternalDevice.objects.get_or_create(
            device_id=device_id,
            defaults={
                'name': device_id,
                'status': ExternalDevice.Status.ACTIVE,
                'device_type': 'modem',
                'metadata': metadata,
            },
        )
        if not external_created:
            ext_meta = dict(ext.metadata or {})
            changed = False
            for key, val in metadata.items():
                if ext_meta.get(key) != val:
                    ext_meta[key] = val
                    changed = True
            if changed:
                ext.metadata = ext_meta
                ext.save(update_fields=['metadata', 'updated_at'])

    return {
        'skipped': False,
        'created': created,
        'device_id': device_id,
        'external_created': external_created,
        'status': hid.status,
        'identity': identity,
    }
