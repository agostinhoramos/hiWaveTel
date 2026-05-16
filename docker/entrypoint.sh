#!/usr/bin/env bash
# dbus system + ModemManager + optional SIM unlock + Gunicorn.
set -euo pipefail

truthy() {
  case "$(echo "${1:-}" | tr '[:upper:]' '[:lower:]')" in
    1 | true | yes | on) return 0 ;;
    *) return 1 ;;
  esac
}

# True when SIM reports PIN lock per mmcli (skip unnecessary --pin when already usable).
_sim_is_locked() {
  local sim_path="$1"
  [[ -n "${sim_path}" ]] || return 1
  mmcli -i "${sim_path}" 2>/dev/null | grep -qiE \
    'SIM lock status:[[:space:]]*sim-pin|unlock required:[[:space:]]*sim-pin|lock:[[:space:]]*sim-pin'
}

# Prefer ModemManager /modem and SIM reported by mmcli (avoids always assuming .../SIM/0).
mmcli_primary_modem_dbuss_path() {
  mmcli -L 2>/dev/null | grep -oE '/org/freedesktop/ModemManager1/Modem/[[:digit:]]+' | head -n1
}

mmcli_primary_sim_dbuss_path() {
  local modem_path="$1"
  [[ -n "${modem_path}" ]] || return 1
  mmcli -m "${modem_path}" 2>/dev/null | grep -oE '/org/freedesktop/ModemManager1/SIM/[[:digit:]]+' | head -n1
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

echo "mmcli -S (rescan ModemManager) ..."
MMCLI_SCAN_TRIES="${MMCLI_SCAN_TRIES:-1}"
MMCLI_SCAN_SLEEP_SEC="${MMCLI_SCAN_SLEEP_SEC:-1}"
for _try in $(seq 1 "${MMCLI_SCAN_TRIES}"); do
  if mmcli -S 2>/dev/null; then
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

PIN="${DEVICE_PIN_CODE:-}"
PIN="${PIN#"${PIN%%[![:space:]]*}"}"
PIN="${PIN%"${PIN##*[![:space:]]}"}"
MODEM_DBUS=""
SIM_DETECT=""
if [[ "${FOUND}" == "1" ]]; then
  MODEM_DBUS="$(mmcli_primary_modem_dbuss_path)"

  # Enable modem if in disabled state, then wait for enabled/registered so that
  # mmcli --messaging-create-sms doesn't fail with "modem not enabled yet".
  if [[ -n "${MODEM_DBUS}" ]]; then
    MODEM_ENABLE_WAIT_SEC="${MODEM_ENABLE_WAIT_SEC:-20}"
    _modem_state_check() { mmcli -m "${MODEM_DBUS}" 2>/dev/null | grep -oE 'state:[[:space:]]*[a-z]+' | head -n1 | grep -oE '[a-z]+$' || echo 'unknown'; }

    _initial_state="$(_modem_state_check)"
    echo "modem ${MODEM_DBUS} initial state: ${_initial_state}"

    if [[ "${_initial_state}" == "disabled" ]]; then
      echo "modem ${MODEM_DBUS} is disabled --- enabling (mmcli --enable) ..."
      mmcli -m "${MODEM_DBUS}" --enable 2>&1 || echo "warning: mmcli --enable failed (continuing)." >&2
    fi

    echo "Waiting for ${MODEM_DBUS} to become enabled or registered (timeout ${MODEM_ENABLE_WAIT_SEC}s) ..."
    for _ in $(seq 1 "${MODEM_ENABLE_WAIT_SEC}"); do
      _cur="$(_modem_state_check)"
      if [[ "${_cur}" == "enabled" || "${_cur}" == "registered" ]]; then
        echo "modem ${MODEM_DBUS} state=${_cur} --- ready for SMS."
        break
      fi
      sleep 1
    done
  fi

  [[ -z "${MODEM_DBUS:-}" ]] || SIM_DETECT="$(mmcli_primary_sim_dbuss_path "${MODEM_DBUS}")" || true
fi

SIM_EXPLICIT="${SIM_PATH:-}"
SIM_EXPLICIT="${SIM_EXPLICIT#"${SIM_EXPLICIT%%[![:space:]]*}"}"
SIM_EXPLICIT="${SIM_EXPLICIT%"${SIM_EXPLICIT##*[![:space:]]}"}"

SIM=""
# Prefer SIM path from mmcli (hard-coded SIM_PATH .../SIM/0 is often wrong).
if truthy "${SIM_PATH_OVERRIDES_MMCLI:-false}" && [[ -n "${SIM_EXPLICIT}" ]]; then
  SIM="${SIM_EXPLICIT}"
elif [[ -n "${SIM_DETECT}" ]]; then
  SIM="${SIM_DETECT}"
elif [[ -n "${SIM_EXPLICIT}" ]]; then
  SIM="${SIM_EXPLICIT}"
fi

if [[ "${FOUND}" == "1" && -n "${PIN}" && -n "${SIM}" ]]; then
  if [[ "${SIM}" == "${SIM_DETECT}" ]] && [[ -n "${SIM_DETECT}" ]]; then
    _sim_src="mmcli"
  elif truthy "${SIM_PATH_OVERRIDES_MMCLI:-false}" && [[ "${SIM}" == "${SIM_EXPLICIT}" ]]; then
    _sim_src="SIM_PATH+override"
  else
    _sim_src="SIM_PATH"
  fi
  if _sim_is_locked "${SIM}"; then
    echo "SIM unlock (${_sim_src}): modem=${MODEM_DBUS:-?} SIM=${SIM} (locked)"
    mmcli -i "${SIM}" --pin="${PIN}" || echo "PIN: unlock failed."
  else
    echo "SIM unlock (${_sim_src}): skipped (SIM not SIM-PIN locked or already usable)."
  fi
elif [[ "${FOUND}" != "1" ]] && ([[ -n "${PIN}" ]] || [[ -n "${SIM_EXPLICIT}" ]] || [[ -n "${SIM_DETECT}" ]]); then
  echo 'Skipping PIN/SIM unlock: modem not enumerated yet.'
elif [[ "${FOUND}" == "1" && -n "${PIN}" && -z "${SIM}" ]]; then
  echo "PIN set (${DEVICE_PIN_CODE:+present}) but no SIM object reported by mmcli (modem ${MODEM_DBUS:-unknown})." >&2
  echo "  Check SIM slot/card; or set SIM_PATH explicitly." >&2
elif [[ "${FOUND}" == "1" && -z "${PIN}" ]] && [[ -n "${SIM_EXPLICIT}" ]]; then
  echo "SIM_PATH set without DEVICE_PIN_CODE: no unlock attempt (normal if SIM is not PIN-locked)." >&2
fi

if [[ -n "${SQLITE_DB_PATH:-}" ]]; then
  mkdir -p "$(dirname "${SQLITE_DB_PATH}")"
fi

cd /app

python manage.py migrate --noinput

# Modem index for Django / mmcli -m $N when auto-detect matches first enumerated modem path.
DETECT_MMCLI_INDEX=""
if [[ "${FOUND}" == "1" && -n "${MODEM_DBUS:-}" ]]; then
  DETECT_MMCLI_INDEX="${MODEM_DBUS##*/Modem/}"
elif [[ "${FOUND}" == "1" ]]; then
  _mp="$(mmcli_primary_modem_dbuss_path)"
  [[ -z "${_mp}" ]] || DETECT_MMCLI_INDEX="${_mp##*/Modem/}"
fi
if truthy "${AUTO_DETECT_MMCLI_INDEX:-true}" && [[ -n "${DETECT_MMCLI_INDEX}" ]]; then
  export MODEM_MMCLI_INDEX="${DETECT_MMCLI_INDEX}"
  echo "MODEM_MMCLI_INDEX auto-detect: ${MODEM_MMCLI_INDEX} (AUTO_DETECT_MMCLI_INDEX=true)."
elif [[ -n "${DETECT_MMCLI_INDEX}" ]]; then
  echo "MODEM_MMCLI_INDEX manual: ${MODEM_MMCLI_INDEX:-0} (would auto-detect as ${DETECT_MMCLI_INDEX}; set AUTO_DETECT_MMCLI_INDEX=true to align)."
fi

if truthy "${RUN_SMS_WATCHER:-}"; then
  MODEM_MMCLI_INDEX="${MODEM_MMCLI_INDEX:-0}"
  python manage.py run_sms_watcher --modem-index "${MODEM_MMCLI_INDEX}" &
  echo "SMS watcher (modem_index=${MODEM_MMCLI_INDEX}) in background."
fi

HIWAVE_PORT="${HIWAVE_PORT:-8000}"
exec gunicorn config.wsgi:application \
  --bind "0.0.0.0:${HIWAVE_PORT}" \
  --workers "${GUNICORN_WORKERS:-2}" \
  --threads "${GUNICORN_THREADS:-2}" \
  --timeout "${GUNICORN_TIMEOUT:-120}"
