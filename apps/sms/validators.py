"""Cross-layer validators reused by serializers and Django models."""

from __future__ import annotations

from django.core.exceptions import ValidationError


def sms_destination_validator(value: str) -> None:
    """Normalize ``to_number`` payloads: printable characters with a sensible digit count."""

    stripped = value.strip()
    if not stripped:
        raise ValidationError('Destination number cannot be blank.')
    digits_only = ''.join(ch for ch in stripped if ch.isdigit())
    if len(digits_only) < 8:
        raise ValidationError('Provide at least eight digits in the destination number.')
    if len(digits_only) > 15:
        raise ValidationError('Destination numbers cannot exceed 15 digits.')
