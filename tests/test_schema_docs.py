"""OpenAPI schema, Swagger UI, and JWT schema access."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from django.urls import reverse

from apps.external_device.spectacular_auth import ApiKeyAuthenticationScheme

pytestmark = pytest.mark.django_db


def test_schema_available_without_auth(api_client):
    resp = api_client.get(reverse('schema'))
    assert resp.status_code == 200


def test_swagger_ui_available_without_auth(api_client):
    resp = api_client.get(reverse('swagger-ui'))
    assert resp.status_code == 200
    page = resp.content.decode('utf-8')
    # Swagger UI: persist Authorize credentials across browser refresh (localStorage).
    assert 'persistAuthorization' in page
    assert 'hiwavetel.swagger.authorized' in page


def test_schema_ok_when_authenticated(auth_client):
    resp = auth_client.get(reverse('schema'))
    assert resp.status_code == 200


def test_schema_api_key_security_for_v1_external_device_routes(api_client):
    """External device gateway uses ApiKeyAuthentication → OpenAPI apiKeyAuth, not Bearer jwtAuth."""
    resp = api_client.get(reverse('schema') + '?format=json')
    assert resp.status_code == 200
    data = resp.json()
    schemes = data['components']['securitySchemes']
    assert 'apiKeyAuth' in schemes
    assert schemes['apiKeyAuth']['type'] == 'apiKey'
    assert schemes['apiKeyAuth']['name'] == 'X-API-Key'

    inbox = data['paths']['/api/v1/sms/inbox/']['get']
    assert inbox['security'] == [{'apiKeyAuth': []}]

    register_post = data['paths']['/api/v1/external-devices/register/']['post']
    assert 'security' not in register_post


def test_schema_jwt_security_for_modem_sms_routes(api_client):
    resp = api_client.get(reverse('schema') + '?format=json')
    data = resp.json()
    inbound = data['paths']['/api/sms/inbound/']['get']
    security_schemes = {k for s in inbound['security'] for k in s}
    assert 'jwtAuth' in security_schemes


def test_schema_sms_status_includes_request_id_query_parameter(api_client):
    resp = api_client.get(reverse('schema') + '?format=json')
    data = resp.json()
    status_get = data['paths']['/api/v1/sms/status/']['get']
    parameters = status_get.get('parameters', [])
    request_id_param = next((p for p in parameters if p.get('name') == 'request_id'), None)
    assert request_id_param is not None
    assert request_id_param['in'] == 'query'
    assert request_id_param['required'] is True


def test_api_key_authentication_scheme_security_definition():
    """Smoke test for ApiKeyAuthenticationScheme.get_security_definition."""
    mock_target = MagicMock()
    scheme = ApiKeyAuthenticationScheme(target=mock_target)
    mock_auto_schema = MagicMock()
    
    security_def = scheme.get_security_definition(mock_auto_schema)
    
    assert security_def['type'] == 'apiKey'
    assert security_def['in'] == 'header'
    assert security_def['name'] == 'X-API-Key'
    assert 'description' in security_def
    assert len(security_def['description']) > 0


def test_api_key_authentication_scheme_target_class():
    """Should have correct target_class attribute."""
    from apps.external_device.authentication import ApiKeyAuthentication
    assert ApiKeyAuthenticationScheme.target_class == ApiKeyAuthentication


def test_api_key_authentication_scheme_name():
    """Should have correct name attribute."""
    assert ApiKeyAuthenticationScheme.name == 'apiKeyAuth'
