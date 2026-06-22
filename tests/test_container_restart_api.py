"""Tests for container restart API."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from django.test import override_settings
from django.urls import reverse


@pytest.mark.django_db
@override_settings(HIWAVE_ALLOW_CONTAINER_RESTART_API=True)
def test_container_restart_accepted(api_client):
    with patch('apps.sms.views_system.schedule_container_restart', return_value=1.0):
        resp = api_client.post(reverse('sms-container-restart'))
    assert resp.status_code == 202
    data = resp.json()
    assert data['accepted'] is True
    assert data['delay_sec'] == 1.0


@pytest.mark.django_db
@override_settings(HIWAVE_ALLOW_CONTAINER_RESTART_API=False)
def test_container_restart_forbidden_when_disabled(api_client):
    resp = api_client.post(reverse('sms-container-restart'))
    assert resp.status_code == 403
