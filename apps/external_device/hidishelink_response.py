"""JSON envelope compatible with hiDisheLink Android client (`success`, `data`, `error`)."""

from __future__ import annotations

from typing import Any

from django.http import JsonResponse


def hidishelink_json_response(
    *,
    success: bool,
    data: dict[str, Any] | list[Any] | None = None,
    error: str | None = None,
    status: int = 200,
) -> JsonResponse:
    """Return a JsonResponse with snake_case envelope keys."""
    return JsonResponse(
        {'success': success, 'data': data, 'error': error},
        status=status,
        json_dumps_params={'ensure_ascii': False},
    )


def iso8601_offset(dt) -> str:
    """ISO-8601 string with timezone offset for Android session expiry parsing."""
    if dt is None:
        return ''
    if hasattr(dt, 'isoformat'):
        return dt.isoformat()
    return str(dt)
