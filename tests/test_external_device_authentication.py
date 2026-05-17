"""Tests for external device authentication and permissions."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from django.contrib.auth.models import AnonymousUser, User
from django.utils import timezone
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.test import APIRequestFactory

from apps.external_device.authentication import (
    ApiKeyAuthentication,
    IsActiveExternalDevice,
    hash_api_key,
)
from apps.external_device.models import ExternalDevice


class TestHashApiKey:
    """Test hash_api_key utility function."""

    def test_returns_sha256_hex(self):
        """Should return SHA-256 hex digest of the key."""
        key = 'test-api-key-123'
        result = hash_api_key(key)
        
        assert len(result) == 64
        assert all(c in '0123456789abcdef' for c in result)

    def test_same_key_produces_same_hash(self):
        """Should produce consistent hash for same key."""
        key = 'my-secret-key'
        hash1 = hash_api_key(key)
        hash2 = hash_api_key(key)
        
        assert hash1 == hash2

    def test_different_keys_produce_different_hashes(self):
        """Should produce different hashes for different keys."""
        hash1 = hash_api_key('key1')
        hash2 = hash_api_key('key2')
        
        assert hash1 != hash2


@pytest.mark.django_db
class TestApiKeyAuthentication:
    """Test ApiKeyAuthentication class."""

    def test_authenticate_with_authorization_header(self):
        """Should authenticate using Authorization: ApiKey header."""
        raw_key = 'test-api-key-456'
        device = ExternalDevice.objects.create(
            device_id='+351913000387',
            name='Test Device',
            api_key_hash=hash_api_key(raw_key),
            status=ExternalDevice.Status.ACTIVE
        )
        
        factory = APIRequestFactory()
        request = factory.get('/api/test/', HTTP_AUTHORIZATION=f'ApiKey {raw_key}')
        
        auth = ApiKeyAuthentication()
        result = auth.authenticate(request)
        
        assert result is not None
        authenticated_device, returned_key = result
        assert authenticated_device.device_id == device.device_id
        assert returned_key == raw_key
        
        device.refresh_from_db()
        assert device.is_available is True
        assert device.last_seen is not None

    def test_authenticate_with_x_api_key_header(self):
        """Should authenticate using X-API-Key header."""
        raw_key = 'test-x-api-key-789'
        device = ExternalDevice.objects.create(
            device_id='+351912345678',
            name='Test Device 2',
            api_key_hash=hash_api_key(raw_key),
            status=ExternalDevice.Status.ACTIVE
        )
        
        factory = APIRequestFactory()
        request = factory.get('/api/test/', HTTP_X_API_KEY=raw_key)
        
        auth = ApiKeyAuthentication()
        result = auth.authenticate(request)
        
        assert result is not None
        authenticated_device, returned_key = result
        assert authenticated_device.device_id == device.device_id
        assert returned_key == raw_key

    def test_authenticate_returns_none_when_no_header(self):
        """Should return None when no auth header is present."""
        factory = APIRequestFactory()
        request = factory.get('/api/test/')
        
        auth = ApiKeyAuthentication()
        result = auth.authenticate(request)
        
        assert result is None

    def test_authenticate_raises_when_invalid_key(self):
        """Should raise AuthenticationFailed for invalid API key."""
        ExternalDevice.objects.create(
            device_id='+351913000387',
            name='Test Device',
            api_key_hash=hash_api_key('correct-key'),
            status=ExternalDevice.Status.ACTIVE
        )
        
        factory = APIRequestFactory()
        request = factory.get('/api/test/', HTTP_AUTHORIZATION='ApiKey wrong-key')
        
        auth = ApiKeyAuthentication()
        
        with pytest.raises(AuthenticationFailed) as exc_info:
            auth.authenticate(request)
        
        assert 'Invalid API key' in str(exc_info.value)

    def test_authenticate_raises_when_device_not_active(self):
        """Should raise AuthenticationFailed when device is not ACTIVE."""
        raw_key = 'test-pending-key'
        ExternalDevice.objects.create(
            device_id='+351913000387',
            name='Test Device',
            api_key_hash=hash_api_key(raw_key),
            status=ExternalDevice.Status.PENDING
        )
        
        factory = APIRequestFactory()
        request = factory.get('/api/test/', HTTP_AUTHORIZATION=f'ApiKey {raw_key}')
        
        auth = ApiKeyAuthentication()
        
        with pytest.raises(AuthenticationFailed) as exc_info:
            auth.authenticate(request)
        
        assert 'Invalid API key' in str(exc_info.value)

    def test_authenticate_raises_when_device_inactive(self):
        """Should raise AuthenticationFailed when device status is INACTIVE."""
        raw_key = 'test-inactive-key'
        ExternalDevice.objects.create(
            device_id='+351913000387',
            name='Test Device',
            api_key_hash=hash_api_key(raw_key),
            status=ExternalDevice.Status.INACTIVE
        )
        
        factory = APIRequestFactory()
        request = factory.get('/api/test/', HTTP_AUTHORIZATION=f'ApiKey {raw_key}')
        
        auth = ApiKeyAuthentication()
        
        with pytest.raises(AuthenticationFailed):
            auth.authenticate(request)

    def test_extract_api_key_from_authorization_header(self):
        """Should extract key from Authorization: ApiKey header."""
        factory = APIRequestFactory()
        request = factory.get('/api/test/', HTTP_AUTHORIZATION='ApiKey my-secret-key')
        
        auth = ApiKeyAuthentication()
        key = auth._extract_api_key(request)
        
        assert key == 'my-secret-key'

    def test_extract_api_key_from_x_api_key_header(self):
        """Should extract key from X-API-Key header."""
        factory = APIRequestFactory()
        request = factory.get('/api/test/', HTTP_X_API_KEY='my-x-api-key')
        
        auth = ApiKeyAuthentication()
        key = auth._extract_api_key(request)
        
        assert key == 'my-x-api-key'

    def test_extract_api_key_returns_none_when_no_header(self):
        """Should return None when no API key header present."""
        factory = APIRequestFactory()
        request = factory.get('/api/test/')
        
        auth = ApiKeyAuthentication()
        key = auth._extract_api_key(request)
        
        assert key is None

    def test_extract_api_key_returns_none_for_invalid_auth_type(self):
        """Should return None for non-ApiKey authorization types."""
        factory = APIRequestFactory()
        request = factory.get('/api/test/', HTTP_AUTHORIZATION='Bearer jwt-token')
        
        auth = ApiKeyAuthentication()
        key = auth._extract_api_key(request)
        
        assert key is None

    def test_extract_api_key_strips_whitespace(self):
        """Should strip whitespace from extracted key."""
        factory = APIRequestFactory()
        request = factory.get('/api/test/', HTTP_AUTHORIZATION='ApiKey   key-with-spaces   ')
        
        auth = ApiKeyAuthentication()
        key = auth._extract_api_key(request)
        
        assert key == 'key-with-spaces'

    def test_authenticate_prefers_authorization_over_x_api_key(self):
        """Should use Authorization header when both headers present."""
        raw_key1 = 'auth-header-key'
        raw_key2 = 'x-api-key-header-key'
        
        device = ExternalDevice.objects.create(
            device_id='+351913000387',
            name='Test Device',
            api_key_hash=hash_api_key(raw_key1),
            status=ExternalDevice.Status.ACTIVE
        )
        
        factory = APIRequestFactory()
        request = factory.get(
            '/api/test/',
            HTTP_AUTHORIZATION=f'ApiKey {raw_key1}',
            HTTP_X_API_KEY=raw_key2
        )
        
        auth = ApiKeyAuthentication()
        result = auth.authenticate(request)
        
        assert result is not None
        authenticated_device, returned_key = result
        assert returned_key == raw_key1


@pytest.mark.django_db
class TestIsActiveExternalDevice:
    """Test IsActiveExternalDevice permission class."""

    def test_allows_active_external_device(self):
        """Should allow access for active ExternalDevice."""
        device = ExternalDevice.objects.create(
            device_id='+351913000387',
            name='Test Device',
            api_key_hash='hash',
            status=ExternalDevice.Status.ACTIVE
        )
        
        request = SimpleNamespace(user=device)
        permission = IsActiveExternalDevice()
        
        assert permission.has_permission(request, None) is True

    def test_denies_pending_external_device(self):
        """Should deny access for pending ExternalDevice."""
        device = ExternalDevice.objects.create(
            device_id='+351913000387',
            name='Test Device',
            api_key_hash='hash',
            status=ExternalDevice.Status.PENDING
        )
        
        request = SimpleNamespace(user=device)
        permission = IsActiveExternalDevice()
        
        assert permission.has_permission(request, None) is False

    def test_denies_inactive_external_device(self):
        """Should deny access for inactive ExternalDevice."""
        device = ExternalDevice.objects.create(
            device_id='+351913000387',
            name='Test Device',
            api_key_hash='hash',
            status=ExternalDevice.Status.INACTIVE
        )
        
        request = SimpleNamespace(user=device)
        permission = IsActiveExternalDevice()
        
        assert permission.has_permission(request, None) is False

    def test_denies_suspended_external_device(self):
        """Should deny access for suspended ExternalDevice."""
        device = ExternalDevice.objects.create(
            device_id='+351913000387',
            name='Test Device',
            api_key_hash='hash',
            status=ExternalDevice.Status.SUSPENDED
        )
        
        request = SimpleNamespace(user=device)
        permission = IsActiveExternalDevice()
        
        assert permission.has_permission(request, None) is False

    def test_denies_regular_django_user(self):
        """Should deny access for regular Django User."""
        user = User.objects.create_user(username='testuser')
        
        request = SimpleNamespace(user=user)
        permission = IsActiveExternalDevice()
        
        assert permission.has_permission(request, None) is False

    def test_denies_anonymous_user(self):
        """Should deny access for anonymous users."""
        request = SimpleNamespace(user=AnonymousUser())
        permission = IsActiveExternalDevice()
        
        assert permission.has_permission(request, None) is False

    def test_denies_none_user(self):
        """Should deny access when user is None."""
        request = SimpleNamespace(user=None)
        permission = IsActiveExternalDevice()
        
        assert permission.has_permission(request, None) is False
