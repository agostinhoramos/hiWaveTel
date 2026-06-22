"""Shared pytest fixtures."""

from __future__ import annotations

import pytest


@pytest.fixture
def api_client():
    from rest_framework.test import APIClient

    return APIClient()


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
