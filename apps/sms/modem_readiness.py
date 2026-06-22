"""Evaluate and persist SMS system readiness (modem + SIM + watcher config)."""

from __future__ import annotations

import logging
import os
from dataclasses import asdict, dataclass, field
from typing import Any

from django.conf import settings
from django.utils import timezone

from apps.sms.modem_env import get_modem_phone_number
from apps.sms.modem_identity import normalize_phone_e164, probe_modem_identity
from apps.sms.mmcli_client import MMCLIClient, MmcliError, resolve_modem_mmcli_index
from apps.sms.models import InboundSms, OutboundSms
from apps.sms.modem_ready import _READY_STATES, get_modem_state, modem_overview_needs_sim_unlock


def _iso_datetime(value) -> str | None:
    if value is None:
        return None
    if hasattr(value, 'isoformat'):
        return value.isoformat()
    return str(value)


@dataclass
class ModemLastActivity:
    at: str | None
    source: str | None
    inbound_sms_at: str | None = None
    outbound_sms_at: str | None = None
    device_last_seen_at: str | None = None
    readiness_checked_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ModemAvailability:
    modem_index: int
    available: bool
    state: str
    checked_at: str = ''
    enumerated_indices: list[int] = field(default_factory=list)
    ping_ok: bool | None = None
    phone_number: str = ''
    detail: str = ''
    last_activity: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

_LOGGER = logging.getLogger(__name__)

READINESS_METADATA_KEY = 'modem_readiness'


@dataclass
class ReadinessIssue:
    code: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {'code': self.code, 'message': self.message}


@dataclass
class ReadinessSnapshot:
    ready: bool
    phone_number: str
    modem_index: int | None
    modem_state: str
    checked_at: str
    capabilities: dict[str, bool]
    issues: list[ReadinessIssue] = field(default_factory=list)
    components: dict[str, Any] = field(default_factory=dict)
    last_persisted_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload['issues'] = [
            item if isinstance(item, dict) else item.to_dict() for item in self.issues
        ]
        return payload


def _truthy_env(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name, '')
    if not raw:
        return default
    return raw.strip().lower() in {'1', 'true', 'yes', 'on'}


def _resolve_phone_fallback(modem_index: int) -> str:
    return get_modem_phone_number(modem_index)


def _load_last_persisted(phone_number: str) -> str | None:
    return None


def evaluate_sms_readiness(
    modem_index: int | None = None,
    *,
    client: MMCLIClient | None = None,
    extra_issues: list[ReadinessIssue] | None = None,
) -> ReadinessSnapshot:
    """Probe mmcli and return a live readiness snapshot."""
    checked_at = timezone.now().isoformat()
    configured = int(modem_index if modem_index is not None else getattr(settings, 'MODEM_MMCLI_INDEX', 0))
    timeout = float(getattr(settings, 'HIWAVE_MMCLI_HEALTH_TIMEOUT', 15.0))
    mm = client or MMCLIClient(timeout_sec=timeout)

    issues: list[ReadinessIssue] = list(extra_issues or [])
    components: dict[str, Any] = {}
    effective_index: int | None = None
    modem_state = 'unknown'
    phone_number = ''
    ping_ok = False
    sim_locked = False

    sms_watcher_configured = _truthy_env('RUN_SMS_WATCHER', default=True)
    components['sms_watcher'] = {'ok': sms_watcher_configured, 'configured': sms_watcher_configured}

    if not sms_watcher_configured:
        issues.append(
            ReadinessIssue(
                code='sms_watcher_disabled',
                message='RUN_SMS_WATCHER is disabled; inbound SMS via D-Bus watcher will not run.',
            )
        )

    try:
        indices = mm.list_modem_indices()
        components['modem_enumerated'] = {'ok': bool(indices), 'indices': indices}
        if not indices:
            issues.append(
                ReadinessIssue(
                    code='no_modem',
                    message='ModemManager returned zero modems via mmcli -L.',
                )
            )
        else:
            try:
                effective_index = resolve_modem_mmcli_index(configured, client=mm)
            except MmcliError as exc:
                issues.append(
                    ReadinessIssue(
                        code='modem_index_missing',
                        message=str(exc)[:512],
                    )
                )
                components['modem_enumerated'] = {'ok': False, 'indices': indices}
            else:
                modem_state = get_modem_state(effective_index, mmcli_path=mm.mmcli_path)
                state_ok = modem_state in _READY_STATES
                components['modem_state'] = {'ok': state_ok, 'state': modem_state}
                if not state_ok:
                    issues.append(
                        ReadinessIssue(
                            code='modem_not_ready',
                            message=(
                                f'Modem state is {modem_state} '
                                f'(expected enabled or registered).'
                            ),
                        )
                    )

                sim_locked = modem_overview_needs_sim_unlock(effective_index, mmcli_path=mm.mmcli_path)
                components['sim_lock'] = {'ok': not sim_locked, 'locked': sim_locked}
                if sim_locked:
                    issues.append(
                        ReadinessIssue(
                            code='sim_pin_locked',
                            message='SIM reports PIN lock; unlock required before SMS.',
                        )
                    )

                ping_ok, ping_text = mm.modem_ping(effective_index)
                components['mmcli_ping'] = {'ok': ping_ok, 'detail': (ping_text or '')[:500]}
                if not ping_ok:
                    issues.append(
                        ReadinessIssue(
                            code='mmcli_unreachable',
                            message=(ping_text or 'mmcli modem ping failed.')[:512],
                        )
                    )

                identity = probe_modem_identity(effective_index, client=mm)
                phone_number = normalize_phone_e164(identity.get('phone_number') or '')
                if not phone_number:
                    phone_number = _resolve_phone_fallback(effective_index)
                phone_ok = bool(phone_number)
                components['phone_number'] = {'ok': phone_ok, 'value': phone_number or None}
                if not phone_ok:
                    issues.append(
                        ReadinessIssue(
                            code='phone_number_missing',
                            message='Could not resolve modem phone number from mmcli or configuration.',
                        )
                    )
    except MmcliError as exc:
        components['modem_enumerated'] = {'ok': False, 'indices': []}
        issues.append(
            ReadinessIssue(
                code='mmcli_unreachable',
                message=str(exc)[:512],
            )
        )
        _LOGGER.warning('evaluate_sms_readiness mmcli failure: %s', exc)

    modem_ok = (
        effective_index is not None
        and modem_state in _READY_STATES
        and not sim_locked
        and ping_ok
    )

    outbound_sms = modem_ok
    inbound_sms = outbound_sms and sms_watcher_configured

    ready = outbound_sms and inbound_sms and not any(
        i.code in {'no_modem', 'modem_index_missing', 'sim_pin_locked', 'phone_number_missing'}
        for i in issues
    )

    last_persisted_at = _load_last_persisted(phone_number) if phone_number else None

    return ReadinessSnapshot(
        ready=ready,
        phone_number=phone_number,
        modem_index=effective_index,
        modem_state=modem_state,
        checked_at=checked_at,
        capabilities={'inbound_sms': inbound_sms, 'outbound_sms': outbound_sms},
        issues=issues,
        components=components,
        last_persisted_at=last_persisted_at,
    )


def persist_readiness_snapshot(snapshot: ReadinessSnapshot) -> None:
    """Readiness persistence removed with external_device layer."""
    return


def refresh_and_persist_readiness(
    modem_index: int | None = None,
    *,
    extra_issues: list[ReadinessIssue] | None = None,
) -> ReadinessSnapshot:
    """Evaluate readiness live (no external metadata persistence)."""
    snapshot = evaluate_sms_readiness(modem_index, extra_issues=extra_issues)
    try:
        persist_readiness_snapshot(snapshot)
    except Exception:
        _LOGGER.exception('persist_readiness_snapshot failed phone=%s', snapshot.phone_number)
    return snapshot


def refresh_readiness_safe(
    modem_index: int | None = None,
    *,
    extra_issues: list[ReadinessIssue] | None = None,
) -> None:
    """Background hook: refresh without raising."""
    try:
        from apps.sms.modem_registry import sync_detected_modems

        sync_detected_modems()
    except Exception:
        _LOGGER.debug('sync_detected_modems in refresh_readiness_safe failed', exc_info=True)
    try:
        refresh_and_persist_readiness(modem_index, extra_issues=extra_issues)
    except Exception:
        _LOGGER.exception('refresh_readiness_safe failed modem_index=%s', modem_index)


def _gather_modem_last_activity(modem_index: int) -> ModemLastActivity:
    """Best-effort last-known activity for a modem index (DB; no mmcli)."""
    inbound_at = (
        InboundSms.objects.filter(modem_index=modem_index)
        .order_by('-created_at')
        .values_list('created_at', flat=True)
        .first()
    )
    outbound_at = (
        OutboundSms.objects.filter(modem_index=modem_index)
        .order_by('-updated_at')
        .values_list('updated_at', flat=True)
        .first()
    )

    candidates: list[tuple[str, Any]] = []
    if inbound_at is not None:
        candidates.append(('inbound_sms', inbound_at))
    if outbound_at is not None:
        candidates.append(('outbound_sms', outbound_at))

    latest_source: str | None = None
    latest_at = None
    if candidates:
        latest_source, latest_at = max(candidates, key=lambda item: item[1])

    return ModemLastActivity(
        at=_iso_datetime(latest_at),
        source=latest_source,
        inbound_sms_at=_iso_datetime(inbound_at),
        outbound_sms_at=_iso_datetime(outbound_at),
        device_last_seen_at=None,
        readiness_checked_at=None,
    )


def _resolve_modem_phone_number(
    modem_index: int,
    *,
    client: MMCLIClient | None = None,
) -> str:
    try:
        identity = probe_modem_identity(modem_index, client=client)
        return normalize_phone_e164(identity.get('phone_number') or '')
    except Exception:
        return get_modem_phone_number(modem_index)


def check_modem_availability(
    modem_index: int,
    *,
    client: MMCLIClient | None = None,
) -> ModemAvailability:
    """Return whether a specific mmcli modem index is present and responsive."""
    checked_at = timezone.now().isoformat()
    last_activity = _gather_modem_last_activity(modem_index).to_dict()
    timeout = float(getattr(settings, 'HIWAVE_MMCLI_HEALTH_TIMEOUT', 15.0))
    mm = client or MMCLIClient(timeout_sec=timeout)

    try:
        indices = mm.list_modem_indices()
    except MmcliError as exc:
        _LOGGER.warning('check_modem_availability mmcli list failure: %s', exc)
        return ModemAvailability(
            modem_index=modem_index,
            available=False,
            state='unknown',
            checked_at=checked_at,
            enumerated_indices=[],
            detail=f'ModemManager unreachable: {str(exc)[:512]}',
            last_activity=last_activity,
        )

    if modem_index not in indices:
        indices_label = ', '.join(str(i) for i in indices) if indices else 'none'
        return ModemAvailability(
            modem_index=modem_index,
            available=False,
            state='missing',
            checked_at=checked_at,
            enumerated_indices=list(indices),
            detail=(
                f'Modem index {modem_index} not reported by ModemManager '
                f'(mmcli -L returned: {indices_label}).'
            ),
            last_activity=last_activity,
        )

    state = get_modem_state(modem_index, mmcli_path=mm.mmcli_path)
    ping_ok, ping_detail = mm.modem_ping(modem_index)
    available = state in _READY_STATES and ping_ok
    phone_number = _resolve_modem_phone_number(modem_index, client=mm)

    if available:
        detail = f'Modem responsive (state={state}, ping ok).'
    elif state not in _READY_STATES:
        detail = f"Modem state '{state}' (expected enabled or registered)."
    elif not ping_ok:
        detail = (ping_detail or 'mmcli modem ping failed.').replace('\n', ' ')[:512]
    else:
        detail = 'Modem not available.'

    return ModemAvailability(
        modem_index=modem_index,
        available=available,
        state=state,
        checked_at=checked_at,
        enumerated_indices=list(indices),
        ping_ok=ping_ok,
        phone_number=phone_number,
        detail=detail,
        last_activity=last_activity,
    )
