#!/usr/bin/env bash
# Validate mmcli / ModemManager on the HOST (outside Docker).
set -euo pipefail

log() { echo "[host-mmcli] $*"; }
fail() { echo "[host-mmcli] FAIL: $*" >&2; exit 1; }

command -v mmcli >/dev/null 2>&1 || fail "mmcli not found; install ModemManager/modemmanager package."

log "mmcli version: $(mmcli --version | head -n1)"

FIRST_MODEM=""
if LIST_OUT="$(mmcli -L 2>/dev/null)"; then
  FIRST_MODEM="$(echo "${LIST_OUT}" | grep -oE '/org/freedesktop/ModemManager1/Modem/[[:digit:]]+' | head -n1 || true)"
fi
[[ -n "${FIRST_MODEM}" ]] || fail "No modem in mmcli -L."

INDEX="${FIRST_MODEM##*/Modem/}"
log "first modem path=${FIRST_MODEM} index=${INDEX}"

SMS_LIST="$(mmcli -m "${INDEX}" --messaging-list-sms 2>&1)" || fail "mmcli --messaging-list-sms failed."
log "SMS messages (first lines):"
echo "${SMS_LIST}" | head -n5

FIRST_SMS_PATH="$(echo "${SMS_LIST}" | grep -oE '/org/freedesktop/ModemManager1/SMS/[[:digit:]]+' | head -n1 || true)"
if [[ -n "${FIRST_SMS_PATH}" ]]; then
  log "inspect sample SMS ${FIRST_SMS_PATH}"
  mmcli -s "${FIRST_SMS_PATH}" >/dev/null || fail "mmcli -s failed for ${FIRST_SMS_PATH}"
else
  log "INFO: modem lists no SMS (empty list is OK)."
fi

log "OK --- host mmcli checklist complete."
