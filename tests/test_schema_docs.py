"""OpenAPI schema, Swagger UI, and JWT schema access."""

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


def test_schema_ok_when_authenticated(auth_client):
    resp = auth_client.get(reverse('schema'))
    assert resp.status_code == 200
