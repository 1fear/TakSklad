#!/usr/bin/env bash
# Ставит сторож контейнеров TakSklad (deploy/vds/container_health_check.sh) на таймер
# Откат: systemctl disable --now taksklad-container-health.timer, затем убрать два юнита
# из каталога systemd и сделать daemon-reload
set -euo pipefail

APP_DIR="${TAKSKLAD_APP_DIR:-/opt/stacks/taksklad/app}"
SYSTEMD_DIR="${TAKSKLAD_SYSTEMD_DIR:-/etc/systemd/system}"
NAME="taksklad-container-health"

if [ "$(id -u)" -ne 0 ]; then
  echo "Run as root to install systemd timer." >&2
  exit 1
fi

# Обработчик отказов живёт на хосте вне репозитория, без него находки некуда записать
for required in /opt/ops/alert_on_failure.sh "$SYSTEMD_DIR/wms-alert@.service"; do
  if [ ! -e "$required" ]; then
    echo "Missing host alert handler: $required" >&2
    exit 1
  fi
done

chmod 0755 "$APP_DIR/deploy/vds/container_health_check.sh"
install -m 0644 "$APP_DIR/deploy/vds/systemd/$NAME.service" "$SYSTEMD_DIR/$NAME.service"
install -m 0644 "$APP_DIR/deploy/vds/systemd/$NAME.timer" "$SYSTEMD_DIR/$NAME.timer"

systemctl daemon-reload
systemctl enable --now "$NAME.timer"
systemctl list-timers "$NAME.timer" --no-pager
