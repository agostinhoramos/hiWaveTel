"""OpenAPI schema and Swagger UI (no authentication)."""

from __future__ import annotations

import pytest
from django.urls import reverse

pytestmark = pytest.mark.django_db


def test_schema_available_without_auth(api_client):
    resp = api_client.get(reverse('schema'))
    assert resp.status_code == 200


def test_swagger_ui_available_without_auth(api_client):
    resp = api_client.get(reverse('swagger-ui'))
    assert resp.status_code == 200


def test_schema_no_security_schemes(api_client):
    resp = api_client.get(reverse('schema') + '?format=json')
    assert resp.status_code == 200
    data = resp.json()
    schemes = data.get('components', {}).get('securitySchemes', {})
    assert schemes == {}
    assert data.get('security') in (None, [], [{}])


def test_schema_includes_send_sms_route(api_client):
    resp = api_client.get(reverse('schema') + '?format=json')
    data = resp.json()
    send_post = data['paths']['/api/sms/send/']['post']
    assert send_post['tags'] == ['SMS']
    security = send_post.get('security')
    assert security in (None, [], [{}])


def test_schema_excludes_legacy_routes(api_client):
    resp = api_client.get(reverse('schema') + '?format=json')
    paths = resp.json()['paths']
    for legacy in (
        '/api/v1/sms/inbox/',
        '/api/sms/inbound/',
        '/api/auth/token/',
        '/api/sms/device/status/',
        '/api/sms/system/readiness/',
    ):
        assert legacy not in paths


def test_schema_modem_availability_public(api_client):
    resp = api_client.get(reverse('schema') + '?format=json')
    data = resp.json()
    availability = data['paths']['/api/sms/system/modem/{modem_index}/availability/']['get']
    assert availability['tags'] == ['System']
