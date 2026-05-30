# Deploy And Rollback Runbook

Дата фиксации: 2026-05-30.

Документ описывает минимальную эксплуатационную процедуру TakSklad 2.0 на VDS. Секреты, пароли, токены и backup-файлы в Git не хранятся.

## 1. DNS И HTTPS

Целевой домен backend:

```text
api.taksklad.uz -> 135.181.245.84
```

На стороне DNS-регистратора нужна A-запись:

```text
type: A
name: api
value: 135.181.245.84
ttl: 300
```

Текущий статус на 2026-05-30:

- в PowerVPS есть только VDS, DNS-зона `taksklad.uz` там не управляется;
- WHOIS `.uz` отвечает, что `taksklad.uz` не найден в базе;
- значит, сначала нужно зарегистрировать домен у `.uz`-регистратора, а уже потом добавить A-запись.

После обновления DNS на VDS в `deploy/vds/.env` должно быть:

```text
TAKSKLAD_BACKEND_HOST=api.taksklad.uz
```

Для переключения после готового DNS на VDS:

```bash
cd /opt/taksklad/app
./deploy/vds/switch_backend_host.sh api.taksklad.uz
```

Если нужно открыть Adminer через отдельный домен:

```bash
cd /opt/taksklad/app
./deploy/vds/switch_backend_host.sh api.taksklad.uz adminer.taksklad.uz
```

Проверка:

```bash
dig +short api.taksklad.uz
curl -fsS https://api.taksklad.uz/health
```

Временный staging URL до переключения DNS:

```text
https://api.135.181.245.84.sslip.io/health
```

## 2. Deploy Backend

Локально:

```bash
rsync -az backend root@135.181.245.84:/opt/taksklad/app/
rsync -az --exclude '.env' deploy/vds/ root@135.181.245.84:/opt/taksklad/app/deploy/vds/
```

На VDS:

```bash
cd /opt/taksklad/app
./deploy/vds/apply_schema.sh
docker compose --env-file deploy/vds/.env -f deploy/vds/docker-compose.yml up -d --build backend-api
curl -fsS https://api.135.181.245.84.sslip.io/health
```

Если DNS уже переключен:

```bash
curl -fsS https://api.taksklad.uz/health
```

## 3. Backup

Ручной backup:

```bash
cd /opt/taksklad/app
./deploy/vds/backup_postgres.sh
```

Проверка расписания:

```bash
systemctl list-timers 'taksklad-postgres-backup.timer'
systemctl status taksklad-postgres-backup.timer
```

## 4. Restore Drill

Restore-drill всегда выполняется в отдельную временную БД и не трогает production database.

```bash
cd /opt/taksklad/app
latest="$(ls -t /opt/taksklad/backups/postgres/taksklad-postgres-*.sql.gz | head -1)"
./deploy/vds/restore_drill.sh "$latest"
```

Успешный результат должен показать таблицы `orders`, `order_items`, `scan_codes`, `imports` и строку `restore_drill_ok`.

## 5. Production Restore

Production restore перезаписывает текущую БД. Запускать только при осознанном откате данных.

```bash
cd /opt/taksklad/app
CONFIRM_RESTORE=YES ./deploy/vds/restore_postgres.sh /opt/taksklad/backups/postgres/taksklad-postgres-YYYYmmddTHHMMSSZ.sql.gz
```

После restore:

```bash
curl -fsS https://api.135.181.245.84.sslip.io/health
```

## 6. Rollback Backend Code

Rollback к предыдущему Git-коммиту:

```bash
cd /opt/taksklad/app
git fetch --all
git checkout <previous-good-commit>
docker compose --env-file deploy/vds/.env -f deploy/vds/docker-compose.yml up -d --build backend-api
```

Если код на VDS доставлялся через `rsync`, rollback делается повторным `rsync` из локального checkout предыдущего хорошего коммита.

## 7. Release Safety

До ручной приёмки нельзя:

- менять `version.json`;
- отправлять desktop push-update;
- собирать и выкладывать Windows archive как обязательное обновление;
- удалять desktop fallback на Google/local режим.

Для включения backend в desktop используются feature flags, а не принудительный переход.

## 8. Acceptance Cleanup

Тестовые acceptance-данные удалять только по явному маркеру.

Dry-run:

```bash
cd /opt/taksklad/app
./deploy/vds/cleanup_acceptance_marker.sh "ACCEPTANCE TELEGRAM 20260531"
```

Удаление:

```bash
cd /opt/taksklad/app
./deploy/vds/cleanup_acceptance_marker.sh "ACCEPTANCE TELEGRAM 20260531" --apply
```

Защита скрипта: marker должен содержать `ACCEPTANCE`, `WEB_UI_SMOKE` или `SMOKE_MVP`.
