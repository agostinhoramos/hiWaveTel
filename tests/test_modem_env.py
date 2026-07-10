"""Tests for per-modem environment variables."""

from __future__ import annotations

import pytest

from apps.sms.modem_env import (
    get_modem_env,
    get_modem_phone_number,
    get_modem_pin_code,
    list_modem_indices_from_env,
    modem_env_key,
)


def test_modem_env_key():
    assert modem_env_key(0, 'DEVICE_PIN_CODE') == 'MODEM_0_DEVICE_PIN_CODE'
    assert modem_env_key(3, 'DEVICE_PHONE_NUMBER') == 'MODEM_3_DEVICE_PHONE_NUMBER'


def test_get_modem_pin_code_strips_quotes(monkeypatch):
    monkeypatch.setenv('MODEM_0_DEVICE_PIN_CODE', "'1369'")
    assert get_modem_pin_code(0) == '1369'


def test_get_modem_phone_number_normalizes(monkeypatch):
    monkeypatch.setenv('MODEM_0_DEVICE_PHONE_NUMBER', '351961343706')
    assert get_modem_phone_number(0) == '+351961343706'


def test_get_modem_env_missing_returns_default():
    assert get_modem_env(99, 'DEVICE_PIN_CODE') == ''


def test_get_modem_pin_code_single_modem_fallback(monkeypatch):
    monkeypatch.setenv('MODEM_0_DEVICE_PIN_CODE', '1369')
    assert get_modem_pin_code(1) == '1369'


def test_get_modem_pin_code_no_fallback_when_multiple_pins(monkeypatch):
    monkeypatch.setenv('MODEM_0_DEVICE_PIN_CODE', '1111')
    monkeypatch.setenv('MODEM_1_DEVICE_PIN_CODE', '2222')
    assert get_modem_pin_code(2) == ''


def test_list_modem_indices_from_env(monkeypatch):
    monkeypatch.setenv('MODEM_0_DEVICE_PIN_CODE', '1111')
    monkeypatch.setenv('MODEM_2_DEVICE_PHONE_NUMBER', '351900000002')
    monkeypatch.setenv('UNRELATED_VAR', 'x')
    assert list_modem_indices_from_env() == [0, 2]
