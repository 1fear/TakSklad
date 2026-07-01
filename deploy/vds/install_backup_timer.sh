#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${TAKSKLAD_APP_DIR:-/opt/stacks/taksklad/app}"
SYSTEMD_DIR="${TAKSKLAD_SYSTEMD_DIR:-/etc/systemd/system}"
SERVICE_NAME="taksklad-postgres-backup"

if [ "$(id -u)" -ne 0 ]; then
  echo "Run as root to install systemd timer." >&2
  exit 1
fi

install -m 0644 "$APP_DIR/deploy/vds/systemd/$SERVICE_NAME.service" "$SYSTEMD_DIR/$SERVICE_NAME.service"
install -m 0644 "$APP_DIR/deploy/vds/systemd/$SERVICE_NAME.timer" "$SYSTEMD_DIR/$SERVICE_NAME.timer"

systemctl daemon-reload
systemctl enable --now "$SERVICE_NAME.timer"
systemctl list-timers "$SERVICE_NAME.timer" --no-pager
