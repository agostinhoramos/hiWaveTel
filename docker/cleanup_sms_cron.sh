#!/usr/bin/env bash
# Run SMS storage rotation inside the hiwavetel Docker container.
#
# Usage (from repo root):
#   ./docker/cleanup_sms_cron.sh
#   ./docker/cleanup_sms_cron.sh --dry-run
#
# Cron example (hourly, adjust paths):
#   0 * * * * /home/webmaster/hiWaveTel/docker/cleanup_sms_cron.sh >> /var/log/hiwavetel-sms-cleanup.log 2>&1
#
# Prefer systemd timer when available — see docker/hiwavetel-cleanup.timer and
# docker/hiwavetel-cleanup.service.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_FILE="${SCRIPT_DIR}/docker-compose.yml"

if [[ ! -f "${COMPOSE_FILE}" ]]; then
  echo "docker-compose.yml not found at ${COMPOSE_FILE}" >&2
  exit 1
fi

EXTRA_ARGS=("$@")
docker compose -f "${COMPOSE_FILE}" exec -T hiwavetel python manage.py cleanup_sms_storage "${EXTRA_ARGS[@]}"
