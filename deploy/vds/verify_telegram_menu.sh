#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${TAKSKLAD_ENV_FILE:-$SCRIPT_DIR/.env}"
COMPOSE_FILE="$SCRIPT_DIR/docker-compose.yml"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Env file not found: $ENV_FILE" >&2
  exit 1
fi

docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" exec -T telegram-worker python - <<'PY'
import json
import os
import sys
import urllib.request


expected_commands = [
    {"command": "menu", "description": "Меню TakSklad"},
    {"command": "buttons", "description": "Призвать кнопки"},
    {"command": "logistics", "description": "Отчёт логистики"},
    {"command": "kiz", "description": "Выгрузка КИЗов"},
    {"command": "date", "description": "Дата отгрузки"},
    {"command": "status", "description": "Статус"},
    {"command": "imports", "description": "Последние импорты"},
]

token = os.environ.get("TELEGRAM_BOT_TOKEN")
if not token:
    print(json.dumps({
        "status": "failed",
        "errors": ["TELEGRAM_BOT_TOKEN is not configured"],
        "commands": [],
        "menu_button": {},
    }, ensure_ascii=False))
    sys.exit(1)


def telegram_api(method):
    url = f"https://api.telegram.org/bot{token}/{method}"
    request = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(request, timeout=15) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not payload.get("ok"):
        raise RuntimeError(payload)
    return payload.get("result")


errors = []
try:
    commands = telegram_api("getMyCommands")
    menu_button = telegram_api("getChatMenuButton")
except Exception as exc:
    print(json.dumps({
        "status": "failed",
        "errors": [str(exc)],
        "commands": [],
        "menu_button": {},
    }, ensure_ascii=False))
    sys.exit(1)

if commands != expected_commands:
    errors.append("Telegram commands do not match expected TakSklad menu")
if not isinstance(menu_button, dict) or menu_button.get("type") != "commands":
    errors.append("Telegram chat menu button must be type=commands")

print(json.dumps({
    "status": "failed" if errors else "ok",
    "errors": errors,
    "commands": commands,
    "expected_commands": expected_commands,
    "menu_button": menu_button,
}, ensure_ascii=False, sort_keys=True))
sys.exit(1 if errors else 0)
PY
