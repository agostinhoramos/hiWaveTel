"""Production settings — require explicit secrets and non-wildcard hosts."""

from __future__ import annotations

import os

from django.core.exceptions import ImproperlyConfigured

from .base import *  # noqa: F403

DEBUG = False
if os.environ.get('DJANGO_DEBUG', '').strip().lower() in {'1', 'true', 'yes', 'on'}:
    DEBUG = True

SECRET_KEY = os.environ.get('DJANGO_SECRET_KEY', '').strip()
if not SECRET_KEY:
    raise ImproperlyConfigured('DJANGO_SECRET_KEY must be set when DJANGO_ENV=production.')

_allowed = os.environ.get('DJANGO_ALLOWED_HOSTS', '').strip()
if not _allowed:
    raise ImproperlyConfigured('DJANGO_ALLOWED_HOSTS must list at least one host in production.')

ALLOWED_HOSTS = [h.strip() for h in _allowed.split(',') if h.strip()]

forbidden = {'', '*'}
if any(h in forbidden for h in ALLOWED_HOSTS):
    raise ImproperlyConfigured("DJANGO_ALLOWED_HOSTS must not be empty or '*'; list explicit hosts.")
