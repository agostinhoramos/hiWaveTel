"""Select Django settings via ``DJANGO_ENV`` ("development"|"production").

Defaults to development-friendly settings for local shells and Compose without extra env vars.
"""

from __future__ import annotations

import os

_env_raw = (
    os.environ.get('DJANGO_ENV')
    or os.environ.get('DJANGO_CONFIGURATION')
    or os.environ.get('DJANGO_STAGE')
    or 'development'
)
_env = _env_raw.strip().lower()

if _env in {'production', 'prod', 'staging'}:
    from .production import *  # noqa: F403,F401
elif _env in {'development', 'dev', 'local', '', 'testing', 'test'}:
    # "test"/"pytest" callers should set DJANGO_ENV=production if parity is required,
    # or rely on sqlite + dev defaults typical for Django's test runner.
    from .development import *  # noqa: F403,F401
else:
    raise ValueError(f"Unknown DJANGO_ENV={_env_raw!r}; use 'development' or 'production'.")