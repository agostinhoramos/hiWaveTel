"""Bootstrap Django superuser from environment variables."""

from __future__ import annotations

import os

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand


def _strip_env(value: str) -> str:
    return value.strip().strip('"').strip("'")


class Command(BaseCommand):
    help = 'Create or repair superuser from DJANGO_SUPERUSER_* environment variables.'

    def handle(self, *args, **options):
        username = _strip_env(os.environ.get('DJANGO_SUPERUSER_USERNAME', ''))
        password = _strip_env(os.environ.get('DJANGO_SUPERUSER_PASSWORD', ''))
        email = _strip_env(os.environ.get('DJANGO_SUPERUSER_EMAIL', ''))

        if not username:
            self.stdout.write('ensure_superuser: skipping (DJANGO_SUPERUSER_USERNAME not set).')
            return

        if not password:
            self.stdout.write('ensure_superuser: skipping (DJANGO_SUPERUSER_PASSWORD not set).')
            return

        User = get_user_model()
        user = User.objects.filter(username=username).first()
        if user is None:
            User.objects.create_superuser(username=username, email=email or '', password=password)
            self.stdout.write(f'ensure_superuser: created superuser {username!r}.')
            return

        changed = False
        if email and user.email != email:
            user.email = email
            changed = True
        if not user.is_superuser:
            user.is_superuser = True
            changed = True
        if not user.is_staff:
            user.is_staff = True
            changed = True
        if not user.is_active:
            user.is_active = True
            changed = True
        if changed:
            user.save()
            self.stdout.write(f'ensure_superuser: updated flags for existing user {username!r}.')
        else:
            self.stdout.write(f'ensure_superuser: user {username!r} already exists (password unchanged).')
