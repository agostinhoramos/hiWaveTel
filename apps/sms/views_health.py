"""Operational health probes (Django-native JSON; not routed through DRF)."""

from __future__ import annotations

import logging
from typing import Any

from django.conf import settings
from django.http import HttpRequest, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET

from .mmcli_client import MMCLIClient, MmcliError, resolve_modem_mmcli_index

_LOGGER = logging.getLogger(__name__)


@require_GET
@csrf_exempt
def health_modem_manager(request: HttpRequest) -> JsonResponse:  # noqa: ARG001
    """Return modem / mmcli status JSON for probes (best-effort, no secrets)."""

    payload: dict[str, Any] = {
        'ok': False,
        'modem_mmcli_indices': [],
        'settings_modem_mmcli_index': settings.MODEM_MMCLI_INDEX,
        'modem_mmcli_ping_ok': False,
        'mmcli_notes': '',
    }

    configured = getattr(settings, 'MODEM_MMCLI_INDEX', None)
    timeout = float(getattr(settings, 'HIWAVE_MMCLI_HEALTH_TIMEOUT', 15.0))

    try:
        client = MMCLIClient(timeout_sec=timeout)
        indices = client.list_modem_indices()
        payload['modem_mmcli_indices'] = indices

        if not indices:
            payload['mmcli_notes'] = 'ModemManager returned zero modems via mmcli -L.'
            return JsonResponse(payload, status=503)

        try:
            effective = resolve_modem_mmcli_index(int(configured), client=client)
        except MmcliError as exc:
            payload['mmcli_notes'] = str(exc)[:512]
            return JsonResponse(payload, status=503)

        payload['effective_modem_mmcli_index'] = effective
        if effective != configured:
            payload['mmcli_notes'] = (
                f'MODEM_MMCLI_INDEX={configured} not in {indices}; using primary modem {effective}.'
            )

        ping_ok, ping_text = client.modem_ping(effective)
        payload['modem_mmcli_ping_ok'] = ping_ok
        if not ping_ok and ping_text:
            payload['mmcli_notes'] = ping_text.replace('\n', ' ')[:2000]

        payload['ok'] = ping_ok

        http_status = 200 if payload['ok'] else 503
        return JsonResponse(payload, status=http_status)

    except MmcliError as exc:
        payload['mmcli_notes'] = str(exc)[:512]
        _LOGGER.warning('health_modem_manager mmcli failure: %s', exc)
        return JsonResponse(payload, status=503)
    except OSError as exc:
        payload['mmcli_notes'] = f'Modem health probe failed ({exc.__class__.__name__}).'
        _LOGGER.warning('health_modem_manager OS error: %s', exc)
        return JsonResponse(payload, status=503)
    except Exception as exc:
        _LOGGER.exception('health_modem_manager unexpected error')
        payload['mmcli_notes'] = f'Unexpected probe error ({exc.__class__.__name__}).'
        return JsonResponse(payload, status=503)
