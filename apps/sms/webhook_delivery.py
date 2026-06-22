"""HTTP webhook delivery for inbound SMS."""

from __future__ import annotations

import json
import logging
import re
import ssl
import time
import urllib.error
import urllib.request
from typing import Any

from django.conf import settings

_LOGGER = logging.getLogger(__name__)
_ssl_verify_disabled_logged = False

_WEBHOOK_SITE_EDIT_RE = re.compile(
    r'^https://webhook\.site/#!/edit/(?P<token>[a-f0-9-]{36})\/?$',
    re.IGNORECASE,
)
_WEBHOOK_SITE_VIEW_RE = re.compile(
    r'^https://webhook\.site/#!/view/(?P<token>[a-f0-9-]{36})\/?$',
    re.IGNORECASE,
)


def normalize_webhook_url(url: str) -> str:
    """Return a POST-able webhook URL (fixes common webhook.site UI copy-paste mistakes)."""
    u = (url or '').strip()
    if not u:
        return u
    for pattern in (_WEBHOOK_SITE_EDIT_RE, _WEBHOOK_SITE_VIEW_RE):
        match = pattern.match(u)
        if match:
            normalized = f'https://webhook.site/{match.group("token")}'
            if normalized != u:
                _LOGGER.info('Normalized webhook URL %s -> %s', u, normalized)
            return normalized
    return u


def get_active_webhook_urls(modem_index: int) -> list[str]:
    """Enabled webhook URLs registered for the given modem index."""
    from apps.sms.models import InboundWebhook

    urls: list[str] = []
    seen: set[str] = set()
    for row in InboundWebhook.objects.filter(
        enabled=True,
        modem_index=modem_index,
    ).order_by('id'):
        u = normalize_webhook_url((row.url or '').strip())
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


def build_outbound_webhook_payload(outbound) -> dict[str, Any]:
    """Webhook payload for SMS sent via ``POST /api/sms/send/``."""
    return {
        'id': outbound.pk,
        'sender': 'me',
        'body': outbound.text or '',
        'modem_index': outbound.modem_index,
        'received_at': outbound.updated_at.isoformat(),
        'mm_state': 'sended',
    }


def _deliver_payload_to_urls(
    payload: dict[str, Any],
    *,
    modem_index: int,
    log_label: str,
    log_id: int,
) -> bool:
    urls = get_active_webhook_urls(modem_index)
    if not urls:
        _LOGGER.info(
            'No inbound webhook URLs for modem_index=%s; skip %s pk=%s',
            modem_index,
            log_label,
            log_id,
        )
        return True

    _LOGGER.info(
        'Delivering %s pk=%s to %s webhook URL(s) modem_index=%s',
        log_label,
        log_id,
        len(urls),
        modem_index,
    )

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
                        'Webhook delivered %s pk=%s url=%s attempt=%s',
                        log_label,
                        log_id,
                        url,
                        attempt + 1,
                    )
                break
            if attempt < retry_max - 1:
                delay = min(retry_base * (2**attempt), 60.0)
                _LOGGER.warning(
                    'Webhook failed %s pk=%s url=%s attempt=%s/%s err=%s retry=%.1fs',
                    log_label,
                    log_id,
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
                'Webhook delivery failed %s pk=%s url=%s err=%s',
                log_label,
                log_id,
                url,
                last_err,
            )
        else:
            _LOGGER.info('Webhook delivered %s pk=%s url=%s', log_label, log_id, url)
    return all_ok


def deliver_outbound_webhooks(outbound) -> bool:
    """POST outbound-sent payload to active webhook URLs for this modem."""
    payload = build_outbound_webhook_payload(outbound)
    return _deliver_payload_to_urls(
        payload,
        modem_index=outbound.modem_index,
        log_label='outbound',
        log_id=outbound.pk,
    )


def deliver_inbound_webhooks(inbound) -> bool:
    """POST inbound payload to active webhook URLs for this modem. Returns True if all succeeded."""
    payload = build_inbound_webhook_payload(inbound)
    return _deliver_payload_to_urls(
        payload,
        modem_index=inbound.modem_index,
        log_label='inbound',
        log_id=inbound.pk,
    )


def _webhook_ssl_context() -> ssl.SSLContext | None:
    """Return SSL context for webhook POST; None uses Python default certificate verification."""
    if getattr(settings, 'SMS_WEBHOOK_SSL_VERIFY', False):
        return None
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _post_json(url: str, payload: dict[str, Any], *, timeout_sec: float) -> tuple[bool, str]:
    global _ssl_verify_disabled_logged

    body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    req = urllib.request.Request(
        url,
        data=body,
        headers={'Content-Type': 'application/json'},
        method='POST',
    )
    ssl_context = _webhook_ssl_context()
    if ssl_context is not None and not _ssl_verify_disabled_logged:
        _LOGGER.info('Webhook SSL certificate verification disabled (SMS_WEBHOOK_SSL_VERIFY=false)')
        _ssl_verify_disabled_logged = True
    try:
        with urllib.request.urlopen(req, timeout=timeout_sec, context=ssl_context) as resp:
            code = getattr(resp, 'status', None) or resp.getcode()
            if 200 <= int(code) < 300:
                return True, ''
            return False, f'HTTP {code}'
    except urllib.error.HTTPError as exc:
        return False, f'HTTP {exc.code}'
    except Exception as exc:
        return False, str(exc)
