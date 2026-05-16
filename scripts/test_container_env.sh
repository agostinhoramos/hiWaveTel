#!/usr/bin/env bash
# Validate mmcli/TTY inside the hiwavetel container (scripts under `/app/scripts` ship in the image).
set -euo pipefail

log() { echo "[container-env] $*"; }
warn() { echo "[container-env] WARN: $*" >&2; }
fail() { echo "[container-env] FAIL: $*" >&2; exit 1; }

command -v mmcli >/dev/null 2>&1 || fail "mmcli not found in container."

: "${MODEM_TTY_PRIMARY:=/dev/ttyUSB2}"
if [[ ! -e "${MODEM_TTY_PRIMARY}" ]]; then
  warn "Primary TTY missing (${MODEM_TTY_PRIMARY})."
fi

FIRST_MODEM=""
if LIST_OUT="$(mmcli -L 2>/dev/null)"; then
  FIRST_MODEM="$(echo "${LIST_OUT}" | grep -oE '/org/freedesktop/ModemManager1/Modem/[[:digit:]]+' | head -n1 || true)"
fi
[[ -n "${FIRST_MODEM}" ]] || fail "mmcli -L shows no modem inside the container."

INDEX="${FIRST_MODEM##*/Modem/}"
log "mmcli modem index=${INDEX} MODEM_MMCLI_INDEX env=${MODEM_MMCLI_INDEX:-unset}"

PYTHONPATH=/app DJANGO_SETTINGS_MODULE=config.settings python - <<'PY' || fail "Django failed to import settings."
import os

import django

os.chdir("/app")
django.setup()
from django.conf import settings

print(f"Django MODEM_MMCLI_INDEX={getattr(settings, 'MODEM_MMCLI_INDEX', None)}")
PY

PORT="${HIWAVE_PORT:-${PORT:-8000}}"
code="$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:${PORT}/api/schema/")"
case "${code}" in
  200) log "/api/schema/ returned 200 — OpenAPI endpoint is served without auth." ;;
  401 | 403) warn "/api/schema/ returned ${code} — expected 200 unless schema is locked down." ;;
  *) warn "/api/schema/ unexpected HTTP ${code} — confirm Gunicorn on ${PORT} matches HIWAVE_PORT." ;;
esac

curl -sf "http://127.0.0.1:${PORT}/api/health/" >/dev/null || warn "curl /api/health failed (non-HTTP-200 or service down)"

log "OK --- container environment checklist complete."
