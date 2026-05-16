"""Shared Django settings loaded by ``development`` and ``production``.

Modem subsystem environment variables remain documented alongside ``manage.py`` workflows.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path


def positive_int_env(name: str, default: int = 0) -> int:
    raw = os.environ.get(name, '').strip()
    if not raw:
        return default
    try:
        v = int(raw)
        return v if v >= 0 else default
    except ValueError:
        return default


def validated_log_level_name(name: str, default: str = 'INFO') -> str:
    """Return a known logging level name for dictConfig (`APPLICATION_LOG_LEVEL`, etc.)."""
    raw = os.environ.get(name, '').strip().upper()
    if not raw:
        return default
    level = getattr(logging, raw, None)
    if isinstance(level, int) and raw != 'NOTSET':
        return raw
    return default


BASE_DIR = Path(__file__).resolve().parent.parent.parent

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'rest_framework',
    'rest_framework_simplejwt',
    'drf_spectacular',
    'apps.sms',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'config.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'config.wsgi.application'

_sqlite_override = os.environ.get('SQLITE_DB_PATH', '').strip()
_db_name = _sqlite_override or (BASE_DIR / 'db.sqlite3')
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': str(_db_name),
    },
}

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'UTC'
USE_I18N = True
USE_TZ = True
STATIC_URL = 'static/'
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# Modem index for ``mmcli -m $N``. Confirm with ``mmcli -L`` where devices differ from the default.
MODEM_MMCLI_INDEX = positive_int_env('MODEM_MMCLI_INDEX', 0)

_modem_health_timeout_raw = os.environ.get('HIWAVE_MMCLI_HEALTH_TIMEOUT', '').strip()
try:
    HIWAVE_MMCLI_HEALTH_TIMEOUT = float(_modem_health_timeout_raw) if _modem_health_timeout_raw else 15.0
except ValueError:
    HIWAVE_MMCLI_HEALTH_TIMEOUT = 15.0

REST_FRAMEWORK = {
    'DEFAULT_SCHEMA_CLASS': 'drf_spectacular.openapi.AutoSchema',
    'DEFAULT_PERMISSION_CLASSES': [
        'rest_framework.permissions.IsAuthenticated',
    ],
    'DEFAULT_AUTHENTICATION_CLASSES': (
        'rest_framework_simplejwt.authentication.JWTAuthentication',
        'rest_framework.authentication.SessionAuthentication',
    ),
    'DEFAULT_PAGINATION_CLASS': 'rest_framework.pagination.PageNumberPagination',
    'PAGE_SIZE': 50,
    'DEFAULT_THROTTLE_CLASSES': [
        'rest_framework.throttling.AnonRateThrottle',
        'rest_framework.throttling.UserRateThrottle',
    ],
    'DEFAULT_THROTTLE_RATES': {
        'anon': '100/day',
        'user': '1000/day',
    },
}

SPECTACULAR_SETTINGS = {
    'TITLE': 'hiWaveTel API',
    'DESCRIPTION': (
        'SMS via ModemManager (mmcli); inbound persisted by D-Bus watcher. '
        'Obtain JWT tokens at `/api/auth/token/` and send `Authorization: Bearer <access>`. '
        'OpenAPI schema `/api/schema/` and Swagger UI `/api/docs/` are public; authenticated '
        '`/api/` SMS routes still require JWT.'
    ),
    'VERSION': '1.0.0',
    'SECURITY_DEFINITIONS': {
        'bearerAuth': {
            'type': 'http',
            'scheme': 'bearer',
            'bearerFormat': 'JWT',
        },
    },
    'SECURITY': [{'bearerAuth': []}],
}

# Persisted logs (single dictConfig — see docs/logging-file-contract.md).
DJANGO_LOG_DIR = os.environ.get('DJANGO_LOG_DIR', '').strip() or str(BASE_DIR / 'logs')
DJANGO_LOG_FILE = Path(os.environ.get('DJANGO_LOG_FILE', 'hiwavetel-api.log').strip() or 'hiwavetel-api.log').name
APPLICATION_LOG_LEVEL = validated_log_level_name('APPLICATION_LOG_LEVEL', 'INFO')
os.makedirs(DJANGO_LOG_DIR, mode=0o755, exist_ok=True)

_LOG_FILE_PATH = str(Path(DJANGO_LOG_DIR) / DJANGO_LOG_FILE)

LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'hiwavetel_file': {
            'format': '%(asctime)s - %(levelname)s - %(message)s',
        },
    },
    'handlers': {
        'file_rotating': {
            'level': APPLICATION_LOG_LEVEL,
            'class': 'logging.handlers.TimedRotatingFileHandler',
            'filename': _LOG_FILE_PATH,
            'when': 'midnight',
            'interval': 1,
            'backupCount': 7,
            'formatter': 'hiwavetel_file',
        },
    },
    'root': {
        'handlers': ['file_rotating'],
        'level': APPLICATION_LOG_LEVEL,
    },
}

SIMPLE_JWT = {}
