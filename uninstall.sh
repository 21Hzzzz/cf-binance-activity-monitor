#!/usr/bin/env bash
set -euo pipefail

APP_NAME="cf-binance-activity-monitor"
INSTALL_DIR="/opt/${APP_NAME}"
CONFIG_FILE="/etc/${APP_NAME}.env"
STATE_DIR="/var/lib/${APP_NAME}"
SERVICE_FILE="/etc/systemd/system/${APP_NAME}.service"
TIMER_FILE="/etc/systemd/system/${APP_NAME}.timer"
PURGE="false"

if [ "${1:-}" = "--purge" ]; then
  PURGE="true"
fi

if [ "$(id -u)" -ne 0 ]; then
  echo "Please run as root, for example: sudo ${INSTALL_DIR}/uninstall.sh" >&2
  exit 1
fi

systemctl disable --now "${APP_NAME}.timer" >/dev/null 2>&1 || true
systemctl stop "${APP_NAME}.service" >/dev/null 2>&1 || true

rm -f "$SERVICE_FILE" "$TIMER_FILE"
systemctl daemon-reload
systemctl reset-failed "${APP_NAME}.service" "${APP_NAME}.timer" >/dev/null 2>&1 || true

rm -rf "$INSTALL_DIR"

if [ "$PURGE" = "true" ]; then
  rm -f "$CONFIG_FILE"
  rm -rf "$STATE_DIR"
  echo "Uninstalled ${APP_NAME} and removed config/state."
else
  echo "Uninstalled ${APP_NAME}."
  echo "Kept config: ${CONFIG_FILE}"
  echo "Kept state: ${STATE_DIR}"
  echo "Run with --purge to remove config and state too."
fi
