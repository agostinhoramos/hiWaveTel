"""Shared pytest fixtures."""

from __future__ import annotations

import pytest

from django.contrib.auth import get_user_model


@pytest.fixture(autouse=True)
def disable_modem_status_auto_publish(settings):
    """Avoid background mmcli telemetry threads when tests trigger MQTT `_on_connect` (default is on in Compose)."""
    settings.MQTT_MODEM_STATUS_AUTO_PUBLISH = False


@pytest.fixture
def api_client():
    from rest_framework.test import APIClient

    return APIClient()


@pytest.fixture
def user(db):
    User = get_user_model()
    return User.objects.create_user(username='api_user', password='test-password-secure')


@pytest.fixture
def auth_client(api_client, user):
    api_client.force_authenticate(user=user)
    return api_client


@pytest.fixture
def two_inbounds(db):
    """Two inbound SMS rows for list/filter assertions."""
    from apps.sms.models import InboundSms

    a = InboundSms.objects.create(
        mm_path='/org/freedesktop/ModemManager1/SMS/0',
        modem_index=0,
        from_number='+351913000387',
        text='hello',
    )
    InboundSms.objects.create(
        mm_path='/org/freedesktop/ModemManager1/SMS/1',
        modem_index=0,
        from_number='+123456781234567',
        text='later',
    )
    return a
