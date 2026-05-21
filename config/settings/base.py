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


def mqtt_credential_strip(value: str) -> str:
    """Strip whitespace and stray quotes from MQTT env vars (docker/.env tolerant)."""
    return value.strip().strip('"').strip("'")


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
    'rest_framework_simplejwt',
    'drf_spectacular',
    'apps.sms',
    'apps.external_device',
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

# MQTT settings for external device gateway (broker connection)
MQTT_BROKER_URL = os.environ.get('MQTT_BROKER_URL', 'localhost').strip()
MQTT_PORT = positive_int_env('MQTT_PORT', 1883)
MQTT_USER = mqtt_credential_strip(os.environ.get('MQTT_USER', '') or '')
MQTT_PASS = mqtt_credential_strip(os.environ.get('MQTT_PASS', '') or '')
MQTT_CLIENT_ID = os.environ.get('MQTT_CLIENT_ID', 'hiwavetel_gateway').strip()
MQTT_KEEPALIVE = positive_int_env('MQTT_KEEPALIVE', 60)
MQTT_QOS = positive_int_env('MQTT_QOS', 1)
MQTT_CLEAN_SESSION = os.environ.get('MQTT_CLEAN_SESSION', 'false').lower() == 'true'
MQTT_EXTERNAL_TOPIC_PREFIX = os.environ.get('MQTT_EXTERNAL_TOPIC_PREFIX', 'hidishelink_dev').strip()

# Modem/catalog MQTT subtree (hiDisheLink: MQTT_BASE_TOPIC_PREFIX). Defaults to MQTT_EXTERNAL_TOPIC_PREFIX.
MQTT_BASE_TOPIC_PREFIX = os.environ.get('MQTT_BASE_TOPIC_PREFIX', '').strip()
if not MQTT_BASE_TOPIC_PREFIX:
    MQTT_BASE_TOPIC_PREFIX = MQTT_EXTERNAL_TOPIC_PREFIX

# Device-topic root (hiDisheLink: MQTT_DEVICE_TOPIC_PREFIX). Empty → derive at runtime as {MQTT_BASE_TOPIC_PREFIX}/devices.
# Django/os.environ does not expand docker-compose ${VAR}; expand one common pattern explicitly.
_mqtt_dev_topic_raw = os.environ.get('MQTT_DEVICE_TOPIC_PREFIX', '').strip()
_mqtt_dev_ph = '${MQTT_BASE_TOPIC_PREFIX}'
if _mqtt_dev_topic_raw.startswith(_mqtt_dev_ph):
    _mqtt_dev_topic_raw = MQTT_BASE_TOPIC_PREFIX.rstrip('/') + _mqtt_dev_topic_raw[len(_mqtt_dev_ph) :]
MQTT_DEVICE_TOPIC_PREFIX = _mqtt_dev_topic_raw

MQTT_PUBLISH_SEND_REQUEST = truthy_env('MQTT_PUBLISH_SEND_REQUEST', default=True)
MQTT_PUBLISH_MODEM_INBOX = truthy_env('MQTT_PUBLISH_MODEM_INBOX', default=False)
_ephemeral_publish_timeout_raw = os.environ.get('MQTT_EPHEMERAL_PUBLISH_TIMEOUT_SEC', '').strip()
try:
    MQTT_EPHEMERAL_PUBLISH_TIMEOUT_SEC = (
        float(_ephemeral_publish_timeout_raw) if _ephemeral_publish_timeout_raw else 15.0
    )
except ValueError:
    MQTT_EPHEMERAL_PUBLISH_TIMEOUT_SEC = 15.0
if MQTT_EPHEMERAL_PUBLISH_TIMEOUT_SEC <= 0:
    MQTT_EPHEMERAL_PUBLISH_TIMEOUT_SEC = 15.0

_mqtt_modem_inbox_mode_raw = os.environ.get('MQTT_MODEM_INBOX_DELIVERY_MODE', 'broadcast').strip().lower()
MQTT_MODEM_INBOX_DELIVERY_MODE = 'per_device' if _mqtt_modem_inbox_mode_raw == 'per_device' else 'broadcast'
MQTT_MODEM_STATUS_SUBSCRIBE = truthy_env('MQTT_MODEM_STATUS_SUBSCRIBE', default=True)
_modem_status_timeout_raw = os.environ.get('MQTT_MODEM_STATUS_COMMAND_TIMEOUT_SEC', '').strip()
try:
    MQTT_MODEM_STATUS_COMMAND_TIMEOUT_SEC = (
        float(_modem_status_timeout_raw) if _modem_status_timeout_raw else 45.0
    )
except ValueError:
    MQTT_MODEM_STATUS_COMMAND_TIMEOUT_SEC = 45.0
if MQTT_MODEM_STATUS_COMMAND_TIMEOUT_SEC <= 0:
    MQTT_MODEM_STATUS_COMMAND_TIMEOUT_SEC = 45.0

MQTT_MODEM_STATUS_AUTO_PUBLISH = truthy_env('MQTT_MODEM_STATUS_AUTO_PUBLISH', default=True)
_modem_status_poll_interval_raw = os.environ.get('MQTT_MODEM_STATUS_POLL_INTERVAL_SEC', '').strip()
try:
    MQTT_MODEM_STATUS_POLL_INTERVAL_SEC = (
        float(_modem_status_poll_interval_raw) if _modem_status_poll_interval_raw else 30.0
    )
except ValueError:
    MQTT_MODEM_STATUS_POLL_INTERVAL_SEC = 30.0
if MQTT_MODEM_STATUS_POLL_INTERVAL_SEC < 0:
    MQTT_MODEM_STATUS_POLL_INTERVAL_SEC = 30.0

MQTT_SUBSCRIBE_MODEM_CATALOG = truthy_env('MQTT_SUBSCRIBE_MODEM_CATALOG', default=True)
# Whether the gateway subscribes to the health/ping wildcard at all (telemetry + pong handling).
# Independent of MQTT_HEALTH_AUTO_PONG: disabling subscription stops telemetry persistence too.
MQTT_HEALTH_PING_SUBSCRIBE = truthy_env('MQTT_HEALTH_PING_SUBSCRIBE', default=True)
MQTT_HEALTH_AUTO_PONG = truthy_env('MQTT_HEALTH_AUTO_PONG', default=True)
MQTT_HEALTH_PING_SUBSCRIBE_QOS = positive_int_env('MQTT_HEALTH_PING_SUBSCRIBE_QOS', 0)
if MQTT_HEALTH_PING_SUBSCRIBE_QOS > 2:
    MQTT_HEALTH_PING_SUBSCRIBE_QOS = 0
MQTT_HEALTH_SUBSCRIBE_PONG = truthy_env('MQTT_HEALTH_SUBSCRIBE_PONG', default=True)

# run_mqtt_gateway: publish tipo B health/ping (source=django) for each active ExternalDevice on this interval (0 = off)
_mqtt_srv_ping_interval_raw = os.environ.get('MQTT_HEALTH_SERVER_PING_INTERVAL_SEC', '60').strip()
try:
    MQTT_HEALTH_SERVER_PING_INTERVAL_SEC = (
        float(_mqtt_srv_ping_interval_raw) if _mqtt_srv_ping_interval_raw else 60.0
    )
except ValueError:
    MQTT_HEALTH_SERVER_PING_INTERVAL_SEC = 0.0
if MQTT_HEALTH_SERVER_PING_INTERVAL_SEC < 0:
    MQTT_HEALTH_SERVER_PING_INTERVAL_SEC = 0.0

MQTT_SEND_RECIPIENTS_CHUNK_SIZE = positive_int_env('MQTT_SEND_RECIPIENTS_CHUNK_SIZE', 50)
if MQTT_SEND_RECIPIENTS_CHUNK_SIZE <= 0:
    MQTT_SEND_RECIPIENTS_CHUNK_SIZE = 50

# Remote hiDisheLink REST (admin integration — fetch MQTT config, device onboarding)
HIDISHELINK_API_URL = os.environ.get('HIDISHELINK_API_URL', 'http://192.168.1.77:5201').strip().rstrip('/')
HIDISHELINK_API_TIMEOUT_SEC = positive_int_env('HIDISHELINK_API_TIMEOUT_SEC', 30)
if HIDISHELINK_API_TIMEOUT_SEC <= 0:
    HIDISHELINK_API_TIMEOUT_SEC = 30

# Android `/api/sms/device/` session TTL (login + refresh)
HIDISHELINK_SESSION_TTL_HOURS = positive_int_env('HIDISHELINK_SESSION_TTL_HOURS', 24)
if HIDISHELINK_SESSION_TTL_HOURS <= 0:
    HIDISHELINK_SESSION_TTL_HOURS = 24

# Optional jitter fraction for mqtt-config map (sec. 5 client contract)
_raw_jitter = os.environ.get('MQTT_RECONNECT_JITTER', '0.2').strip()
try:
    MQTT_RECONNECT_JITTER = float(_raw_jitter) if _raw_jitter else 0.2
except ValueError:
    MQTT_RECONNECT_JITTER = 0.2

# Optional extra / legacy toggle; django tipo B pings also honor MQTT_HEALTH_AUTO_PONG above.
MQTT_HEALTH_GATEWAY_AUTO_PONG_DJANGO = truthy_env('MQTT_HEALTH_GATEWAY_AUTO_PONG_DJANGO', default=False)

# Default SMS inbox flag exposed to Android mqtt-config (`SMS_INBOX_ENABLED`)
SMS_INBOX_ENABLED_DEFAULT = truthy_env('SMS_INBOX_ENABLED_DEFAULT', default=True)

# Remote hiDisheLink bridge mode settings
MQTT_REMOTE_BRIDGE_ENABLED = truthy_env('MQTT_REMOTE_BRIDGE_ENABLED', default=True)
MQTT_LOCAL_BROKER_ENABLED = truthy_env('MQTT_LOCAL_BROKER_ENABLED', default=True)

# Remote client device_id (from HiDishelinkDevice or env)
MQTT_REMOTE_DEVICE_ID = os.environ.get('MQTT_REMOTE_DEVICE_ID', '').strip()

# Health heartbeat interval (s) for remote broker (tipo A telemetry without source:django)
MQTT_REMOTE_HEALTH_HEARTBEAT_SEC = positive_int_env('MQTT_REMOTE_HEALTH_HEARTBEAT_SEC', 60)
if MQTT_REMOTE_HEALTH_HEARTBEAT_SEC <= 0:
    MQTT_REMOTE_HEALTH_HEARTBEAT_SEC = 60

# Gateway startup: fetch mqtt-config from remote API or use cached snapshot
MQTT_CONFIG_STARTUP_REFRESH = truthy_env('MQTT_CONFIG_STARTUP_REFRESH', default=False)

# SMS storage rotation (apps/external_device/management/commands/cleanup_sms_storage.py)
SMS_STORAGE_ROTATION_ENABLED = truthy_env('SMS_STORAGE_ROTATION_ENABLED', default=True)
SMS_MAX_MESSAGES_PER_DEVICE = positive_int_env('SMS_MAX_MESSAGES_PER_DEVICE', 1000)
if SMS_MAX_MESSAGES_PER_DEVICE <= 0:
    SMS_MAX_MESSAGES_PER_DEVICE = 1000
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
# Async processing of InboundSms mirror + MQTT publish with retry logic
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

# MQTT client tuning (mirrors hiDisheLink mqtt-config; optional fallbacks when no remote config)
MQTT_AUTO_RECONNECT = truthy_env('MQTT_AUTO_RECONNECT', default=True)
MQTT_CONNECTION_TIMEOUT = positive_int_env('MQTT_CONNECTION_TIMEOUT', 30)
MQTT_RECONNECT_INITIAL_DELAY_MS = positive_int_env('MQTT_RECONNECT_INITIAL_DELAY_MS', 1000)
MQTT_RECONNECT_MAX_DELAY_MS = positive_int_env('MQTT_RECONNECT_MAX_DELAY_MS', 30000)
_raw_bm = os.environ.get('MQTT_RECONNECT_BACKOFF_MULTIPLIER', '2').strip()
try:
    MQTT_RECONNECT_BACKOFF_MULTIPLIER = float(_raw_bm) if _raw_bm else 2.0
except ValueError:
    MQTT_RECONNECT_BACKOFF_MULTIPLIER = 2.0
MQTT_RECONNECT_MAX_RETRIES = positive_int_env('MQTT_RECONNECT_MAX_RETRIES', 0)
MQTT_CONNECTION_WATCHDOG_INTERVAL_MS = positive_int_env('MQTT_CONNECTION_WATCHDOG_INTERVAL_MS', 0)

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
        'SMS via ModemManager (mmcli); inbound persisted by D-Bus watcher.\n\n'
        '- **Modem API** (`/api/sms/…`): JWT from `/api/auth/token/` — in Swagger authorize '
        'scheme **jwtAuth** (Bearer).\n'
        '- **External devices** (`/api/v1/…`): API key — in Swagger authorize scheme '
        '**apiKeyAuth** (header `X-API-Key`). Bearer JWT is not accepted on these routes.\n\n'
        'OpenAPI `/api/schema/` and Swagger UI `/api/docs/` are public; try endpoints still '
        'require the correct auth per route.'
    ),
    'VERSION': '1.0.0',
    # Do not set SECURITY globally: it was appended to every operation and forced Bearer on
    # `/api/v1/*` while the server expects X-API-Key. Per-endpoint security comes from auth
    # classes (jwtAuth / apiKeyAuth via OpenApiAuthenticationExtension).
    'SECURITY': [],
    'SWAGGER_UI_SETTINGS': {
        'deepLinking': True,
        # Keep Authorize dialog values after refresh (localStorage).
        'persistAuthorization': True,
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

SIMPLE_JWT = {}
