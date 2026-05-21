"""Create Django superuser from env vars if it does not exist (idempotent bootstrap)."""

from __future__ import annotations

import os

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand


def _env_strip(value: str) -> str:
    return value.strip().strip('"').strip("'")


class Command(BaseCommand):
    help = (
        'Ensure a Django superuser exists using DJANGO_SUPERUSER_USERNAME, '
        'DJANGO_SUPERUSER_EMAIL, and DJANGO_SUPERUSER_PASSWORD from the environment.'
    )

    def handle(self, *args, **options):  # type: ignore[override]
        username = _env_strip(os.environ.get('DJANGO_SUPERUSER_USERNAME', ''))
        email = _env_strip(os.environ.get('DJANGO_SUPERUSER_EMAIL', ''))
        password = _env_strip(os.environ.get('DJANGO_SUPERUSER_PASSWORD', ''))

        if not username:
            self.stdout.write(
                self.style.WARNING(
                    'ensure_superuser: DJANGO_SUPERUSER_USERNAME not set; skipping.'
                )
            )
            return

        User = get_user_model()
        existing = User.objects.filter(username=username).first()
        if existing is not None:
            updated_fields: list[str] = []
            if not existing.is_superuser:
                existing.is_superuser = True
                updated_fields.append('is_superuser')
            if not existing.is_staff:
                existing.is_staff = True
                updated_fields.append('is_staff')
            if not existing.is_active:
                existing.is_active = True
                updated_fields.append('is_active')
            if email and existing.email != email:
                existing.email = email
                updated_fields.append('email')
            if updated_fields:
                existing.save(update_fields=updated_fields)
                self.stdout.write(
                    self.style.SUCCESS(
                        f'ensure_superuser: updated existing user {username!r} '
                        f'({", ".join(updated_fields)}).'
                    )
                )
            else:
                self.stdout.write(
                    f'ensure_superuser: superuser {username!r} already exists; no changes.'
                )
            return

        if not password:
            self.stdout.write(
                self.style.WARNING(
                    f'ensure_superuser: user {username!r} does not exist and '
                    'DJANGO_SUPERUSER_PASSWORD is empty; skipping creation.'
                )
            )
            return

        User.objects.create_superuser(username=username, email=email, password=password)
        self.stdout.write(
            self.style.SUCCESS(f'ensure_superuser: created superuser {username!r}.')
        )
