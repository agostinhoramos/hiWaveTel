"""Management command wiring (mocked asyncio / lightweight listener coroutine)."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from django.core.management import call_command

from apps.sms.management.commands import run_sms_watcher as cmd_mod


def test_run_sms_watcher_keyboard_interrupt(capsys):
    """Exit cleanly after ``KeyboardInterrupt`` from ``asyncio.run``."""

    def abort_run(coroutine_main):
        coroutine_main.close()
        raise KeyboardInterrupt

    with patch.object(cmd_mod.asyncio, 'run', side_effect=abort_run):
        cmd_mod.Command().handle(
            modem_index=0,
            reconnect_after=5.0,
            skip_initial_sync=False,
            all_modems=False,
        )

    out = capsys.readouterr().out.lower()
    assert 'interrupt' in out


@pytest.mark.parametrize(('skip_via_cli_flag', 'expected_snapshot'), [(True, False), (False, True)])
def test_run_command_maps_skip_sync_to_listener_snapshot(monkeypatch, skip_via_cli_flag, expected_snapshot):
    """``asyncio.run`` completes instantly using a patched listener coroutine."""

    captured: dict[str, object] = {}

    async def fake_listener(modem_index: int, reconnect_after: float, *, initial_snapshot: bool = False) -> None:
        captured['modem_index'] = modem_index
        captured['reconnect_after'] = reconnect_after
        captured['initial_snapshot'] = initial_snapshot

    monkeypatch.setattr(cmd_mod, 'run_modem_added_listener', fake_listener)

    kwargs = {'skip_initial_sync': True} if skip_via_cli_flag else {}
    call_command('run_sms_watcher', modem_index=8, reconnect_after=3.125, **kwargs)

    assert captured['modem_index'] == 8
    assert captured['reconnect_after'] == 3.125
    assert captured['initial_snapshot'] is expected_snapshot


@pytest.mark.django_db
def test_ensure_superuser_creates_from_env(monkeypatch):
    """First run creates superuser from DJANGO_SUPERUSER_* env vars."""
    from django.contrib.auth import get_user_model

    monkeypatch.setenv('DJANGO_SUPERUSER_USERNAME', 'bootstrap_admin')
    monkeypatch.setenv('DJANGO_SUPERUSER_EMAIL', 'bootstrap@test.invalid')
    monkeypatch.setenv('DJANGO_SUPERUSER_PASSWORD', 'bootstrap-pw-123')

    User = get_user_model()
    assert not User.objects.filter(username='bootstrap_admin').exists()

    call_command('ensure_superuser')

    user = User.objects.get(username='bootstrap_admin')
    assert user.is_superuser
    assert user.is_staff
    assert user.is_active
    assert user.email == 'bootstrap@test.invalid'
    assert user.check_password('bootstrap-pw-123')


@pytest.mark.django_db
def test_ensure_superuser_idempotent(monkeypatch):
    """Second run does not duplicate user or change password."""
    from django.contrib.auth import get_user_model

    monkeypatch.setenv('DJANGO_SUPERUSER_USERNAME', 'bootstrap_dup')
    monkeypatch.setenv('DJANGO_SUPERUSER_EMAIL', 'dup@test.invalid')
    monkeypatch.setenv('DJANGO_SUPERUSER_PASSWORD', 'first-password')

    User = get_user_model()
    call_command('ensure_superuser')
    assert User.objects.filter(username='bootstrap_dup').count() == 1

    monkeypatch.setenv('DJANGO_SUPERUSER_PASSWORD', 'second-password-should-not-apply')
    call_command('ensure_superuser')

    assert User.objects.filter(username='bootstrap_dup').count() == 1
    user = User.objects.get(username='bootstrap_dup')
    assert user.check_password('first-password')
    assert not user.check_password('second-password-should-not-apply')


@pytest.mark.django_db
def test_ensure_superuser_repairs_existing_user_flags(monkeypatch):
    """Existing non-superuser gets staff/superuser flags without password reset."""
    from django.contrib.auth import get_user_model

    User = get_user_model()
    User.objects.create_user(
        username='repair_me',
        email='old@test.invalid',
        password='keep-this-password',
        is_superuser=False,
        is_staff=False,
        is_active=True,
    )

    monkeypatch.setenv('DJANGO_SUPERUSER_USERNAME', 'repair_me')
    monkeypatch.setenv('DJANGO_SUPERUSER_EMAIL', 'new@test.invalid')
    monkeypatch.setenv('DJANGO_SUPERUSER_PASSWORD', 'ignored-new-password')

    call_command('ensure_superuser')

    user = User.objects.get(username='repair_me')
    assert user.is_superuser
    assert user.is_staff
    assert user.is_active
    assert user.email == 'new@test.invalid'
    assert user.check_password('keep-this-password')


@pytest.mark.django_db
def test_ensure_superuser_skips_when_username_missing(monkeypatch, capsys):
    monkeypatch.delenv('DJANGO_SUPERUSER_USERNAME', raising=False)
    monkeypatch.setenv('DJANGO_SUPERUSER_PASSWORD', 'pw')

    call_command('ensure_superuser')

    out = capsys.readouterr().out.lower()
    assert 'skipping' in out


@pytest.mark.django_db
def test_ensure_superuser_strips_quotes_from_password(monkeypatch):
    """Password values wrapped in quotes (common in .env) are stripped."""
    from django.contrib.auth import get_user_model

    monkeypatch.setenv('DJANGO_SUPERUSER_USERNAME', 'quoted_pw_user')
    monkeypatch.setenv('DJANGO_SUPERUSER_EMAIL', 'quoted@test.invalid')
    monkeypatch.setenv('DJANGO_SUPERUSER_PASSWORD', "'quoted-password'")

    call_command('ensure_superuser')

    user = get_user_model().objects.get(username='quoted_pw_user')
    assert user.check_password('quoted-password')
