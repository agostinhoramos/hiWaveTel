"""LAN / DisallowedHost: garantir que ALLOWED_HOSTS aceita IP sem porta no header."""

from __future__ import annotations

import pytest
from django.test import Client


@pytest.mark.django_db
def test_lan_ip_with_port_in_http_host_accepts_matching_allowed_hosts(settings) -> None:
    """Browser envia HTTP_HOST=com IP:porta; ALLOWED_HOSTS deve listar só o IP (ou hostname)."""
    settings.ALLOWED_HOSTS = ['localhost', '127.0.0.1', '192.168.1.65', '[::1]']
    client = Client()
    response = client.get('/', HTTP_HOST='192.168.1.65:8000')
    # DisallowedHost → resposta 400 (SecurityMiddleware / CommonMiddleware)
    assert response.status_code != 400, 'DisallowedHost deve estar resolvido com IP em ALLOWED_HOSTS'
