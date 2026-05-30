# VDS Release Readiness

Документ фиксирует состояние подготовки TakSklad к VDS-релизу. Это не Windows-релиз и не включение автообновлений на рабочих компьютерах.

## Текущий Статус

Готово для staging-проверок:

- VDS на Ubuntu 24.04 подготовлен.
- Docker/Compose установлены.
- Traefik + HTTPS работают.
- Postgres работает во внутренней Docker-сети.
- Backend API доступен через HTTPS.
- Сервисный Bearer-токен защищает `/api/v1/*`.
- Traefik явно закреплен на Docker-сети `traefik` через `traefik.docker.network`, чтобы backend/adminer не проксировались через внутреннюю сеть Postgres.
- Реализованы:
  - `GET /health`;
  - `GET /api/v1/orders/active`;
  - `POST /api/v1/imports`;
  - `GET /api/v1/imports`;
  - `POST /api/v1/scans`;
  - `POST /api/v1/orders/{order_id}/complete`;
  - `GET /api/v1/reports/day`.
- Добавлены backup/restore-скрипты Postgres.
- Добавлен systemd timer для ежедневного Postgres backup.
- SkladBot worker перенесён на VDS, работает по API-ключу из server-side `.env`.
- Telegram worker перенесён на VDS, использует server-side токен и не требует запуска Telegram polling на рабочих ПК.
- VDS staging smoke с импортом, сканами, завершением заказа, backup и cleanup пройден.
- Restore-drill из последнего backup-файла пройден на отдельной временной БД.

Не готово для production:

- Desktop-приложение ещё не подключено к backend.
- Нет Alembic-миграций; текущая схема рассчитана на стартовый deploy.
- DNS `api.taksklad.uz` ещё не настроен: домен `taksklad.uz` нужно сначала зарегистрировать у `.uz`-регистратора.
- Не проведена ручная приемка на реальных заказах склада.

## Backend API

### Активные Заказы

`GET /api/v1/orders/active`

Возвращает заказы, которые не находятся в статусах `completed`, `done`, `closed`, вместе с позициями.

### Импорт Заказов

`POST /api/v1/imports`

Принимает строки текущего desktop/Excel/Google-формата и создает `orders` + `order_items`.

Поддерживаемые поля:

- `Дата отгрузки` или `Дата получения заказа`;
- `Тип оплаты`;
- `Клиент`;
- `Адрес`;
- `Торговый представитель`;
- `Товары`;
- `Кол-во ШТ`;
- `Кол-во блок`;
- `ID заказа`;
- `ID импорта`;
- `Источник файла`;
- `Строка файла`;
- `Номер заявки SkladBot`;
- `ID заявки SkladBot`.

Поведение:

- несколько товаров одного клиента/адреса/даты/оплаты группируются в один заказ;
- повторный импорт той же позиции не создает дубль;
- невалидные строки попадают в `errors`;
- результат пишется в `imports`;
- действие пишется в `audit_log`.

### История Импортов

`GET /api/v1/imports`

Возвращает историю импортов с итогами:

- `rows_total`;
- `rows_imported`;
- `orders_created`;
- `items_created`;
- `duplicate_rows`;
- `invalid_rows`;
- `errors`.

### Дневной Отчёт

`GET /api/v1/reports/day?report_date=YYYY-MM-DD`

Возвращает сводку из Postgres:

- заказы с `order_date` на выбранную дату;
- заказы, по которым были сканы в выбранную дату;
- план/скан/остаток по блокам;
- количество сканов за день;
- группировку по типу оплаты;
- номера заявок SkladBot, если они пришли при импорте.

### Скан КИЗ

`POST /api/v1/scans`

Создает запись в `scan_codes`, увеличивает `scanned_blocks`, защищает от дублей и пишет аудит.

### Завершение Заказа

`POST /api/v1/orders/{order_id}/complete`

Закрывает заказ только если обязательные позиции досканированы. При раннем закрытии возвращает `409` со списком недосканированных позиций.

## Backup И Restore

### Ручной Backup На VDS

Из `/opt/taksklad/app`:

```bash
./deploy/vds/backup_postgres.sh
```

По умолчанию backup сохраняется в:

```text
/opt/taksklad/backups/postgres
```

Retention по умолчанию: `14` дней.

Переопределение:

```bash
TAKSKLAD_BACKUP_DIR=/secure/backups TAKSKLAD_BACKUP_RETENTION_DAYS=30 ./deploy/vds/backup_postgres.sh
```

### Ручной Restore На VDS

Restore намеренно требует подтверждение:

```bash
CONFIRM_RESTORE=YES ./deploy/vds/restore_postgres.sh /opt/taksklad/backups/postgres/taksklad-postgres-YYYYmmddTHHMMSSZ.sql.gz
```

Важно: restore очищает схему `public` и восстанавливает данные из backup-файла.

### Автоматический Backup На VDS

Установка timer:

```bash
cd /opt/taksklad/app
./deploy/vds/install_backup_timer.sh
```

Проверка:

```bash
systemctl list-timers taksklad-postgres-backup.timer --no-pager
systemctl status taksklad-postgres-backup.service --no-pager
```

По умолчанию backup запускается каждый день в `03:20` и хранит файлы `14` дней.

## Проверки Перед Релизной Приемкой

Локально:

```bash
.venv/bin/python -m unittest discover -s tests
.venv/bin/python -m py_compile main.py sitecustomize.py taksklad/__init__.py src/taksklad/*.py tests/*.py backend/app/*.py
docker compose --env-file deploy/vds/.env -f deploy/vds/docker-compose.yml config
docker compose --env-file deploy/traefik/.env.example -f deploy/traefik/docker-compose.yml config
```

Локальный Docker smoke:

1. Поднять `postgres + backend-api`.
2. Импортировать тестовые строки.
3. Проверить активный список.
4. Отсканировать КИЗ.
5. Проверить дубль КИЗ.
6. Завершить заказ.
7. Остановить тестовый стек через `docker compose down -v`.

VDS staging smoke:

1. Проверить `/health`.
2. Проверить `401` без Bearer-токена.
3. Проверить импорт временного заказа.
4. Проверить активный список.
5. Проверить скан/дубль/закрытие.
6. Удалить временные smoke-данные.
7. Выполнить ручной backup.

Фактический результат 2026-05-30:

- `/health` вернул `200`;
- закрытые `/api/v1/*` без Bearer-токена вернули `401`;
- импорт временного заказа прошел;
- повторный импорт не создал дубль позиции;
- сканирование, дубль КИЗ и проверки завершения заказа отработали корректно;
- `GET /api/v1/reports/day` вернул сводку по временным smoke-данным;
- ручной backup создал backup-файл;
- временные smoke-данные удалены из staging БД.

## Следующий Шаг После Этого Этапа

Следующий релизный блок:

1. Зарегистрировать `taksklad.uz` и настроить DNS `api.taksklad.uz`.
2. Переключить VDS с временного `sslip.io` host на `api.taksklad.uz`.
3. Подключить desktop к backend за feature flag.
4. Включить dual-write сканов: локально + backend.
5. Провести ручную приемку на копии реальных заказов.
