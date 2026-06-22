"""Filters for inbound SMS before webhook delivery."""

from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from apps.sms.models import OutboundSms

_ECHO_WINDOW_MINUTES = 15


def inbound_ready_for_webhook(inbound) -> bool:
    """Wait until mmcli snapshot has sender or body before notifying."""
    return bool((inbound.text or '').strip()) or bool((inbound.from_number or '').strip())


def inbound_should_skip_webhook(inbound) -> bool:
    """Skip likely outbound echo from modem inbox."""
    body = (inbound.text or '').strip()
    if not body:
        return False
    cutoff = timezone.now() - timedelta(minutes=_ECHO_WINDOW_MINUTES)
    qs = OutboundSms.objects.filter(
        state=OutboundSms.State.SENT,
        text=body,
        created_at__gte=cutoff,
    )
    sender = (inbound.from_number or '').strip()
    if sender:
        qs = qs.filter(to_number=sender)
    return qs.exists()
