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


def truthy_env(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name, '').strip().lower()
    if not raw:
        return default
    return raw in ('1', 'true', 'yes', 'on')


BASE_DIR = Path(__file__).resolve().parent.parent.parent

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'rest_framework',
    'drf_spectacular',
    'apps.sms',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
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

_sqlite_busy_raw = os.environ.get('SQLITE_BUSY_TIMEOUT_SEC', '30').strip()
try:
    SQLITE_BUSY_TIMEOUT_SEC = float(_sqlite_busy_raw) if _sqlite_busy_raw else 30.0
except ValueError:
    SQLITE_BUSY_TIMEOUT_SEC = 30.0
if SQLITE_BUSY_TIMEOUT_SEC <= 0:
    SQLITE_BUSY_TIMEOUT_SEC = 30.0

SQLITE_LOCKED_RETRY_COUNT = positive_int_env('SQLITE_LOCKED_RETRY_COUNT', 15)
if SQLITE_LOCKED_RETRY_COUNT <= 0:
    SQLITE_LOCKED_RETRY_COUNT = 15

_sqlite_backoff_raw = os.environ.get('SQLITE_LOCKED_RETRY_BACKOFF_SEC', '0.02').strip()
try:
    SQLITE_LOCKED_RETRY_BACKOFF_SEC = float(_sqlite_backoff_raw) if _sqlite_backoff_raw else 0.02
except ValueError:
    SQLITE_LOCKED_RETRY_BACKOFF_SEC = 0.02
if SQLITE_LOCKED_RETRY_BACKOFF_SEC <= 0:
    SQLITE_LOCKED_RETRY_BACKOFF_SEC = 0.02

_sqlite_options: dict = {'timeout': SQLITE_BUSY_TIMEOUT_SEC}
_sqlite_pragmas = []
if truthy_env('SQLITE_INIT_WAL', default=True):
    _sqlite_pragmas.append('PRAGMA journal_mode=WAL')
if truthy_env('SQLITE_SYNCHRONOUS_NORMAL', default=True):
    _sqlite_pragmas.append('PRAGMA synchronous=NORMAL')
if _sqlite_pragmas:
    _sqlite_options['init_command'] = '; '.join(_sqlite_pragmas)

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': str(_db_name),
        'OPTIONS': dict(_sqlite_options),
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
STATIC_ROOT = BASE_DIR / 'staticfiles'

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# Modem index for ``mmcli -m $N``. Confirm with ``mmcli -L`` where devices differ from the default.
MODEM_MMCLI_INDEX = positive_int_env('MODEM_MMCLI_INDEX', 0)

_modem_health_timeout_raw = os.environ.get('HIWAVE_MMCLI_HEALTH_TIMEOUT', '').strip()
try:
    HIWAVE_MMCLI_HEALTH_TIMEOUT = float(_modem_health_timeout_raw) if _modem_health_timeout_raw else 15.0
except ValueError:
    HIWAVE_MMCLI_HEALTH_TIMEOUT = 15.0

# Inbound SMS webhook delivery (apps/sms/webhook_delivery.py)
_webhook_timeout_raw = os.environ.get('SMS_WEBHOOK_TIMEOUT_SEC', '').strip()
try:
    SMS_WEBHOOK_TIMEOUT_SEC = float(_webhook_timeout_raw) if _webhook_timeout_raw else 15.0
except ValueError:
    SMS_WEBHOOK_TIMEOUT_SEC = 15.0
SMS_WEBHOOK_RETRY_MAX = positive_int_env('SMS_WEBHOOK_RETRY_MAX', 5)
if SMS_WEBHOOK_RETRY_MAX <= 0:
    SMS_WEBHOOK_RETRY_MAX = 5
_webhook_retry_base_raw = os.environ.get('SMS_WEBHOOK_RETRY_BASE_SEC', '1.0').strip()
try:
    SMS_WEBHOOK_RETRY_BASE_SEC = float(_webhook_retry_base_raw) if _webhook_retry_base_raw else 1.0
except ValueError:
    SMS_WEBHOOK_RETRY_BASE_SEC = 1.0

# Container restart API (apps/sms/container_restart.py)
HIWAVE_ALLOW_CONTAINER_RESTART_API = truthy_env('HIWAVE_ALLOW_CONTAINER_RESTART_API', default=True)
_container_restart_delay_raw = os.environ.get('HIWAVE_CONTAINER_RESTART_DELAY_SEC', '1.0').strip()
try:
    HIWAVE_CONTAINER_RESTART_DELAY_SEC = (
        float(_container_restart_delay_raw) if _container_restart_delay_raw else 1.0
    )
except ValueError:
    HIWAVE_CONTAINER_RESTART_DELAY_SEC = 1.0

# SMS storage rotation (management command cleanup_sms_storage)
SMS_STORAGE_ROTATION_ENABLED = truthy_env('SMS_STORAGE_ROTATION_ENABLED', default=True)
SMS_MAX_MESSAGES_PER_MODEM = positive_int_env('SMS_MAX_MESSAGES_PER_MODEM', 2000)
if SMS_MAX_MESSAGES_PER_MODEM <= 0:
    SMS_MAX_MESSAGES_PER_MODEM = 2000
SMS_ROTATION_BATCH_SIZE = positive_int_env('SMS_ROTATION_BATCH_SIZE', 100)
if SMS_ROTATION_BATCH_SIZE <= 0:
    SMS_ROTATION_BATCH_SIZE = 100
SMS_CLEANUP_ON_STARTUP = truthy_env('SMS_CLEANUP_ON_STARTUP', default=False)

# SMS reliability & recovery (apps/sms/dbus_watch.py, dead_letter_queue.py)
MODEM_SNAPSHOT_RECOVERY_INTERVAL_SEC = positive_int_env('MODEM_SNAPSHOT_RECOVERY_INTERVAL_SEC', 300)
SMS_DLQ_ENABLED = truthy_env('SMS_DLQ_ENABLED', default=True)
SMS_DLQ_DB_PATH = os.environ.get('SMS_DLQ_DB_PATH', str(BASE_DIR / 'dlq_sms.db'))
SMS_DLQ_MAX_SIZE = positive_int_env('SMS_DLQ_MAX_SIZE', 1000)
SMS_DLQ_RETRY_INTERVAL_SEC = positive_int_env('SMS_DLQ_RETRY_INTERVAL_SEC', 60)
SMS_DLQ_MAX_RETRIES = positive_int_env('SMS_DLQ_MAX_RETRIES', 10)
SMS_DEBUG_LOGGING = truthy_env('SMS_DEBUG_LOGGING', default=False)

# Inbound SMS whitelist: only accept SMS from these numbers (comma-separated, no whitespace)
# Empty string = whitelist disabled (accept all)
_whitelist_raw = os.environ.get('SMS_INBOUND_WHITELIST', '').strip()
SMS_INBOUND_WHITELIST = [n.strip() for n in _whitelist_raw.split(',') if n.strip()] if _whitelist_raw else []

# Extended mmcli polling while ModemManager reports state=receiving (apps/sms/services.py)
MMCLI_RECEIVING_MAX_WAIT_SEC = positive_int_env('MMCLI_RECEIVING_MAX_WAIT_SEC', 60)
if MMCLI_RECEIVING_MAX_WAIT_SEC <= 0:
    MMCLI_RECEIVING_MAX_WAIT_SEC = 60

# Inbound SMS post-save processor queue (apps/sms/inbound_processor.py)
INBOUND_PROCESSOR_WORKERS = positive_int_env('INBOUND_PROCESSOR_WORKERS', 2)
INBOUND_PROCESSOR_MAX_SIZE = positive_int_env('INBOUND_PROCESSOR_MAX_SIZE', 500)
INBOUND_PROCESSOR_RETRY_MAX = positive_int_env('INBOUND_PROCESSOR_RETRY_MAX', 5)
_retry_base_raw = os.environ.get('INBOUND_PROCESSOR_RETRY_BASE_SEC', '1.0').strip()
try:
    INBOUND_PROCESSOR_RETRY_BASE_SEC = float(_retry_base_raw) if _retry_base_raw else 1.0
except ValueError:
    INBOUND_PROCESSOR_RETRY_BASE_SEC = 1.0
if INBOUND_PROCESSOR_RETRY_BASE_SEC <= 0:
    INBOUND_PROCESSOR_RETRY_BASE_SEC = 1.0

# Outbound SMS async processor (apps/sms/outbound_processor.py)
OUTBOUND_ASYNC_ENABLED = truthy_env('OUTBOUND_ASYNC_ENABLED', default=False)
OUTBOUND_PROCESSOR_WORKERS = positive_int_env('OUTBOUND_PROCESSOR_WORKERS', 1)
OUTBOUND_PROCESSOR_MAX_SIZE = positive_int_env('OUTBOUND_PROCESSOR_MAX_SIZE', 10000)
_outbox_poll_raw = os.environ.get('OUTBOUND_OUTBOX_POLL_SEC', '0.1').strip()
try:
    OUTBOUND_OUTBOX_POLL_SEC = float(_outbox_poll_raw) if _outbox_poll_raw else 0.1
except ValueError:
    OUTBOUND_OUTBOX_POLL_SEC = 0.1

REST_FRAMEWORK = {
    'DEFAULT_SCHEMA_CLASS': 'drf_spectacular.openapi.AutoSchema',
    'DEFAULT_PERMISSION_CLASSES': [
        'rest_framework.permissions.AllowAny',
    ],
    'DEFAULT_AUTHENTICATION_CLASSES': [],
}

SPECTACULAR_SETTINGS = {
    'TITLE': 'hiWaveTel API',
    'DESCRIPTION': (
        'SMS gateway via ModemManager (mmcli).\n\n'
        '- **Send SMS:** `POST /api/sms/send/` — no authentication.\n'
        '- **Modems:** `GET/PUT /api/sms/modems/{id}/`, `POST /api/sms/modems/sync/`.\n'
        '- **Inbound SMS:** delivered to HTTP webhooks registered per modem.\n'
        '- **System:** `GET /api/health/`, availability probe, `POST /api/sms/system/container/restart/`.\n\n'
        'Configure inbound webhooks via API or Django Admin → Inbound webhooks.'
    ),
    'VERSION': '2.0.0',
    'SECURITY': [],
    'SWAGGER_UI_SETTINGS': {
        'deepLinking': True,
    },
}

# Persisted logs (single dictConfig — see docs/logging-file-contract.md).
DJANGO_LOG_DIR = os.environ.get('DJANGO_LOG_DIR', '').strip() or str(BASE_DIR / 'logs')
DJANGO_LOG_FILE = Path(os.environ.get('DJANGO_LOG_FILE', 'hiwavetel-api.log').strip() or 'hiwavetel-api.log').name
APPLICATION_LOG_LEVEL = validated_log_level_name('APPLICATION_LOG_LEVEL', 'INFO')
os.makedirs(DJANGO_LOG_DIR, mode=0o755, exist_ok=True)

_LOG_FILE_PATH = str(Path(DJANGO_LOG_DIR) / DJANGO_LOG_FILE)

class HealthProbeFilter(logging.Filter):
    """Suppress 5xx ERROR logs from health endpoint during startup."""
    def filter(self, record):
        msg = record.getMessage()
        # Suppress ERROR logs for health endpoint 503 responses
        if record.levelno >= logging.ERROR and '/api/health/' in msg:
            return False
        return True


LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'filters': {
        'health_probe_filter': {
            '()': 'django.utils.log.CallbackFilter',
            'callback': lambda record: not (record.levelno >= logging.ERROR and '/api/health/' in record.getMessage()),
        },
    },
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
            'filters': ['health_probe_filter'],
        },
        'console': {
            'level': APPLICATION_LOG_LEVEL,
            'class': 'logging.StreamHandler',
            'stream': 'ext://sys.stdout',
            'formatter': 'hiwavetel_file',
            'filters': ['health_probe_filter'],
        },
    },
    'loggers': {
        'django.request': {
            'handlers': ['file_rotating', 'console'],
            'level': 'WARNING',
            'propagate': False,
        },
        'django.server': {
            'handlers': ['file_rotating', 'console'],
            'level': 'WARNING',
            'propagate': False,
        },
    },
    'root': {
        'handlers': ['file_rotating', 'console'],
        'level': APPLICATION_LOG_LEVEL,
    },
}

