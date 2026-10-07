#!/usr/bin/env bash
# Сторож контейнеров и готовности TakSklad, запускается таймером taksklad-container-health.timer
#
# 07.10.2026: smartup-auto-import-worker пять дней был unhealthy, а /ready отвечал 503 с 03.09,
# и никто не узнал: healthcheck Docker никого не оповещает, а правила monitoring/observability
# ведут в local-jsonl://temporary. Скрипт выходит кодом 2, если сервис пропал, нездоров или
# /ready не ok, и юнит пишет находку в общий журнал отказов хоста (/opt/ops/alert_on_failure.sh,
# /var/log/wms-alerts). В Telegram она не уходит: 13.09.2026 владелец попросил складывать отказы
# сторожей на сервере и разбирать пачкой (/opt/ops/wms/alerts-digest.sh)
# Код 1 значит, что сторож сам не смог выполниться
set -euo pipefail

PROJECT="${TAKSKLAD_COMPOSE_PROJECT:-vds}"
SERVICES="${TAKSKLAD_EXPECTED_SERVICES:-backend-api frontend postgres skladbot-worker smartup-auto-import-worker telegram-worker}"
READY_SERVICE="${TAKSKLAD_READY_SERVICE:-backend-api}"
READY_URL="${TAKSKLAD_READY_URL:-http://127.0.0.1:8000/ready}"

# Выполняется внутри контейнера backend-api: на хосте может не быть curl
READY_PY="$(cat <<'PY'
import json, sys, urllib.error, urllib.request
try:
    response = urllib.request.urlopen(sys.argv[1], timeout=10)
    code, body = response.status, response.read()
except urllib.error.HTTPError as error:
    code, body = error.code, error.read()
data = json.loads(body or b"{}")
queue = data.get("queue") or {}
print("READY %s http=%s blocking=%s stale=%s" % (
    data.get("status", "unknown"), code,
    queue.get("hot_path_blocking_count"), queue.get("hot_path_stale_processing_count"),
))
PY
)"

findings=0
ready_container=""

for service in $SERVICES; do
  name="$(docker ps --filter "label=com.docker.compose.project=${PROJECT}" \
    --filter "label=com.docker.compose.service=${service}" --format '{{.Names}}' | awk 'NR == 1')"
  if [ -z "$name" ]; then
    echo "FINDING service_missing=${service}"
    findings=1
    continue
  fi
  health="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$name")"
  if [ "$health" = "unhealthy" ]; then
    echo "FINDING unhealthy_service=${service}"
    echo "--- последняя проверка здоровья ${name}:"
    docker inspect --format '{{range .State.Health.Log}}{{.ExitCode}} {{.Output}}{{end}}' "$name" | tail -c 300
    echo
    findings=1
  elif [ "$service" = "$READY_SERVICE" ]; then
    ready_container="$name"
  fi
done

if [ -n "$ready_container" ]; then
  ready="$(docker exec "$ready_container" python -c "$READY_PY" "$READY_URL" 2>&1 | tail -n 1 || true)"
  case "$ready" in
    "READY ok "*) ;;
    *)
      echo "FINDING ready_not_ok ${ready:-no_output}"
      findings=1
      ;;
  esac
fi

if [ "$findings" -ne 0 ]; then
  exit 2
fi
echo "CONTAINER_HEALTH_OK project=${PROJECT} services=$(printf '%s\n' $SERVICES | wc -l | tr -d ' ')"
