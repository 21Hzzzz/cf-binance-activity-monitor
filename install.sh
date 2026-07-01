#!/usr/bin/env bash
set -euo pipefail

APP_NAME="cf-binance-activity-monitor"
INSTALL_DIR="/opt/${APP_NAME}"
CONFIG_FILE="/etc/${APP_NAME}.env"
STATE_DIR="/var/lib/${APP_NAME}"
SERVICE_FILE="/etc/systemd/system/${APP_NAME}.service"
TIMER_FILE="/etc/systemd/system/${APP_NAME}.timer"
RAW_BASE_URL="${RAW_BASE_URL:-https://raw.githubusercontent.com/21Hzzzz/cf-binance-activity-monitor/main}"

if [ "$(id -u)" -ne 0 ]; then
  echo "Please run as root, for example: sudo bash install.sh" >&2
  exit 1
fi

need_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required command: $1" >&2
    exit 1
  fi
}

need_command python3
need_command systemctl
need_command curl

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd || pwd)"

read_existing_config() {
  local key="$1"
  if [ -f "$CONFIG_FILE" ]; then
    sed -n "s/^${key}=//p" "$CONFIG_FILE" | tail -n 1
  fi
}

prompt_value() {
  local key="$1"
  local prompt="$2"
  local required="${3:-true}"
  local secret="${4:-false}"
  local value="${!key:-}"

  if [ -z "$value" ]; then
    value="$(read_existing_config "$key" || true)"
  fi

  if [ -z "$value" ] && [ -r /dev/tty ]; then
    while true; do
      if [ "$secret" = "true" ]; then
        read -r -s -p "$prompt: " value </dev/tty
        echo >/dev/tty
      else
        read -r -p "$prompt: " value </dev/tty
      fi
      if [ -n "$value" ] || [ "$required" != "true" ]; then
        break
      fi
      echo "$key is required." >/dev/tty
    done
  fi

  if [ -z "$value" ] && [ "$required" = "true" ]; then
    echo "Missing required value: $key" >&2
    echo "You can pass it non-interactively, for example:" >&2
    echo "sudo env ${key}=... bash install.sh" >&2
    exit 1
  fi

  case "$value" in
    *$'\n'*|*$'\r'*)
      echo "$key must not contain newlines." >&2
      exit 1
      ;;
  esac

  printf '%s' "$value"
}

install_file() {
  local name="$1"
  local mode="$2"
  local target="${INSTALL_DIR}/${name}"

  if [ -f "${script_dir}/${name}" ]; then
    install -m "$mode" "${script_dir}/${name}" "$target"
  else
    curl -fsSL "${RAW_BASE_URL}/${name}" -o "$target"
    chmod "$mode" "$target"
  fi
}

telegram_bot_token="$(prompt_value TELEGRAM_BOT_TOKEN "Telegram bot token" true true)"
telegram_chat_id="$(prompt_value TELEGRAM_CHAT_ID "Telegram chat id" true false)"
telegram_thread_id="$(prompt_value TELEGRAM_MESSAGE_THREAD_ID "Telegram message thread id (optional)" false false)"

install -d -m 0755 "$INSTALL_DIR"
install -d -m 0755 "$STATE_DIR"

install_file "monitor.py" 0755
install_file "uninstall.sh" 0755

umask 077
{
  echo "# ${APP_NAME} runtime config"
  echo "TELEGRAM_BOT_TOKEN=${telegram_bot_token}"
  echo "TELEGRAM_CHAT_ID=${telegram_chat_id}"
  if [ -n "$telegram_thread_id" ]; then
    echo "TELEGRAM_MESSAGE_THREAD_ID=${telegram_thread_id}"
  fi
  echo "STATE_PATH=${STATE_DIR}/state.json"
  echo "BINANCE_LANG=zh-CN"
  echo "ALERT_ON_FIRST_RUN=false"
  echo "RESOURCE_BATCH_SIZE=100"
  echo "RESOURCE_BATCHES_PER_RUN=35"
  echo "RESOURCE_PROBE_BATCHES_PER_RUN=10"
  echo "RESOURCE_SCAN_START_ID=100003800"
  echo "RESOURCE_BACKTRACK=800"
  echo "RESOURCE_RECENT_AHEAD=1700"
  echo "ERROR_ALERT_COOLDOWN_SECONDS=3600"
} >"$CONFIG_FILE"
chmod 600 "$CONFIG_FILE"

cat >"$SERVICE_FILE" <<EOF
[Unit]
Description=Binance Activity Monitor
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
EnvironmentFile=${CONFIG_FILE}
StateDirectory=${APP_NAME}
ExecStart=/usr/bin/python3 ${INSTALL_DIR}/monitor.py run
EOF

cat >"$TIMER_FILE" <<EOF
[Unit]
Description=Run Binance Activity Monitor every 15 minutes

[Timer]
OnBootSec=2min
OnCalendar=*:0/15
AccuracySec=1min
RandomizedDelaySec=30s
Persistent=true
Unit=${APP_NAME}.service

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now "${APP_NAME}.timer"

echo "Running one baseline check now..."
if systemctl start "${APP_NAME}.service"; then
  echo "Installed ${APP_NAME}."
  echo "Config: ${CONFIG_FILE}"
  echo "State: ${STATE_DIR}/state.json"
  echo "Timer: systemctl status ${APP_NAME}.timer"
  echo "Logs: journalctl -u ${APP_NAME}.service -n 100 --no-pager"
else
  echo "Install finished, but the first run failed. Check logs:" >&2
  echo "journalctl -u ${APP_NAME}.service -n 100 --no-pager" >&2
  exit 1
fi
