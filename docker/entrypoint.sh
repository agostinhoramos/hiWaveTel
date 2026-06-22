#!/usr/bin/env bash
# dbus system + ModemManager + optional SIM unlock + Gunicorn.
set -euo pipefail

truthy() {
  case "$(echo "${1:-}" | tr '[:upper:]' '[:lower:]')" in
    1 | true | yes | on) return 0 ;;
    *) return 1 ;;
  esac
}

# Docker file bind-mounts create a directory when the host file is missing; recover and create the DB file.
ensure_sqlite_file() {
  local db_path="${1:-}"
  [[ -n "${db_path}" ]] || return 0

  local parent
  parent="$(dirname "${db_path}")"
  mkdir -p "${parent}"

  if [[ -d "${db_path}" ]]; then
    if find "${db_path}" -mindepth 1 -print -quit 2>/dev/null | grep -q .; then
      echo "SQLITE_DB_PATH is a non-empty directory: ${db_path}" >&2
      exit 1
    fi
    rmdir "${db_path}"
    echo "Removed empty SQLite directory (Docker bind-mount artefact): ${db_path}"
  fi

  if [[ ! -f "${db_path}" ]]; then
    touch "${db_path}"
    echo "Created SQLite database: ${db_path}"
  fi
}

# Prefer ModemManager /modem path reported by mmcli (avoids hard-coded indices).
mmcli_primary_modem_dbuss_path() {
  mmcli -L 2>/dev/null | grep -oE '/org/freedesktop/ModemManager1/Modem/[[:digit:]]+' | head -n1
}

# Expected ports/interfaces (ttyUSB2/3 + wwan0 on host when visible here).
MODEM_TTY_PRIMARY="${MODEM_TTY_PRIMARY:-/dev/ttyUSB2}"
MODEM_TTY_SECONDARY="${MODEM_TTY_SECONDARY:-/dev/ttyUSB3}"
MODEM_LTE_INTERFACE="${MODEM_LTE_INTERFACE:-wwan0}"

_modem_fail() {
  echo "CHECKLIST modem: ERROR --- $*" >&2
  exit 2
}

_check_char_tty() {
  local path="$1" role="$2"
  if [[ ! -e "${path}" ]]; then
    _modem_fail "${role}: '${path}' missing in container. Check docker-compose.yml (devices:), ls -l ${path} on host, USB cable and Quectel module; without this ModemManager/mmcli cannot reach SMS over AT."
  fi
  if [[ ! -c "${path}" ]]; then
    _modem_fail "${role}: '${path}' exists but is not a character device (expected USB serial TTY)."
  fi
  if [[ ! -r "${path}" ]]; then
    _modem_fail "${role}: '${path}' not readable (cgroup/device policies or missing device passthrough)."
  fi
  echo "CHECKLIST modem: OK  [${role}] ${path}"
}

modem_preflight() {
  if truthy "${SKIP_MODEM_HARDWARE_CHECK:-}"; then
    echo 'CHECKLIST modem: skipped (SKIP_MODEM_HARDWARE_CHECK).'
    return 0
  fi

  echo 'CHECKLIST modem: start ---'
  _check_char_tty "${MODEM_TTY_PRIMARY}" "AT_SMS_ttyUSB(primary)"
  if [[ -n "${MODEM_TTY_SECONDARY:-}" ]]; then
    _check_char_tty "${MODEM_TTY_SECONDARY}" "AT_backup_ttyUSB(secondary)"
  fi

  local net_sysfs="/sys/class/net/${MODEM_LTE_INTERFACE}"
  if truthy "${MODEM_STRICT_WWAN_CHECK:-false}"; then
    if [[ ! -d "${net_sysfs}" ]]; then
      _modem_fail "LTE netdev '${MODEM_LTE_INTERFACE}' missing here. On Docker bridge, '${MODEM_LTE_INTERFACE}' is often only on the host. To enforce this check inside the container: network_mode host in Compose (with MODEM_STRICT_WWAN_CHECK=true), or set MODEM_STRICT_WWAN_CHECK=false."
    fi
    echo "CHECKLIST modem: OK  [LTE_${MODEM_LTE_INTERFACE}] sysfs present"
  else
    if [[ -d "${net_sysfs}" ]]; then
      echo "CHECKLIST modem: OK  [LTE_${MODEM_LTE_INTERFACE}] visible inside container"
    else
      echo "CHECKLIST modem: INFO [LTE_${MODEM_LTE_INTERFACE}] not visible inside container (normal on Docker bridge; LTE uses the host netdev). Check ip link on the host; for fatal check use MODEM_STRICT_WWAN_CHECK=true + network_mode: host."
    fi
  fi
  echo 'CHECKLIST modem: end ---'
}

modem_preflight

# network_mode: host — host ModemManager must not hold the same USB modem as in-container MM.
if command -v systemctl >/dev/null 2>&1; then
  if systemctl is-active --quiet ModemManager 2>/dev/null; then
    echo 'WARNING: host ModemManager.service is active — stop it or the container modem stays locked/unusable:' >&2
    echo '  sudo systemctl stop ModemManager NetworkManager' >&2
  fi
fi

export DBUS_SYSTEM_BUS_ADDRESS="${DBUS_SYSTEM_BUS_ADDRESS:-unix:path=/run/dbus/system_bus_socket}"

mkdir -p /run/dbus /run/modem-manager
rm -f /run/dbus/pid || true

if ! command -v dbus-daemon >/dev/null 2>&1; then
  echo "dbus-daemon not found" >&2
  exit 1
fi

dbus-daemon --system --fork
if command -v dbus-send >/dev/null 2>&1; then
  for _i in $(seq 1 50); do
    if dbus-send --system --print-reply --dest=org.freedesktop.DBus /org/freedesktop/DBus org.freedesktop.DBus.ListNames >/dev/null 2>&1; then
      echo "D-Bus ready (${_i}/50)"
      break
    fi
    sleep 0.2
  done
else
  echo "dbus-send not found; falling back to short sleep before udev/MM." >&2
  sleep 1
fi

start_udev_daemon() {
  mkdir -p /run/udev/rules.d /etc/udev/rules.d /dev/.udev 2>/dev/null || true
  if [[ -x /lib/systemd/systemd-udevd ]]; then
    echo "udev: /lib/systemd/systemd-udevd --daemon"
    /lib/systemd/systemd-udevd --daemon 2>/dev/null || echo "udev: warning systemd-udevd failed (continuing)." >&2
  elif command -v udevd >/dev/null 2>&1; then
    echo "udev: udevd --daemon"
    udevd --daemon 2>/dev/null || echo "udev: warning udevd failed (continuing)." >&2
  else
    echo "udev: no systemd-udevd/udevd found (continuing without automatic uevents)." >&2
  fi
  sleep 1
  udevadm trigger --type=subsystems --action=add 2>/dev/null || true
  udevadm trigger --type=devices --action=add 2>/dev/null || true
  udevadm settle --timeout=5 2>/dev/null || true
}

start_udev_daemon

if truthy "${MODEM_MANAGER_DEBUG:-}"; then
  echo "ModemManager debug mode (MODEM_MANAGER_DEBUG)."
  ModemManager --debug >/tmp/ModemManager.log 2>&1 &
else
  ModemManager >/tmp/ModemManager.log 2>&1 &
fi
MM_PID="$!"
trap 'kill "${MM_PID}" 2>/dev/null || true' EXIT

for _i in $(seq 1 60); do
  if mmcli -L >/dev/null 2>&1; then
    echo "ModemManager reachable via mmcli (${_i}/60)"
    break
  fi
  sleep 0.2
done

MODEM_MM_WARMUP_SEC="${MODEM_MM_WARMUP_SEC:-5}"
if [[ "${MODEM_MM_WARMUP_SEC}" -gt 0 ]]; then
  echo "ModemManager warmup ${MODEM_MM_WARMUP_SEC}s before enumeration ..."
  sleep "${MODEM_MM_WARMUP_SEC}"
fi

echo "mmcli -S (rescan ModemManager) ..."
MMCLI_SCAN_TRIES="${MMCLI_SCAN_TRIES:-1}"
MMCLI_SCAN_SLEEP_SEC="${MMCLI_SCAN_SLEEP_SEC:-1}"
for _try in $(seq 1 "${MMCLI_SCAN_TRIES}"); do
  if mmcli -S >/dev/null 2>&1; then
    echo "mmcli -S OK (attempt ${_try}/${MMCLI_SCAN_TRIES})"
    break
  fi
  echo "mmcli -S failed (attempt ${_try}/${MMCLI_SCAN_TRIES})" >&2
  if [[ "${_try}" -lt "${MMCLI_SCAN_TRIES}" ]]; then
    sleep "${MMCLI_SCAN_SLEEP_SEC}"
  fi
done

MODEM_ENUMERATION_WAIT_SEC="${MODEM_ENUMERATION_WAIT_SEC:-30}"
echo "Waiting for modem (mmcli -L, timeout ${MODEM_ENUMERATION_WAIT_SEC}s) ..."
FOUND="0"
for _ in $(seq 1 "${MODEM_ENUMERATION_WAIT_SEC}"); do
  if mmcli -L 2>/dev/null | grep -q '/org/freedesktop/ModemManager1/Modem/'; then
    FOUND="1"
    break
  fi
  sleep 1
done

if [[ "${FOUND}" != "1" ]]; then
  echo "Warning: no modem listed after timeout (${MODEM_ENUMERATION_WAIT_SEC}s)." >&2
  echo "Host tips (modem must be free):" >&2
  echo "  - systemctl stop ModemManager NetworkManager ; mmcli -L (should error / no ModemManager on host)." >&2
  echo "  - lsof or fuser -v ${MODEM_TTY_PRIMARY} to see which process holds the TTY." >&2
  echo "  - EC25 QMI: on host run \`ls -l /dev/cdc-wdm*\`; set MODEM_QMI_DEVICE and docker compose up (cdc-wdm mapped)." >&2
  echo "  - ModemManager log shows \"Failed to find a net port in the QMI modem\"? QMI ModemManager needs wwan*" >&2
  echo "      visible in the same network namespace: ensure docker-compose.yml keeps network_mode: host (defaults in-repo)." >&2
  echo "  - USB/udev cues: sysfs is mounted read-only via docker-compose.yml (/sys/bus/usb:ro); remove that volume locally if forbidden." >&2
  echo "  - Host ModemManager + app in Docker (D-Bus) is an alternative if you avoid network_mode host." >&2
  echo "--- mmcli -L ---"
  mmcli -L 2>&1 || true
  echo "--- ModemManager.log (tail) ---"
  if [[ -s /tmp/ModemManager.log ]]; then
    tail -n 80 /tmp/ModemManager.log || true
  else
    echo "(log missing or empty)"
  fi
  if grep -Fq 'Failed to find a net port in the QMI modem' /tmp/ModemManager.log 2>/dev/null; then
    echo "*** Diagnostic: QMI modem without net port: MODEM_QMI_DEVICE + network_mode: host (already in docker/docker-compose.yml; see comments there)." >&2
  fi
fi

MODEM_DBUS=""
if [[ "${FOUND}" == "1" ]]; then
  MODEM_DBUS="$(mmcli_primary_modem_dbuss_path)"
  if [[ -n "${MODEM_DBUS:-}" ]]; then
    echo "modem detected: ${MODEM_DBUS} (unlock/enable deferred to wait_modem_ready --all)"
    echo "modem path: ${MODEM_DBUS}"
  fi
fi

ensure_sqlite_file "${SQLITE_DB_PATH:-}"

# Ensure log directory exists before Django boots (paired with SQLite path above).
mkdir -p "${DJANGO_LOG_DIR:-/app/logs}"

APP_ROOT=/app
if [[ -f /app/host/manage.py ]]; then
  APP_ROOT=/app/host
  echo "Using bind-mounted app source: ${APP_ROOT}"
fi
cd "${APP_ROOT}"

python manage.py migrate --noinput

python manage.py ensure_superuser

if truthy "${SMS_CLEANUP_ON_STARTUP:-false}"; then
  python manage.py cleanup_sms_storage
  echo "SMS storage cleanup completed (SMS_CLEANUP_ON_STARTUP=true)."
fi

# Default mmcli index for shells (first enumerated modem, fallback 0).
DETECT_MMCLI_INDEX=""
if [[ "${FOUND}" == "1" && -n "${MODEM_DBUS:-}" ]]; then
  DETECT_MMCLI_INDEX="${MODEM_DBUS##*/Modem/}"
elif [[ "${FOUND}" == "1" ]]; then
  _mp="$(mmcli_primary_modem_dbuss_path)"
  [[ -z "${_mp}" ]] || DETECT_MMCLI_INDEX="${_mp##*/Modem/}"
fi
MODEM_MMCLI_INDEX="${DETECT_MMCLI_INDEX:-${MODEM_MMCLI_INDEX:-0}}"
export MODEM_MMCLI_INDEX
mkdir -p /etc/profile.d
printf 'export MODEM_MMCLI_INDEX=%s\n' "${MODEM_MMCLI_INDEX}" > /etc/profile.d/hiwavetel-modem.sh
echo "Default MODEM_MMCLI_INDEX=${MODEM_MMCLI_INDEX} (see /etc/profile.d/hiwavetel-modem.sh)."

echo 'Syncing detected modems to database ...'
python manage.py sync_modems || echo 'warning: sync_modems failed (API modem registry may be stale).' >&2

echo 'Waiting for all modems SMS/Messaging readiness ...'
if python manage.py wait_modem_ready --all; then
  echo 'All modems SMS/Messaging ready.'
else
  echo "warning: one or more modems not ready; restarting ModemManager once for QMI recovery ..." >&2
  kill "${MM_PID}" 2>/dev/null || true
  wait "${MM_PID}" 2>/dev/null || true
  sleep 3
  if truthy "${MODEM_MANAGER_DEBUG:-}"; then
    ModemManager --debug >/tmp/ModemManager.log 2>&1 &
  else
    ModemManager >/tmp/ModemManager.log 2>&1 &
  fi
  MM_PID="$!"
  MODEM_MM_WARMUP_SEC="${MODEM_MM_WARMUP_SEC:-5}"
  if [[ "${MODEM_MM_WARMUP_SEC}" -gt 0 ]]; then
    sleep "${MODEM_MM_WARMUP_SEC}"
  fi
  mmcli -S >/dev/null 2>&1 || true
  python manage.py sync_modems || true
  if python manage.py wait_modem_ready --all; then
    echo 'All modems SMS/Messaging ready after ModemManager restart.'
  else
    echo 'warning: one or more modems still not ready; SMS watcher will keep retrying.' >&2
  fi
fi

if truthy "${RUN_SMS_WATCHER:-}"; then
  HIWAVETEL_ROLE=watcher HIWAVETEL_QUEUE_ENABLED=true \
    python manage.py run_sms_watcher --all-modems &
  echo 'SMS watchers (--all-modems) started in background.'
fi

HIWAVE_PORT="${HIWAVE_PORT:-8000}"
export HIWAVETEL_ROLE=api
export HIWAVETEL_QUEUE_ENABLED=false
exec gunicorn config.wsgi:application \
  --bind "0.0.0.0:${HIWAVE_PORT}" \
  --workers "${GUNICORN_WORKERS:-2}" \
  --threads "${GUNICORN_THREADS:-2}" \
  --timeout "${GUNICORN_TIMEOUT:-120}"
