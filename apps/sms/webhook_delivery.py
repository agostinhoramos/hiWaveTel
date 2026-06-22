"""HTTP webhook delivery for inbound SMS."""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from typing import Any

from django.conf import settings

_LOGGER = logging.getLogger(__name__)


def get_active_webhook_urls(modem_index: int) -> list[str]:
    """Enabled webhook URLs registered for the given modem index."""
    from apps.sms.models import InboundWebhook

    urls: list[str] = []
    seen: set[str] = set()
    for row in InboundWebhook.objects.filter(
        enabled=True,
        modem_index=modem_index,
    ).order_by('id'):
        u = (row.url or '').strip()
        if u and u not in seen:
            seen.add(u)
            urls.append(u)
    return urls


def build_inbound_webhook_payload(inbound) -> dict[str, Any]:
    return {
        'id': inbound.pk,
        'sender': inbound.from_number or '',
        'body': inbound.text or '',
        'modem_index': inbound.modem_index,
        'received_at': inbound.created_at.isoformat(),
        'mm_state': inbound.mm_state or '',
    }


def _post_json(url: str, payload: dict[str, Any], *, timeout_sec: float) -> tuple[bool, str]:
    body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    req = urllib.request.Request(
        url,
        data=body,
        headers={'Content-Type': 'application/json'},
        method='POST',
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            code = getattr(resp, 'status', None) or resp.getcode()
            if 200 <= int(code) < 300:
                return True, ''
            return False, f'HTTP {code}'
    except urllib.error.HTTPError as exc:
        return False, f'HTTP {exc.code}'
    except Exception as exc:
        return False, str(exc)


def deliver_inbound_webhooks(inbound) -> bool:
    """POST inbound payload to active webhook URLs for this modem. Returns True if all succeeded."""
    urls = get_active_webhook_urls(inbound.modem_index)
    if not urls:
        _LOGGER.debug(
            'No inbound webhook URLs for modem_index=%s; skip pk=%s',
            inbound.modem_index,
            inbound.pk,
        )
        return True

    payload = build_inbound_webhook_payload(inbound)
    timeout = float(getattr(settings, 'SMS_WEBHOOK_TIMEOUT_SEC', 15.0))
    retry_max = int(getattr(settings, 'SMS_WEBHOOK_RETRY_MAX', 5))
    retry_base = float(getattr(settings, 'SMS_WEBHOOK_RETRY_BASE_SEC', 1.0))

    all_ok = True
    for url in urls:
        ok = False
        last_err = ''
        for attempt in range(retry_max):
            ok, last_err = _post_json(url, payload, timeout_sec=timeout)
            if ok:
                if attempt:
                    _LOGGER.info(
                        'Webhook delivered inbound pk=%s url=%s attempt=%s',
                        inbound.pk,
                        url,
                        attempt + 1,
                    )
                break
            if attempt < retry_max - 1:
                delay = min(retry_base * (2**attempt), 60.0)
                _LOGGER.warning(
                    'Webhook failed inbound pk=%s url=%s attempt=%s/%s err=%s retry=%.1fs',
                    inbound.pk,
                    url,
                    attempt + 1,
                    retry_max,
                    last_err,
                    delay,
                )
                time.sleep(delay)
        if not ok:
            all_ok = False
            _LOGGER.error(
                'Webhook delivery failed inbound pk=%s url=%s err=%s',
                inbound.pk,
                url,
                last_err,
            )
        else:
            _LOGGER.info('Webhook delivered inbound pk=%s url=%s', inbound.pk, url)
    return all_ok
