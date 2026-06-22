"""Filters for inbound SMS before webhook delivery."""

from __future__ import annotations

from datetime import timedelta

from django.utils import timezone

from apps.sms.models import OutboundSms

_ECHO_WINDOW_MINUTES = 15
_OUTBOUND_MM_STATES = frozenset({'sent', 'delivered', 'sending'})


def inbound_ready_for_webhook(inbound) -> bool:
    """Wait until mmcli snapshot has sender or body before notifying."""
    return bool((inbound.text or '').strip()) or bool((inbound.from_number or '').strip())


def find_matching_outbound(inbound) -> OutboundSms | None:
    """Return outbound row when inbound persistence is echo of an API/mmcli send."""
    mm_path = (inbound.mm_path or '').strip()
    if mm_path:
        match = (
            OutboundSms.objects.filter(mm_path=mm_path)
            .exclude(state=OutboundSms.State.FAILED)
            .order_by('-pk')
            .first()
        )
        if match is not None:
            return match

    body = (inbound.text or '').strip()
    if not body:
        return None

    cutoff = timezone.now() - timedelta(minutes=_ECHO_WINDOW_MINUTES)
    base_qs = OutboundSms.objects.filter(
        modem_index=inbound.modem_index,
        text=body,
        created_at__gte=cutoff,
    ).exclude(state=OutboundSms.State.FAILED)

    mm_state = (inbound.mm_state or '').strip().lower()
    if mm_state in _OUTBOUND_MM_STATES:
        return base_qs.order_by('-pk').first()

    sender = (inbound.from_number or '').strip()
    if sender:
        match = base_qs.filter(to_number=sender, state=OutboundSms.State.SENT).order_by('-pk').first()
        if match is not None:
            return match

    return None


def inbound_should_skip_webhook(inbound) -> bool:
    """Skip inbound webhook when delivery is handled from the outbound send path."""
    return find_matching_outbound(inbound) is not None
