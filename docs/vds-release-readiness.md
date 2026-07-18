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
- Telegram worker принимает Excel-вложения `.xlsx/.xlsm`, преобразует их в backend import payload и отправляет в `POST /api/v1/imports`.
- Telegram worker показывает кнопки в нижнем меню Telegram и обрабатывает Excel-файлы через очередь `pending_events`.
- VDS staging smoke с импортом, сканами, завершением заказа, backup и cleanup пройден.
- Restore-drill из последнего backup-файла пройден на отдельной временной БД.

Не готово для production:

- DB-only desktop требует завершённой scoped-миграции credential на каждом рабочем Windows-профиле.
- Нет Alembic-миграций; текущая схема рассчитана на стартовый deploy.
- DNS `api.taksklad.uz` ещё не настроен: домен `taksklad.uz` ожидает финальную активацию/делегацию у регистратора.
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

### Telegram Excel Import

Telegram worker на VDS принимает Excel-документы, команды и callback-кнопки только из
`TELEGRAM_ADMIN_CHAT_IDS`. Чаты из `TELEGRAM_ALLOWED_CHAT_IDS`, которые не входят в
админский список, считаются `outbound-only`: бот может отправлять туда настроенные
отчёты, но молча игнорирует входящие сообщения и файлы.

Управление в Telegram:

- кнопки находятся в системном меню команд Telegram рядом с полем ввода, без навязчивой reply-клавиатуры;
- доступны кнопки `Дата отгрузки`, `Отчёт логистики`, `Выгрузка КИЗов`, `Статус`;
- системная кнопка меню команд Telegram настроена через `setMyCommands` и `setChatMenuButton`;
- команды меню: `/date`, `/logistics`, `/kiz_files`, `/status`;
- админские текстовые команды `/health`, `/imports` и `/logs` сохранены как скрытый fallback;
- все входящие команды и Excel-файлы доступны только chat ID из `TELEGRAM_ADMIN_CHAT_IDS`;
- Excel-файлы можно просто отправлять или пересылать в чат.

Поддерживается:

- `.xlsx`;
- `.xlsm`;
- лист `Заявки` как приоритетный;
- fallback на первый лист с обязательными колонками;
- алиасы колонок клиента, оплаты, товара, количества, даты, адреса, торгового представителя и SkladBot номера;
- ограничение размера через `TELEGRAM_WORKER_MAX_FILE_BYTES`;
- timeout скачивания через `TELEGRAM_WORKER_FILE_TIMEOUT_SECONDS`;
- расчёт блоков через `TAKSKLAD_DEFAULT_PIECES_PER_BLOCK`, если в Excel нет колонки блоков.

Очередь:

- каждый Excel-файл становится событием `telegram_excel_import` в `pending_events`;
- несколько файлов подряд обрабатываются последовательно;
- после постановки в очередь файл не теряется при перезапуске worker;
- итог каждого импорта возвращается сообщением в Telegram.

Проверено на staging:

- container rebuild с `openpyxl`;
- smoke внутри `telegram-worker`: тестовый `.xlsx` разобран в одну строку import payload;
- backend `/health` после rebuild отвечает `200`.
- локально покрыто тестами меню команд, логистический отчёт, `Выгрузка КИЗов`, кнопку `Статус`, постановка файла в очередь и последовательная обработка нескольких queued imports.
- после обновления нижнего меню `backend-api` и `telegram-worker` пересобраны и запущены на VDS;
- внутри VDS `telegram-worker` выполнен compile-check обновлённых файлов.
- Telegram API `getMyCommands` возвращает `date`, `logistics`, `kiz_files`, `status`;
- `deploy/vds/verify_telegram_menu.sh` проверяет live-меню Telegram через Bot API и входит в `acceptance_status.sh`;
- Telegram API `getChatMenuButton` возвращает `type=commands`.

Не проверено:

- реальный upload файла в боевой Telegram-чат;
- ручная сверка строк из реального Excel на Windows.

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

Дополнительный результат 2026-05-30 по Telegram Excel import:

- `backend-api` и `telegram-worker` пересобраны и перезапущены на VDS;
- `telegram-worker` успешно импортирует `openpyxl`;
- тестовый `.xlsx` внутри контейнера разобран в payload с `source=telegram`;
- реальные Telegram-файлы в этом шаге не отправлялись.

### One-shot provisioning до first adoption

Для аварийного релиза это только manual P0 bridge, а не production lifecycle automation. Обычный `Deploy Production` не создаёт, не вращает и не отзывает principals. Будущая автоматизация должна быть отдельным reviewed workflow; сейчас её нет.

До запуска one-shot доверенный администратор вне VDS обязан проверить immutable tag → exact main SHA, `release.json` и OCI `image@sha256` через GitHub/Sigstore attestation с ожидаемым release workflow. На VDS используется уже локально staged exact digest с `--pull never`; строка approval не заменяет attestation. Затем для того же стабильного UUID операции создаётся свежий `pg_dump` через `backup_postgres.sh` с `TAKSKLAD_BACKUP_OPERATION_ID` и новым `TAKSKLAD_BACKUP_RESULT_FILE`. One-shot принимает exact result/archive paths и непосредственно перед DB mutation перепроверяет bytes/SHA/freshness/operation UUID/Alembic head. Единственный head exact image обязан совпасть с live `alembic_version`.

`deploy/vds/provision_service_principal.sh` требует отдельные literal approvals, связанные с action, kind, identifier, operation UUID, source SHA, release tag, image digest и backup operation. Для каждой операции скрипт создаёт новое operation-unique internal network, временно подключает только exact PostgreSQL и dedicated `principal-provisioner`, затем всегда отключает и удаляет только эту сеть; обычный compose не держит PostgreSQL в admin network. Pre-existing/foreign network блокирует запуск и не удаляется. Writers не останавливаются; любая ошибка cleanup означает nonzero, а не ложный `OK`.

Acceptance использует canonical identifier `acceptance.release` и постоянный `/opt/stacks/taksklad/private/acceptance-canary.token` mode `0600`. Desktop handoff — временный `/opt/stacks/taksklad/private/desktop-token`: после установки pair token+expected identifier в DPAPI и успешного signed-helper canary применяется только `destroy-handoff` с новым approval. Rotation acceptance выполняет inode-safe atomic replace; desktop rotation требует отсутствующего staging file. Нельзя вручную перемещать, копировать, архивировать или удалять plaintext handoff. Revoke всегда DB-first; `cleanup=unverified`/residue — блокирующий partial state, foreign file сохраняется для escalation.

Live запуск one-shot, prod-write approval, backup/PITR truth, Windows handoff и revoke остаются отдельными gates родителя и оператора.

## Windows Приёмка С Backend Flags

Подробный чеклист: [windows-backend-acceptance.md](/Users/anton/Documents/work/TakSklad/docs/windows-backend-acceptance.md).

Перед приёмкой на Windows собрать test archive:

```powershell
.\tools\build_windows_test_archive.ps1 -InstallDependencies
```

Этот helper собирает только тестовый архив в `outputs\windows_test_build`; GitHub Release, рабочий `version.json` и автообновление не трогает.

Минимальные flags для тестовой Windows-копии:

```powershell
$env:TAKSKLAD_BACKEND_ENABLED = "1"
$env:TAKSKLAD_BACKEND_READ_ORDERS_ENABLED = "1"
$env:TAKSKLAD_BACKEND_BASE_URL = "https://api.taksklad.uz"
# backend credential читается только из current-user DPAPI store
$env:TAKSKLAD_BACKEND_TIMEOUT_SECONDS = "8"
```

Важно:

- токен не писать в документацию, чат, скриншоты и Git;
- credential file для acceptance principal заранее provisioned в защищённом каталоге VDS,
  принадлежит текущему deploy user, имеет mode `0400/0600` и не попадает в GitHub/env/log/release asset;
- provision, rotation и revoke canary principal требуют отдельного операторского gate;
- web-session auth проверяется отдельным credentialed canary;
- server-side data-free returns canary выполняется внутри deploy transaction до записи current-release;
  failure запускает schema-compatible runtime rollback и затем exact identity + health/ready verification;
- первый adoption: principal provision выполняется manual P0 bridge до deploy; candidate после deploy обязан дать canonical v2 exact-identifier `204`;
- включать flags сначала только на тестовой копии;
- при проблеме остановить acceptance и вручную вернуть предыдущий DB-compatible release/config;
  desktop fallback на локальное/Google-хранилище отсутствует;
- Windows candidate `2.0.44` не считать опубликованным до final preflight; переключение public channel на exact `2.0.44`/`onedir_zip` выполняется только единым immutable promotion.

## Следующий Шаг После Этого Этапа

Перед ручными acceptance-шагами локально запустить:

```bash
.venv/bin/python tools/release_preflight.py --phase candidate --skip-network
```

Candidate preflight проверяет локальный `2.0.44`, текущий поддерживаемый public channel и отсутствие tracked runtime/secret-файлов; он не доказывает публикацию. Final preflight отдельно требует exact опубликованный `2.0.44`, `onedir_zip`, downloads и attestations.

Фактические результаты ручной приёмки фиксировать в:

`outputs/taksklad_acceptance/ACCEPTANCE_RESULTS.md`

Строгая серверная проверка релизной готовности:

```bash
cd /opt/taksklad/app
./deploy/vds/acceptance_status.sh --require-go
```

После production smoke 2026-07-02 команда должна проходить, потому что `release_go_no_go.status=go`.

Текущий релизный статус:

1. Антон подтвердил боевой контур: Smartup auto export, Telegram import в БД, скан КИЗов и создание заявок SkladBot.
2. `outputs/taksklad_acceptance/ACCEPTANCE_RESULTS.md` зафиксирован как production smoke `2026-07-02`.
3. `tools/release_go_no_go.py` возвращает `status=go`.
4. `tools/feature_acceptance_status.py --require-manual-complete --require-no-open-errors` проходит: manual pending `0`, open errors `0`.
5. Live `/ready` чистый: DB/migrations OK, queue active `0`, `google_mirror=ok`.

Для повторной искусственной приемки можно использовать synthetic marker:

```bash
cd /opt/taksklad/app
./deploy/vds/diagnose_skladbot_match.sh --marker "ACCEPTANCE TELEGRAM 20260531" --limit 5 --request-limit 20
```

Новый Windows archive готовить только после отдельного release checklist и повторного smoke для новой версии.
