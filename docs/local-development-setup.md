# Локальная Среда Разработки TakSklad

Документ фиксирует настройку ноутбука для разработки desktop-части и VDS/backend-части TakSklad. Секреты, реальные токены, Google credentials и рабочие `.env` не должны попадать в Git.

## Состояние Ноутбука На 30.05.2026

Установлено и проверено:

- Homebrew.
- Git.
- GitHub CLI, авторизация под аккаунтом `1fear`.
- Python `3.12.13` в проектной `.venv`.
- Python-зависимости из `requirements.txt`.
- Python-зависимости backend из `backend/requirements.txt`.
- Docker CLI `29.5.2`.
- Docker Compose plugin `5.1.4`.
- Docker Buildx plugin `0.34.1`.
- Colima `0.10.1` как локальный Docker engine.

Colima запущен как Homebrew service:

```bash
brew services start colima
```

Проверка:

```bash
colima status
docker info
docker compose version
docker buildx version
```

Docker Compose plugin подключен через `~/.docker/config.json`:

```json
{
  "cliPluginsExtraDirs": [
    "/opt/homebrew/lib/docker/cli-plugins"
  ]
}
```

## Python

Проектная среда:

```bash
cd /Users/anton/Documents/work/TakSklad
.venv/bin/python --version
.venv/bin/python -m pip install -r requirements.txt -r backend/requirements.txt
```

Проверки:

```bash
.venv/bin/python -m unittest discover -s tests
.venv/bin/python -m py_compile main.py sitecustomize.py taksklad/__init__.py src/taksklad/*.py tests/*.py backend/app/*.py
```

## Backend / VDS Compose Локально

Для локальной проверки compose нужен рабочий env-файл. Он создаётся из шаблона и игнорируется Git:

```bash
cp deploy/vds/.env.example deploy/vds/.env
chmod 600 deploy/vds/.env
```

Проверка конфигурации:

```bash
docker compose --env-file deploy/vds/.env -f deploy/vds/docker-compose.yml config
```

Для локального smoke нужно создать внешнюю сеть Traefik, потому что compose ожидает её как уже существующую:

```bash
docker network inspect traefik >/dev/null 2>&1 || docker network create traefik
docker compose --env-file deploy/vds/.env -f deploy/vds/docker-compose.yml up -d --build postgres backend-api
```

Проверка backend внутри контейнера:

```bash
docker compose --env-file deploy/vds/.env -f deploy/vds/docker-compose.yml exec -T backend-api python - <<'PY'
from urllib.request import urlopen
print(urlopen("http://127.0.0.1:8000/health", timeout=5).read().decode())
PY
```

Проверка таблиц Postgres:

```bash
docker compose --env-file deploy/vds/.env -f deploy/vds/docker-compose.yml exec -T postgres psql -U taksklad -d taksklad -c "\\dt"
```

Остановка тестового стека с удалением placeholder-тома:

```bash
docker compose --env-file deploy/vds/.env -f deploy/vds/docker-compose.yml down -v
```

## Что Не Хранить В Git

- `deploy/vds/.env`
- `credentials.json`
- `TakSklad_data.json`
- реальные Telegram токены;
- реальные Google private keys;
- реальные VPS-пароли и ключи.
