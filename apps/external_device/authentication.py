"""API key authentication backend and permissions for external devices."""

from __future__ import annotations

import hashlib
import logging
from typing import TYPE_CHECKING

from django.utils import timezone
from rest_framework import authentication, permissions
from rest_framework.exceptions import AuthenticationFailed

from .models import ExternalDevice

if TYPE_CHECKING:
    from rest_framework.request import Request

_LOGGER = logging.getLogger(__name__)


def hash_api_key(raw_key: str) -> str:
    """Return SHA-256 hex hash of the raw API key."""
    return hashlib.sha256(raw_key.encode('utf-8')).hexdigest()


class ApiKeyAuthentication(authentication.BaseAuthentication):
    """Authenticate external devices using API keys.
    
    Supports both `Authorization: ApiKey <key>` and `X-API-Key: <key>` headers.
    """

    def authenticate(self, request: Request) -> tuple[ExternalDevice, str] | None:
        """Authenticate the request using API key.
        
        Returns:
            (device, raw_key) if authenticated, None if no auth header present.
            
        Raises:
            AuthenticationFailed: If key is invalid or device is not active.
        """
        raw_key = self._extract_api_key(request)
        if not raw_key:
            _LOGGER.warning('ApiKey auth missing key for path=%s', request.path)
            return None

        key_hash = hash_api_key(raw_key)
        
        try:
            device = ExternalDevice.objects.get(
                api_key_hash=key_hash,
                status=ExternalDevice.Status.ACTIVE
            )
        except ExternalDevice.DoesNotExist:
            _LOGGER.warning('ApiKey auth failed for path=%s key_hash_prefix=%s', request.path, key_hash[:8])
            raise AuthenticationFailed('Invalid API key or device not active.')

        device.last_seen = timezone.now()
        device.is_available = True
        device.save(update_fields=['last_seen', 'is_available'])
        _LOGGER.info('ApiKey auth success path=%s device=%s', request.path, device.device_id)

        return (device, raw_key)

    def _extract_api_key(self, request: Request) -> str | None:
        """Extract API key from Authorization or X-API-Key header."""
        auth_header = request.META.get('HTTP_AUTHORIZATION', '')
        if auth_header.startswith('ApiKey '):
            return auth_header[7:].strip()
        
        x_api_key = request.META.get('HTTP_X_API_KEY', '')
        if x_api_key:
            return x_api_key.strip()
        
        return None


class IsActiveExternalDevice(permissions.BasePermission):
    """Permission that requires the user to be an active ExternalDevice."""

    def has_permission(self, request: Request, view) -> bool:  # type: ignore[override]
        """Check if user is an ExternalDevice with active status."""
        if not isinstance(request.user, ExternalDevice):
            return False
        return request.user.status == ExternalDevice.Status.ACTIVE
