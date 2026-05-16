"""Development settings — DEBUG on, forgiving defaults for localhost."""

from __future__ import annotations

import os

from .base import *  # noqa: F403

DEBUG = True

_secret = os.environ.get('DJANGO_SECRET_KEY', '').strip()
SECRET_KEY = _secret or 'django-insecure-dev-change-me-before-sharing'

_allowed = os.environ.get('DJANGO_ALLOWED_HOSTS', '').strip()
if _allowed:
    ALLOWED_HOSTS = [h.strip() for h in _allowed.split(',') if h.strip()]
else:
    ALLOWED_HOSTS = ['localhost', '127.0.0.1', '[::1]']
