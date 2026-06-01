# Журнал Работ По Проекту

Документ фиксирует ход работ: что сделано, что не сделано, какие ошибки найдены, какие решения приняты и что требует проверки. Новые записи добавляются сверху.

## 2026-05-30

### Telegram нижнее меню и очередь Excel-файлов

**Цель:** сделать управление Telegram-ботом через нижнюю панель кнопок и разрешить отправлять несколько Excel-файлов подряд без ручного ожидания между файлами.

**Сделано:**

- В серверном `telegram-worker` добавлена постоянная нижняя клавиатура Telegram.
- Кнопки перенесены в reply keyboard:
  - `Дневной отчёт`;
  - `Статус backend`;
  - `История импортов`;
  - `Помощь`.
- Добавлена системная кнопка меню команд Telegram через `setMyCommands` и `setChatMenuButton`.
- Кнопка меню команд открывает те же действия: `/report`, `/health`, `/imports`, `/help`.
- `/start` и `/help` теперь показывают подсказку по нижнему меню, а не inline-кнопки.
- Текстовые команды `/report`, `/health`, `/imports`, `/help` оставлены как запасной вариант.
- Excel-документы `.xlsx/.xlsm` больше не импортируются прямо внутри обработки update.
- Каждый Excel-файл ставится в очередь `pending_events` с типом `telegram_excel_import`.
- Worker после обработки update забирает файлы из очереди и импортирует их по порядку.
- Если пользователь отправит или перешлёт 5 Excel-файлов подряд, все 5 будут поставлены в очередь.
- Для неподдержанных файлов возвращается понятное сообщение без падения worker.

**Проверки:**

- `.venv/bin/python -m unittest tests.test_backend_telegram_import` - 7 тестов пройдены.
- `.venv/bin/python -m py_compile backend/app/*.py tests/*.py` - успешно.
- `.venv/bin/python -m unittest discover -s tests` - 66 тестов пройдены.
- VDS `backend-api` и `telegram-worker` пересобраны и перезапущены.
- `https://api.135.181.245.84.sslip.io/health` вернул `200`.
- На VDS `backend-api` и `telegram-worker` находятся в статусе `Up`.
- Внутри контейнера `telegram-worker` выполнен `py_compile` для `telegram_worker.py` и `excel_importer.py`.
- Внутри VDS проверено через Telegram API: `getMyCommands` вернул `report`, `health`, `imports`, `help`.
- `getChatMenuButton` вернул `type=commands`.

**Ограничения:**

- Изменение сделано в серверной VDS-линии `backend/app/telegram_worker.py`.
- Старый desktop Telegram polling остаётся legacy/fallback и отдельно не переделывался под нижнее меню.
- Реальный боевой Telegram upload test нужно провести отдельным ручным шагом.

### Пользовательская инструкция по бизнес-процессу

**Цель:** зафиксировать TakSklad понятным языком для менеджеров, склада, руководителей и администратора, без технической перегрузки.

**Сделано:**

- Добавлен документ [user-business-process-guide.md](/Users/anton/Documents/work/TakSklad/docs/user-business-process-guide.md).
- Описаны роли: заказчик, менеджер, сотрудник склада, руководитель, администратор.
- Описаны процессы: Excel из Smartup/другого источника, Telegram import, desktop import, SkladBot-сопоставление, сканирование КИЗов, завершение заказа, печать, завершение дня.
- Добавлены Mermaid-диаграммы общего процесса, процесса по ролям и состояний заказа.
- В [project-overview.md](/Users/anton/Documents/work/TakSklad/docs/project-overview.md) добавлена ссылка на новую инструкцию.

**Ограничения:**

- Документ описывает текущую рабочую логику и отдельно помечает, что Smartup API, автоматическое создание SkladBot-заявок и production web frontend пока не готовы.

### Telegram Excel import через backend и подготовка Windows-приёмки

**Цель:** закрыть серверный импорт Excel-файлов из Telegram и подготовить безопасную Windows-приёмку desktop backend bridge без релиза и без push-уведомлений.

**Сделано:**

- Добавлен backend parser `backend/app/excel_importer.py` для `.xlsx/.xlsm`.
- Parser ищет лист `Заявки`, либо первый лист с обязательными колонками.
- Поддержаны алиасы колонок клиента, оплаты, товара, количества, даты, адреса, торгового представителя, количества блоков и номеров SkladBot.
- Дата берётся из колонки, имени файла, строк над заголовком или текущей даты как fallback.
- Если `Кол-во блок` нет, количество блоков считается через `TAKSKLAD_DEFAULT_PIECES_PER_BLOCK`.
- Excel workbook закрывается явно после чтения, чтобы Windows не держал файл залоченным.
- Telegram worker теперь:
  - принимает Excel-документ из разрешённого Telegram chat_id;
  - скачивает файл через Telegram file API;
  - ограничивает размер через `TELEGRAM_WORKER_MAX_FILE_BYTES`;
  - преобразует Excel в payload backend import;
  - отправляет строки в `POST /api/v1/imports`;
  - отвечает в Telegram итогом импорта.
- Ошибки Telegram download скрывают полный URL с bot token.
- Ответы Telegram worker отправляются обычным текстом без `parse_mode=HTML`, чтобы спецсимволы в имени файла или ошибке не ломали Telegram-ответ.
- В VDS compose добавлены настройки:
  - `TELEGRAM_WORKER_FILE_TIMEOUT_SECONDS`;
  - `TELEGRAM_WORKER_MAX_FILE_BYTES`;
  - `TAKSKLAD_DEFAULT_PIECES_PER_BLOCK`.
- Backend image пересобран на VDS, потому что добавлена зависимость `openpyxl`.
- `backend-api` и `telegram-worker` пересобраны и перезапущены на VDS.
- Добавлен документ Windows-приёмки: [windows-backend-acceptance.md](/Users/anton/Documents/work/TakSklad/docs/windows-backend-acceptance.md).

**Проверки:**

- `.venv/bin/python -m unittest tests.test_backend_telegram_import` - 2 теста пройдены.
- `.venv/bin/python -m unittest discover -s tests` - 61 тест пройден.
- `.venv/bin/python -m py_compile main.py sitecustomize.py taksklad/__init__.py src/taksklad/*.py tests/*.py backend/app/*.py` - успешно.
- `docker compose --env-file deploy/vds/.env.example -f deploy/vds/docker-compose.yml config` - успешно.
- `docker compose --env-file deploy/traefik/.env.example -f deploy/traefik/docker-compose.yml config` - успешно.
- VDS `/health` на временном домене `sslip.io` - `200`.
- На VDS `backend-api` и `telegram-worker` запущены после rebuild.
- Внутри контейнера `telegram-worker` выполнен smoke: создан тестовый `.xlsx`, parser вернул одну строку Telegram import payload.

**Что не проверено:**

- Реальная отправка Excel-файла в боевой Telegram-чат не выполнялась в этом шаге.
- Ручная Windows-приёмка с backend flags не выполнена в macOS/VDS-среде.
- Windows archive, `version.json`, GitHub Release и push-уведомления не трогались.

**Решение:**

- Telegram Excel import можно считать технически реализованным на staging.
- Перед релизом 2.0 нужен реальный Telegram upload test и Windows acceptance по чеклисту.

### Черновой frontend для VDS-линии

**Цель:** быстро получить рабочий web draft, чтобы можно было смотреть будущий TakSklad не только через desktop-приложение.

**Сделано:**

- Добавлена папка `frontend/` с React + Vite + TypeScript.
- Собран первый web-интерфейс TakSklad:
  - список активных заказов;
  - поиск по клиенту, адресу, оплате и номеру SkladBot;
  - карточка выбранного заказа;
  - выбор позиции;
  - ввод КИЗ и отправка скана в backend;
  - завершение заказа;
  - дневной отчёт;
  - история импортов.
- Frontend не содержит backend service token в JS-сборке.
- API-запросы frontend идут через same-origin `/api`.
- Nginx внутри frontend-контейнера проксирует `/api` во внутренний `backend-api` и сам добавляет `Authorization`.
- Публичный frontend закрыт Traefik basic-auth.
- Пароль basic-auth сохранён локально в `~/.taksklad/frontend-basic-auth.env`, в git и документацию не внесён.
- Добавлен Dockerfile frontend и nginx-template для отдачи статической сборки и API-proxy.
- VDS compose расширен сервисом `frontend`.
- Frontend поднят на VDS через Traefik:
  - `https://app.135.181.245.84.sslip.io`.
- Backend API получил CORS middleware для разрешённых frontend-origin.
- На VDS добавлен CORS origin для временного frontend-домена и будущего `app.taksklad.uz`.

**Проверки:**

- `npm run build` в `frontend/` - успешно.
- `python -m unittest tests.test_backend_skeleton` - успешно.
- `curl https://app.135.181.245.84.sslip.io/` без basic-auth - `401`.
- `curl https://app.135.181.245.84.sslip.io/` с basic-auth - `200`, отдаёт HTML frontend.
- `curl https://api.135.181.245.84.sslip.io/health` - `200`.
- CORS preflight с origin `https://app.135.181.245.84.sslip.io` - `200`, header `access-control-allow-origin` корректный.
- `GET https://app.135.181.245.84.sslip.io/api/v1/orders/active` через frontend-proxy с basic-auth - `200`.
- Headless Chrome screenshot публичного frontend - интерфейс отрисован.

**Что не готово:**

- Это web draft, не production-кабинет.
- Нет полноценной авторизации пользователей и ролей.
- Нет загрузки Excel через web-форму.
- Нет websocket/live-обновлений.
- Домен `taksklad.uz` ещё ожидает активацию/делегацию, поэтому используется временный `sslip.io`.

**Решение:**

- Frontend можно использовать как основу для будущего кабинета 2.0.
- До нормальной auth-модели доступ к web draft ограничивается Traefik basic-auth.
- После активации домена нужно переключить frontend на `app.taksklad.uz`, backend на `api.taksklad.uz` и обновить CORS origins.

### Product MVP 2.0: foundation, desktop bridge и VDS workers

**Дата:** 2026-05-30.

**Цель:** пройти план 2.0 максимально далеко без Windows-приёмки и без изменения `version.json`.

**Сделано:**

- Добавлен [deploy-rollback-runbook.md](/Users/anton/Documents/work/TakSklad/docs/deploy-rollback-runbook.md).
- Добавлен `deploy/vds/apply_schema.sh` для безопасного применения текущей SQL-схемы.
- Добавлен `deploy/vds/restore_drill.sh`; restore-drill на VDS выполнен в отдельную временную БД.
- Desktop получил backend feature flags:
  - `TAKSKLAD_BACKEND_ENABLED`;
  - `TAKSKLAD_BACKEND_READ_ORDERS_ENABLED`;
  - `TAKSKLAD_BACKEND_BASE_URL`;
  - `TAKSKLAD_BACKEND_API_TOKEN`.
- Добавлен desktop backend API client.
- Добавлена offline-очередь `pending_backend_events` для backend scan/complete событий.
- Скан КИЗ по-прежнему сначала пишется в локальный backup, затем ставится в backend-очередь.
- При ошибке backend сканирование не блокируется.
- Desktop умеет читать активные заказы из backend при включённом отдельном флаге чтения.
- Desktop Excel-импорт умеет отправлять строки в backend при включённом backend flag.
- `GET /api/v1/orders/active` теперь отдаёт `scan_codes` и номера SkladBot из Postgres.
- Добавлен `skladbot-worker` как отдельный VDS-контейнер.
- SkladBot worker проверяет окно сегодня + вчера и пишет результат матчинга в `orders.raw_payload`.
- Добавлен `telegram-worker` как отдельный VDS-контейнер.
- Telegram worker хранит offset в Postgres и снимает будущий конфликт двух desktop `getUpdates`.
- VDS compose расширен сервисами `skladbot-worker` и `telegram-worker`.
- VDS staging пересобран и поднят с тремя backend-процессами: API, SkladBot worker, Telegram worker.
- В Telegram worker отключены сторонние HTTP INFO-логи, чтобы transport-слой не писал секреты в URL.

**Проверки 2026-05-30:**

- `.venv/bin/python -m py_compile main.py sitecustomize.py taksklad/__init__.py src/taksklad/*.py tests/*.py backend/app/*.py` - успешно.
- `.venv/bin/python -m unittest discover -s tests` - 58 тестов пройдены.
- `bash -n deploy/vds/*.sh` - успешно.
- `docker compose --env-file deploy/vds/.env -f deploy/vds/docker-compose.yml config` - успешно.
- VDS `/health` на временном домене `sslip.io` - `200`.
- VDS `GET /api/v1/orders/active` с токеном - `200`, активных заказов `0`.
- VDS restore-drill - `restore_drill_ok`, таблицы читаются.
- VDS smoke: import `201`, duplicate scan `409`, complete `200`, report source `postgres`, cleanup smoke-данных выполнен.

**Что не получилось / внешние блокеры:**

- `api.taksklad.uz` пока не резолвится: нужна A-запись `api -> 135.181.245.84` у DNS-провайдера.
- На момент первого MVP-прогона реальные `SKLADBOT_API_TOKEN` и `TELEGRAM_BOT_TOKEN` ещё не были загружены; позже этот блокер снят, см. дополнение по ключам ниже.
- Windows-приёмку, сборку Windows archive и staged rollout нельзя честно завершить с macOS/VDS без рабочего Windows-компьютера.
- `version.json` специально не менялся, push-уведомления об обновлении не отправлялись.
- Telegram worker пока не делает полноценный авто-импорт Excel-вложений; до приёмки 2.0 использовать desktop/backend импорт.

**Решения:**

- DNS и Windows release вынесены в обязательные ручные acceptance-шаги.
- Backend bridge сделан за feature flags, чтобы текущая desktop-линия не изменила поведение без явного включения.
- VDS workers добавлены так, чтобы staging не ломался даже при временном отсутствии токенов.

**Дополнение по ключам:**

- Реальные Telegram/SkladBot ключи из локального `TakSklad_data.json` загружены в VDS `.env`.
- `skladbot-worker` и `telegram-worker` перезапущены.
- SkladBot API отвечает `200`.
- Telegram worker запущен с token/chat allowlist.
- DNS `taksklad.uz` всё ещё заблокирован: `dig +trace` показывает отсутствие делегации/зоны для домена на уровне `.uz`.

### Backend API MVP: дневной отчёт и автоматический backup

**Дата:** 2026-05-30.

**Цель:** закрыть последний backend MVP endpoint и добавить минимальную эксплуатационную защиту данных на VDS.

**Сделано:**

- Реализован `GET /api/v1/reports/day`.
- Отчёт строится из Postgres и не зависит от Google Sheets.
- Отчёт включает заказы выбранной даты и заказы, по которым были сканы в выбранную дату.
- Возвращаются totals по заказам, позициям, плану блоков, сканам, остаткам и группам оплаты.
- Добавлен systemd timer `taksklad-postgres-backup.timer`.
- На VDS timer включен, ручной запуск backup service создал backup-файл.
- Backend на VDS пересобран и поднят.
- VDS smoke `/reports/day` прошел на временном заказе.
- Smoke-данные удалены из staging БД.

**Проверки 2026-05-30:**

- `.venv/bin/python -m unittest discover -s tests` - 55 тестов пройдены.
- `.venv/bin/python -m py_compile main.py sitecustomize.py taksklad/__init__.py src/taksklad/*.py tests/*.py backend/app/*.py` - успешно.
- `docker compose --env-file deploy/vds/.env -f deploy/vds/docker-compose.yml config` - успешно.
- `docker compose --env-file deploy/traefik/.env.example -f deploy/traefik/docker-compose.yml config` - успешно.
- `bash -n deploy/vds/backup_postgres.sh deploy/vds/restore_postgres.sh deploy/vds/install_backup_timer.sh` - успешно.
- `git diff --check -- . ':!archive/**'` - успешно.
- VDS smoke: health `200`, protected report без токена `401`, import `201`, scans `201`, complete `200`, report `200`, cleanup `0/0`.

**Что остается после MVP:**

- Настроить DNS `api.taksklad.uz`.
- Подключить desktop к backend через feature flag.
- Включить dual-write сканов: локально + backend.
- Вынести SkladBot worker на сервер.
- Провести restore-drill на отдельной временной БД.
- Пройти ручную приемку на реальных заказах.

### Подготовлены backend import/history и Postgres backup для VDS-релиза

**Цель:** закрыть основные блокеры перед релизной приемкой VDS-линии: backend должен уметь сам наполнять `orders/order_items`, хранить историю импортов и иметь ручную процедуру backup/restore.

**Сделано:**

- Реализован `POST /api/v1/imports`.
- Реализован `GET /api/v1/imports`.
- Импорт принимает строки текущего desktop/Excel/Google-формата с русскими колонками.
- Несколько товаров одного клиента/адреса/даты/оплаты группируются в один заказ с несколькими позициями.
- Повторный импорт той же позиции не создает дубль.
- Невалидные строки считаются отдельно и возвращаются в `errors`.
- Результат импорта пишется в таблицу `imports`.
- Импорт пишет событие в `audit_log`.
- Добавлены `deploy/vds/backup_postgres.sh` и `deploy/vds/restore_postgres.sh`.
- Добавлен документ `docs/vds-release-readiness.md`.

**Что не сделано:**

- `GET /api/v1/reports/day` пока остается заглушкой `501`.
- Автоматический cron/systemd backup не включался.
- Desktop пока не подключался к backend.
- SkladBot worker ещё не перенесён на сервер.

**Проверки:**

- `.venv/bin/python -m unittest tests/test_backend_api_persistence.py` - 5 тестов пройдены.
- `.venv/bin/python -m unittest discover -s tests` - 53 теста пройдены.
- `.venv/bin/python -m py_compile main.py sitecustomize.py taksklad/__init__.py src/taksklad/*.py tests/*.py backend/app/*.py` - успешно.
- `docker compose --env-file deploy/vds/.env -f deploy/vds/docker-compose.yml config` - успешно.
- `docker compose --env-file deploy/traefik/.env.example -f deploy/traefik/docker-compose.yml config` - успешно.
- `bash -n deploy/vds/backup_postgres.sh` - успешно.
- `bash -n deploy/vds/restore_postgres.sh` - успешно.
- Локальный Docker/Postgres smoke с импортом:
  - первый импорт двух строк - `201`;
  - повторный импорт той же позиции - `201`, `duplicate_rows=1`, `items_created=0`;
  - активный список после импорта - `200`, один заказ с двумя позициями;
  - раннее завершение заказа - `409`;
  - скан первой позиции - `201`;
  - дубль КИЗ - `409`;
  - завершение при недосканированной второй позиции - `409`;
  - скан второй позиции - `201`;
  - завершение заказа после всех сканов - `200`;
  - история импортов - `200`;
  - тестовый Docker-стек остановлен через `docker compose down -v`.

### Реализован первый слой backend-бизнес-логики заказов и КИЗ

**Цель:** заменить часть MVP-заглушек реальной Postgres-логикой, не подключая пока desktop-приложение и не делая Windows-релиз.

**Сделано:**

- Реализован `GET /api/v1/orders/active`: отдаёт заказы, которые не находятся в статусах `completed`, `done`, `closed`, вместе с позициями.
- Реализован `POST /api/v1/scans`:
  - принимает `order_item_id` и КИЗ;
  - чистит пробелы вокруг кода;
  - пишет код в `scan_codes`;
  - увеличивает `scanned_blocks` у позиции;
  - переводит позицию в `completed`, когда отсканировано нужное число блоков;
  - возвращает `409`, если код уже был отсканирован;
  - пишет событие в `audit_log`.
- Реализован `POST /api/v1/orders/{order_id}/complete`:
  - проверяет, что обязательные КИЗ-позиции досканированы;
  - возвращает `409` со списком недосканированных позиций, если закрывать рано;
  - переводит заказ и позиции в `completed`;
  - пишет событие в `audit_log`.
- SQLAlchemy-модели переведены на переносимые типы `Uuid`/`JSON` с Postgres-вариантом `JSONB`, чтобы backend-логику можно было тестировать без Docker через SQLite.
- Добавлены FastAPI/SQLite тесты backend-персистентности.
- В backend-зависимости добавлен `httpx`, который требуется `FastAPI TestClient`.

**Что не сделано:**

- `POST /imports`, `GET /imports`, `GET /reports/day` пока остаются заглушками `501`.
- Desktop-приложение пока не отправляет сканы в backend.
- Миграционный механизм Alembic еще не добавлен.
- Синхронизация Google Sheets/SkladBot в Postgres еще не реализована.

**Проверки:**

- `.venv/bin/python -m unittest tests/test_backend_api_persistence.py` - 3 теста пройдены.
- `.venv/bin/python -m unittest discover -s tests` - 51 тест пройден.
- `.venv/bin/python -m py_compile main.py sitecustomize.py taksklad/__init__.py src/taksklad/*.py tests/*.py backend/app/*.py` - успешно.
- `docker compose --env-file deploy/vds/.env -f deploy/vds/docker-compose.yml config` - успешно.
- `docker compose --env-file deploy/traefik/.env.example -f deploy/traefik/docker-compose.yml config` - успешно.
- Локальный Docker/Postgres smoke:
  - `GET /api/v1/orders/active` - `200`;
  - раннее `POST /api/v1/orders/{id}/complete` - `409`;
  - первый `POST /api/v1/scans` - `201`;
  - повторный дубль того же КИЗ - `409`;
  - второй `POST /api/v1/scans` - `201`;
  - закрытие заказа после всех сканов - `200`;
  - активный список после закрытия - `[]`.
- Тестовый Docker-стек остановлен через `docker compose down -v`.
- Staging VDS обновлен: `backend-api` пересобран и перезапущен без изменения `version.json`.
- Внешняя проверка staging:
  - `GET /health` - `200`;
  - `GET /api/v1/orders/active` без токена - `401`;
  - `GET /api/v1/orders/active` с токеном - `200`, пустой список.
- VDS smoke с временным заказом через внешний HTTPS API:
  - активный список - `200`;
  - раннее завершение - `409`;
  - первый скан - `201`;
  - дубль КИЗ - `409`;
  - второй скан - `201`;
  - завершение после сканов - `200`;
  - временные smoke-заказы удалены, остаток `0`.

**Ошибки во время проверки:**

- Первый VDS smoke-запуск сорвался на локальном shell с `command not found: curl` после sourcing env-файлов. API и сервер при этом не падали.
- Решение: повторная проверка выполнена через явный `/usr/bin/curl`; оставшийся тестовый `vds-smoke` заказ найден и удалён из staging БД.

### Выполнен первичный VDS-deploy backend smoke

**Цель:** подготовить сервер Ubuntu 24.04 под VDS-линию TakSklad и проверить, что минимальный backend-каркас реально поднимается за HTTPS без выкладки Windows-релиза.

**Сделано:**

- Данные доступа сохранены локально в `~/.taksklad/*.env` с правами `600`; в Git они не добавлялись.
- По прямому указанию пароль root не менялся и вход по паролю не отключался.
- На сервер добавлен SSH key для дальнейшего подключения без ввода пароля.
- Проверена VDS: Ubuntu 24.04, Docker/Compose установлены, UFW включен.
- В UFW разрешены только базовые входы для текущего этапа: `22`, `80`, `443`.
- Создана внешняя Docker network `traefik`.
- Поднят Traefik на временных `sslip.io`-доменах.
- Backend-проект синхронизирован в `/opt/taksklad/app` без `.git`, `.venv`, секретов, логов, архивов и runtime-данных.
- На сервере создан рабочий `/opt/taksklad/app/deploy/vds/.env` с реальными значениями; файл не хранится в Git.
- Собраны и запущены контейнеры `postgres` и `backend-api`.
- Добавлен воспроизводимый шаблон Traefik в `deploy/traefik/`.

**Найденные ошибки и решения:**

- Traefik `v3.3` не видел Docker provider на Docker API `1.54`: в логах была ошибка `client version 1.24 is too old`.
- Решение: обновлен Traefik до `v3.6`; после этого маршрутизация backend заработала.
- Для совместимости в шаблоне Traefik закреплен `DOCKER_API_VERSION=1.44`.

**Проверки:**

- `docker run --rm hello-world` на сервере - успешно.
- `docker compose up -d --build postgres backend-api` на сервере - успешно.
- Postgres container - `healthy`.
- Внутренний `/health` из контейнера backend вернул `200`.
- Внешний `https://api.135.181.245.84.sslip.io/health` вернул `200`.
- Без Bearer-токена `GET /api/v1/orders/active` вернул `401`.
- С Bearer-токеном запрос дошел до приложения и вернул ожидаемый MVP-ответ `501 Not Implemented`.
- В Postgres созданы таблицы: `users`, `orders`, `order_items`, `scan_codes`, `imports`, `import_files`, `pending_events`, `audit_log`.
- Наружу запущены только `traefik`, `backend-api`, `postgres`; Adminer не запускался.

**Что не сделано:**

- DNS домена `taksklad.uz` еще не настроен на сервер. Пока используется временный домен `sslip.io`.
- Endpoint'ы бизнес-логики остаются MVP-заглушками `501`.
- Desktop-приложение не подключалось к backend.
- Backup/restore Postgres еще не настроены.
- Adminer не опубликован наружу.

### Настроена локальная среда разработки на ноутбуке

**Цель:** поставить на ноут всё необходимое для текущего проекта: desktop-разработка, backend-разработка, Docker/Compose для локальной проверки VDS-стека и GitHub-доступ.

**Сделано:**

- Проверено, что локальная `.venv` использует Python `3.12.13`.
- Установлены/проверены зависимости из `requirements.txt` и `backend/requirements.txt`.
- Проверен GitHub CLI: авторизация под аккаунтом `1fear`.
- Через Homebrew установлены:
  - `docker`
  - `docker-compose`
  - `docker-buildx`
  - `colima`
- Добавлен Docker config `~/.docker/config.json`, чтобы Docker видел Homebrew Compose/Buildx plugins.
- Colima запущен как локальный Docker engine и добавлен в Homebrew services.
- Создан локальный `deploy/vds/.env` из `deploy/vds/.env.example`; файл игнорируется Git.
- Создана локальная Docker network `traefik` для compose-smoke.
- Локально собран и поднят VDS-smoke стек `postgres + backend-api`.
- После проверки тестовый стек остановлен через `docker compose down -v`, чтобы не оставлять контейнеры и placeholder-том.
- Добавлена инструкция `docs/local-development-setup.md`.

**Проверки:**

- `.venv/bin/python -m unittest discover -s tests` - 47 тестов пройдены.
- `.venv/bin/python -m py_compile main.py sitecustomize.py taksklad/__init__.py src/taksklad/*.py tests/*.py backend/app/*.py` - успешно.
- `docker run --rm hello-world` - успешно.
- `docker compose --env-file deploy/vds/.env -f deploy/vds/docker-compose.yml config` - успешно.
- `docker compose --env-file deploy/vds/.env -f deploy/vds/docker-compose.yml up -d --build postgres backend-api` - успешно.
- В контейнере `backend-api` endpoint `/health` вернул `{"status":"ok"}`.
- Без Bearer-токена `GET /api/v1/orders/active` вернул `401`; с placeholder-токеном вернул ожидаемый `501`.
- В Postgres созданы таблицы: `users`, `orders`, `order_items`, `scan_codes`, `imports`, `import_files`, `pending_events`, `audit_log`.

**Что не сделано:**

- Реальные VDS-секреты и домены не заполнялись.
- Docker Compose на VDS не запускался; проверка была только локальная на Colima.
- Desktop-приложение к backend не подключалось.

### Начат VDS/backend MVP-каркас

**Цель:** начать серверную линию без релиза Windows и без push-уведомлений рабочим компьютерам. Первый шаг - зафиксировать минимальный backend API, PostgreSQL-схему и Docker Compose под уже подготовленную VDS-инфраструктуру.

**Пошаговый план этапа:**

1. Завести backend-каркас с минимальным API-контрактом и healthcheck.
2. Описать стартовую PostgreSQL-схему под заказы, позиции, КИЗы, импорты, очереди и аудит.
3. Добавить Dockerfile и compose-стек для VDS: PostgreSQL, backend API, Adminer, Traefik labels.
4. Добавить тесты, которые не требуют Docker и реальной базы, но проверяют структуру, env, схему и compose.
5. Прогнать unit/smoke/static проверки и отдельно отметить, что не проверено локально.

**Сделано:**

- Добавлена папка `backend/` с FastAPI-приложением.
- Реализован `GET /health`.
- Зафиксированы контрактные endpoint'ы MVP, которые пока честно возвращают `501 Not Implemented`:
  - `GET /api/v1/orders/active`
  - `POST /api/v1/scans`
  - `POST /api/v1/orders/{order_id}/complete`
  - `POST /api/v1/imports`
  - `GET /api/v1/imports`
  - `GET /api/v1/reports/day`
- Добавлена проверка сервисного Bearer-токена через `TAKSKLAD_API_TOKEN`; без токена авторизация отключена для локального smoke.
- Добавлена стартовая SQL-схема `backend/sql/001_initial_schema.sql`:
  - `users`
  - `orders`
  - `order_items`
  - `scan_codes`
  - `imports`
  - `import_files`
  - `pending_events`
  - `audit_log`
- Добавлены SQLAlchemy-модели под те же сущности.
- Добавлен `deploy/vds/docker-compose.yml`:
  - `postgres`
  - `backend-api`
  - `adminer`
  - внутренний network `taksklad-internal`
  - внешний network Traefik
  - Postgres не публикуется наружу.
- Добавлен `deploy/vds/.env.example` только с placeholder-значениями.
- `.gitignore` расширен для `.env`/`.env.*`, при этом `.env.example` не игнорируется.
- Добавлены тесты `tests/test_backend_skeleton.py`.

**Решения:**

- Backend пока не подключается к desktop-приложению. Рабочие компьютеры продолжают работать по текущей стабильной схеме.
- Windows-архив, GitHub Release, tag и `version.json` не менялись. Рабочая линия автообновления остаётся закреплена на `1.1.7`.
- Стартовая SQL-схема добавлена как init SQL для первого контейнера. Для следующих изменений потребуется Alembic или отдельная миграционная процедура.
- Docker Compose публикует HTTP-сервис через Traefik, а не открывает backend/Postgres напрямую наружу.

**Что не сделано:**

- Нет CRUD-логики и записи сканов в Postgres.
- Нет миграции существующих Google Sheets данных в Postgres.
- Нет desktop feature flag для dual-write в backend.
- Нет Telegram worker, SkladBot worker и report worker.
- Нет backup/restore процедуры Postgres.
- Docker Compose не был реально поднят локально, потому что Docker CLI в текущем окружении не установлен.

**Проверки:**

- `.venv/bin/python -m unittest tests/test_backend_skeleton.py` - 5 тестов пройдены.
- `.venv/bin/python -m unittest discover -s tests` - 47 тестов пройдены.
- `.venv/bin/python -m py_compile main.py sitecustomize.py taksklad/__init__.py src/taksklad/*.py tests/*.py backend/app/*.py` - успешно.
- `python3 -m json.tool version.json` - успешно, манифест всё ещё `1.1.7`.
- `git diff --check -- . ':!archive/**'` - успешно.
- Поиск старого имени проекта вне архива - совпадений нет.
- Локальный FastAPI smoke после установки backend-зависимостей:
  - `GET http://127.0.0.1:8010/health` вернул `200` и `{"status":"ok"}`.
  - `GET /api/v1/orders/active` вернул ожидаемый `501`.
  - Проверка `TAKSKLAD_API_TOKEN`: без Bearer-токена `401`, с верным токеном доступ проходит.
- SQLAlchemy metadata импортируется, таблицы схемы видны.

## 2026-05-29

### Продолжено разбиение `main.py`: печать и завершение дня

**Цель:** вынести оставшиеся боковые сценарии, но не распиливать критичный поток сканирования ради уменьшения файла.

**Сделано:**

- В `src/taksklad/app_printing.py` вынесены диалог параметров печати и повторная печать очереди `pending_prints`.
- В `src/taksklad/app_day_end.py` вынесены `update_stats_display()` и ручное завершение дня `end_day()`.
- `ScanningApp` подключает новые mixin'ы `PrintingActionsMixin` и `DayEndActionsMixin`.
- `src/taksklad/main.py` уменьшен с 1431 до 1172 строк.

**Решение:**

- `finish_legal_entity()` пока оставлен в `main.py`, потому что это часть рабочего сценария завершения заказа: там связаны сохраненные позиции, печать сводки, backup завершения и обновление списка.
- `create_day_report_excel` оставлен импортированным через `taksklad.main` для совместимости существующих тестов.

**Что не сделано:**

- Ядро сканирования, выбор позиций, завершение заказа и базовая сборка UI пока остаются в `main.py`.
- Backend/API, PostgreSQL и серверные worker-процессы пока не добавлялись.

**Проверки:**

- `.venv/bin/python -m py_compile main.py sitecustomize.py taksklad/__init__.py src/taksklad/*.py tests/*.py` - успешно.
- `.venv/bin/python -m unittest discover -s tests` - 35 тестов пройдены.
- `git diff --check -- . ':!archive/**'` - успешно.
- Поиск старого имени проекта вне архива - совпадений нет.

### Продолжено разбиение `main.py`: SkladBot orchestration

**Цель:** вынести фоновый SkladBot-синк из `main.py`, не меняя сам механизм сопоставления заявок и не трогая сканирование.

**Сделано:**

- В `src/taksklad/app_skladbot.py` вынесены `run_skladbot_periodic_refresh()` и `sync_skladbot_async()`.
- `ScanningApp` подключает новый `SkladBotActionsMixin`.
- В `ScanningApp` добавлена тонкая точка `fetch_sheet_data_after_skladbot_sync()`, чтобы mixin мог обновить список после успешного SkladBot-синка без импорта `main.py`.
- `src/taksklad/main.py` уменьшен с 1490 до 1431 строки.

**Решение:**

- `fetch_sheet_data_with_sync()` пока оставлен в `main.py`, потому что существующие тесты подменяют `sync_skladbot_request_numbers` через `taksklad.main`.
- Сам алгоритм SkladBot-матчинга не менялся: вынесена только Tkinter-оркестрация фонового запуска и применения результата в UI.

**Что не сделано:**

- Сканирование, выбор позиций, завершение заказа и обновление заказов пока остаются в `main.py`.
- Backend/API, PostgreSQL и серверный SkladBot worker пока не добавлялись.

**Проверки:**

- `.venv/bin/python -m py_compile main.py sitecustomize.py taksklad/__init__.py src/taksklad/*.py tests/*.py` - успешно.
- `.venv/bin/python -m unittest discover -s tests` - 35 тестов пройдены.
- `git diff --check -- . ':!archive/**'` - успешно.
- Поиск старого имени проекта вне архива - совпадений нет.

### Продолжено разбиение `main.py`: справочник товаров и контрольная панель

**Цель:** убрать из `main.py` еще два боковых UI-сценария, не затрагивая критичный поток сканирования.

**Сделано:**

- В `src/taksklad/app_catalog.py` вынесена UI-логика справочника товаров: список товаров, карточка, сохранение, создание и удаление правил.
- В `src/taksklad/app_control_panel.py` вынесены UI контрольной панели и расчет дневной статистики из Google Sheets.
- `ScanningApp` подключает новые mixin'ы `CatalogActionsMixin` и `ControlPanelMixin`.
- `src/taksklad/main.py` уменьшен с 1771 до 1490 строк.
- Убраны ставшие лишними импорты из `main.py`.

**Решение:**

- Расчет статистики контрольной панели перенесен вместе с UI в один модуль, потому что пока это операторская desktop-функция, а не общий backend-сервис.
- Ядро сканирования и сохранения КИЗов не трогалось, чтобы не рисковать рабочим сценарием склада.

**Что не сделано:**

- Сканирование, выбор позиций, завершение заказа, печать и SkladBot refresh-оркестрация пока остаются в `main.py`.
- Backend/API, PostgreSQL и серверные worker-процессы пока не добавлялись.

**Проверки:**

- `.venv/bin/python -m py_compile main.py sitecustomize.py taksklad/__init__.py src/taksklad/*.py tests/*.py` - успешно.
- `.venv/bin/python -m unittest discover -s tests` - 35 тестов пройдены.
- `git diff --check -- . ':!archive/**'` - успешно.
- Проверка лишних импортов для `main.py`, `app_catalog.py`, `app_control_panel.py` - чисто.

### Продолжено разбиение `main.py`: Telegram polling и Excel import

**Цель:** дальше уменьшить `main.py`, но не менять рабочее поведение desktop-приложения перед будущей серверной миграцией.

**Сделано:**

- В `src/taksklad/app_telegram.py` перенесены оставшиеся Telegram-действия из `ScanningApp`: обработка сообщений, callback-кнопок, импорт Excel из Telegram, polling updates и lock одного Telegram-слушателя.
- В `src/taksklad/app_imports.py` вынесена UI-логика ручного Excel-импорта: выбор файлов, preview, подтверждение, запись новых строк и Telegram-уведомление об импортированном документе.
- В `ScanningApp` оставлена тонкая точка `fetch_sheet_data_after_import()`, чтобы mixin'ы могли обновить список после импорта без обратного импорта `main.py`.
- `src/taksklad/main.py` уменьшен с 2347 до 1771 строки.

**Решение:**

- Не переносить пока `fetch_sheet_data_with_sync()` из `main.py`: существующие тесты подменяют его зависимости через `taksklad.main`, а преждевременный перенос потребовал бы отдельной адаптации тестового слоя.
- UI-mixin'ы используют методы `ScanningApp`, а не импортируют `main.py`, чтобы не создать циклические зависимости.

**Что не сделано:**

- `ScanningApp` пока остается в `main.py`.
- Сканирование, выбор позиций, сохранение КИЗов и построение основного UI пока не вынесены.
- Backend/API, PostgreSQL и серверные worker-процессы пока не добавлялись.

**Проверки:**

- `.venv/bin/python -m py_compile main.py sitecustomize.py taksklad/__init__.py src/taksklad/*.py tests/*.py` - успешно.
- `.venv/bin/python -m unittest discover -s tests` - 35 тестов пройдены.
- `git diff --check -- . ':!archive/**'` - успешно.

### Начато разбиение `main.py`

**Цель:** уменьшить god-модуль без переписывания поведения desktop-версии и подготовить код к будущему переносу на VDS/API.

**Сделано:**

- Вынесен HTTPS-клиент в `src/taksklad/http_client.py`.
- Вынесена логика автообновления в `src/taksklad/update_service.py`.
- Вынесена печать PNG-сводок и настройки печати в `src/taksklad/printing.py`.
- Вынесены локальные очереди `pending_saves`, `pending_prints` и `scan_backups` в `src/taksklad/pending_store.py`.
- Вынесены дневные отчеты, отчеты по документам, сортировка групп заявок и сводки по позициям в `src/taksklad/reports.py`.
- Вынесен виджет кнопки `AppButton` в `src/taksklad/ui_widgets.py`.
- Вынесен верхний Telegram-сервис в `src/taksklad/telegram_service.py`: настройки, API, отправка сообщений/документов, очередь Telegram, состояние дневных отчетов.
- Вынесены Telegram-действия UI в `src/taksklad/app_telegram.py`: отправка отчетов, меню, уведомления, daily report scheduler, polling updates и обработка Telegram-сообщений.
- Вынесена UI-логика автообновления в `src/taksklad/app_updates.py`.
- Вынесена UI-логика ручного Excel-импорта в `src/taksklad/app_imports.py`.
- Вынесена UI-логика справочника товаров в `src/taksklad/app_catalog.py`.
- Вынесены UI и расчет статистики контрольной панели в `src/taksklad/app_control_panel.py`.
- Вынесена SkladBot-оркестрация в `src/taksklad/app_skladbot.py`.
- Вынесены настройки/очередь печати в `src/taksklad/app_printing.py`.
- Вынесено ручное завершение дня и отображение статистики в `src/taksklad/app_day_end.py`.
- Вынесено форматирование дублей КИЗ в `src/taksklad/duplicate_codes.py`.
- В `src/taksklad/main.py` оставлены импорты старых публичных функций, чтобы существующие тесты и вызовы через `taksklad.main` не ломались.
- `src/taksklad/main.py` уменьшен с 4190 строк до 1172 строк.

**Ошибка в процессе:**

- После выноса отчетов упал тест дневного отчета: он подменял `BACKUP_DIR`, `REPORTS_DIR` и `load_pending_saves` через `taksklad.main`, а код отчета уже работал из `taksklad.reports`.

**Решение:**

- Тест обновлен так, чтобы подменять эти зависимости в новом модуле `taksklad.reports`. Рабочее поведение приложения не менялось.

**Что не сделано:**

- `ScanningApp` пока остается в `main.py`.
- Основной UI, сканирование, сохранение КИЗов, выбор позиций и завершение заказа пока остаются в `main.py`.
- Backend/API, PostgreSQL и серверные worker-процессы пока не добавлялись.

**Проверки:**

- `.venv/bin/python -m py_compile main.py sitecustomize.py taksklad/__init__.py src/taksklad/*.py tests/*.py` - успешно.
- `.venv/bin/python -m unittest discover -s tests` - 35 тестов пройдены.

### Локальная структуризация репозитория

**Сделано:**

- Кодовые модули перенесены в пакет `src/taksklad/`.
- Корневой `main.py` оставлен как тонкая точка запуска для разработки и PyInstaller.
- Добавлен bridge-пакет `taksklad/` и `sitecustomize.py`, чтобы локальные тесты могли импортировать `taksklad` без установки пакета.
- Старые локальные артефакты перенесены в `archive/repo-cleanup-20260529/`: логи, backup JSON, старые credentials-снимки, `reports/`, `exports/`, `scan_backups/`, legacy runtime JSON и cache.
- В корне оставлены активные `credentials.json` и `TakSklad_data.json`, чтобы не сломать локальный запуск.
- Во всех рабочих файлах проекта удалены упоминания старого названия; официальное название — `TakSklad`.

**Проверки:**

- `.venv/bin/python -m unittest discover -s tests` - 35 тестов пройдены.
- `.venv/bin/python -m py_compile main.py src/taksklad/*.py tests/*.py` - успешно.

### Подготовка к аккуратной уборке репозитория

**Решение:** уборку репозитория делать через инвентаризацию и локальный `archive/`, без удаления файлов вслепую.

**Сделано:**

- Добавлен документ `docs/repo-cleanup-inventory.md`.
- В `.gitignore` добавлен `archive/`.
- В `.gitignore` добавлены общие временные шаблоны `*.tmp`, `*.bak`, `*.backup`.
- Зафиксированы категории: код, документация, секреты, рабочие данные, логи, backup, отчёты, release-артефакты.

**Что не сделано специально:**

- Файлы не переносились автоматически, чтобы не сломать локальный запуск через текущие `credentials.json` и `TakSklad_data.json`.
- Реальные секреты и содержимое credential-файлов не выводились в отчёт.

### Решение: фокус на VDS, desktop только для критичных блокеров

**Контекст:** приложение в ближайшее время должно перейти на серверную архитектуру/VDS. Текущая desktop-версия нужна как рабочий инструмент склада до миграции, но не должна забирать время на несущественные улучшения.

**Решение:**

- Не делать крупный рефакторинг desktop-версии ради красоты кода до начала серверной миграции.
- Не добавлять в desktop новые тяжёлые фоновые процессы, которые позже всё равно должны уехать в backend/workers.
- Исправлять в desktop только то, что прямо мешает складу работать: сканирование, сохранение КИЗов, импорт, печать, безопасное обновление, понятные ошибки.
- Все новые архитектурные решения проектировать с учётом VDS: backend API, PostgreSQL, отдельные worker-сервисы, Docker Compose, серверный Telegram/SkladBot.
- Если есть выбор между временным desktop-обходом и серверной подготовкой, приоритет у серверной подготовки, пока складская работа не заблокирована.

### Решение по рабочей версии 1.1.7

**Контекст:** на рабочих компьютерах стоит `1.1.7`, глобальных проблем нет, приложение выполняет естественную функцию склада.

**Решение:**

- Не собирать и не выкатывать новый архив на этом этапе.
- Не переводить рабочие ПК на новую версию автоматически.
- Публичный `version.json` закрепить на стабильной линии `1.1.7`, чтобы рабочие компьютеры не получали принудительный апдейт и не видели лишний prompt обновления.
- Текущую ветку кода вести как стабилизационный кандидат будущей версии, пока не пройдены ручные проверки.

**Что изменено:**

- В `version.json` выставлено `latest_version = 1.1.7`.
- В `version.json` выставлено `min_supported_version = 1.1.7`.
- `mandatory` оставлен `false`.
- Поля `download_url` и SHA очищены, чтобы манифест стабильной линии не ссылался на непроверенный билд `1.1.17`.

**Что не делаем сейчас:**

- Не собираем release-архив.
- Не возвращаем `mandatory: true`.
- Не поднимаем `min_supported_version` выше `1.1.7`, пока склад работает на этой версии.

### В работе: стабилизация desktop перед серверной архитектурой

**Цель:** начать roadmap с самого рискованного места текущей версии - чтобы сканирование не блокировалось долгим обновлением заказов.

**Сделано:**

- Заведен этот журнал работ в `docs/implementation-log.md`.
- В `main.py` отделено фоновое обновление списка заказов от общей блокирующей операции `operation_in_progress`.
- Ручное обновление списка больше не должно сбрасывать выбранную позицию во время сканирования.
- Если пользователь выбрал позицию уже после старта обновления, завершение обновления тоже не сбрасывает этот выбор.
- При активной позиции обновление идет в фоне со статусом `Обновляю список заказов в фоне, сканирование доступно...`.
- Повторное нажатие `Обновить` во время уже идущего обновления показывает отдельное сообщение, а не общий текст `Дождитесь завершения текущей операции`.
- Фоновая синхронизация SkladBot не стартует параллельно с ручным обновлением, сохранением или активным сканированием.
- Обновлен устаревший тест SkladBot: минимальный `requests_limit` теперь 500, а не 100.
- Снижено количество чтений Google Sheets при обновлении списка: снимок строк, полученный для заказов, теперь переиспользуется для сбора уже отсканированных КИЗов.
- Добавлен cooldown для фоновых Google Sheets обращений после `429`/timeout: Telegram lock/state не добивают квоту повторными запросами сразу после временной ошибки.
- Для SkladBot добавлен `dry_run=True`, чтобы проверять сопоставление заявок без записи в Google Sheets.
- Для SkladBot добавлен отдельный `api_timeout_seconds` (по умолчанию 8 сек.), чтобы фоновой синк не зависал слишком долго на медленных деталях заявки.

**Решение:**

- Для реально блокирующих действий оставлен `operation_in_progress`: импорт, сохранение КИЗов, отчеты, контрольная панель.
- Для обновления заказов добавлено отдельное состояние `refresh_in_progress`.
- Сканирование проверяет только `operation_in_progress`, поэтому простая загрузка списка не мешает вводить КИЗы.
- Для защиты от `429 quota exceeded` убрано лишнее повторное `get_all_values()` на каждом обновлении списка.
- Для защиты от серийных `429`/timeout добавлен короткий backoff только на фоновые Google-операции (`Telegram lock`, общий `telegram_state`). Ручное обновление и сохранение КИЗов не блокируются этим cooldown.

**Что еще не сделано:**

- Не вынесен backend API.
- Не добавлен PostgreSQL.
- Не сделан серверный Telegram worker.
- Не сделан серверный SkladBot worker.
- Не собран новый release-архив.

**Что проверить вручную:**

1. Выбрать заказ.
2. Начать сканировать КИЗы.
3. Нажать `Обновить`.
4. Убедиться, что поле сканирования принимает коды, а текущая позиция не сбрасывается.
5. После завершения обновления проверить, что список слева обновился, а текущая позиция осталась на месте.

**Результат UI-smoke:**

- Автоматизированный smoke без реальных Google/SkladBot/Telegram вызовов пройден: во время фонового обновления тестовый КИЗ принят, `operation_in_progress = False`, текущая позиция сохранена после завершения обновления.
- Первый вариант smoke с настоящим фоновым потоком упал из-за ограничения Tkinter на macOS (`main thread is not in main loop`). Это ограничение тестового запуска без `mainloop`, не рабочий сценарий Windows-приложения. Повторный smoke выполнен через ручное завершение фоновой операции.

**Риски:**

- Если Google Sheets долго отвечает или выдает quota/timeout, статус обновления может висеть до завершения фонового потока.
- Если другой компьютер уже записал те же КИЗы в Google Sheets, локальная проверка дублей узнает об этом только после обновления списка или при сохранении позиции.

**Проверки в коде:**

- `python3 -m py_compile main.py` - успешно.
- `.venv/bin/python -m py_compile main.py` - успешно.
- `.venv/bin/python -m py_compile main.py storage.py sheets.py skladbot.py skladbot_sync.py` - успешно.
- `.venv/bin/python -m unittest tests/test_skladbot_sync.py tests/test_telegram_lock.py` - 18 тестов пройдены.
- `python3 -m json.tool version.json` - манифест валидный JSON.
- `.venv/bin/python -m unittest discover -s tests` - 35 тестов пройдены после первого набора стабилизации.

**Проверка SkladBot:**

- `sync_skladbot_request_numbers(..., dry_run=True)` прошел без записи в Google Sheets.
- В текущем Google `data`: 125 строк, активных невыполненных заказов 0, групп для SkladBot-сопоставления 0. Поэтому dry-run не нашел, что сопоставлять.
- Отдельная read-only проверка SkladBot API с лимитом 10 заявок прошла: API настроен, получено 10 заявок-кандидатов, в примерах есть `unloading_date`, recipient и товары.
- Полный read-only прогон с лимитом 500 был остановлен: слишком долгий для интерактивной проверки. После этого добавлен `SKLADBOT_API_TIMEOUT_SECONDS = 8`.

**Особенность проверки:**

- Во время тестов выводится `ERROR:root:SkladBot: не удалось получить заявки` - это ожидаемый сценарий внутри теста `test_api_failure_does_not_overwrite_sheet_statuses`. Тест специально имитирует падение API и проверяет, что статусы в таблице не затираются.

### Подготовка безопасного Git-снимка без автообновления

**Дата:** 2026-05-29.

**Цель:** зафиксировать текущую desktop-стабилизацию в Git так, чтобы рабочие компьютеры на стабильной линии не получили push-уведомление об обновлении.

**Сделано:**

- Публичный `version.json` оставлен закрепленным на рабочей линии `1.1.7`.
- В `version.json` очищены `download_url`, `download_url_onedir` и SHA, `mandatory` оставлен `false`.
- Проверено, что GitHub Actions workflow сборки Windows не запускается обычным `push`; он стартует только при опубликованном релизе или ручном `workflow_dispatch`.
- Документация очищена от конкретных значений Google service account, `private_key_id` и `SPREADSHEET_ID`; реальные значения сверяются только по локальной рабочей конфигурации.

**Что сознательно не делаем сейчас:**

- Не публикуем релиз.
- Не создаем тег для автообновления.
- Не собираем и не выкладываем архив в release assets.
- Не поднимаем `latest_version`/`min_supported_version` выше `1.1.7`.

**Следующий контроль перед выкладкой на склад:**

1. На Windows открыть сборку-кандидат.
2. Проверить запуск, обновление списка, выбор заказа, сканирование, завершение заказа, печать, завершение дня.
3. Отдельно проверить обновление списка во время активного сканирования.
4. Только после ручной проверки готовить release-архив и отдельное обновление `version.json`.

**Локальные проверки 2026-05-29:**

- `.venv/bin/python -m py_compile main.py sitecustomize.py taksklad/__init__.py src/taksklad/*.py tests/*.py` - успешно.
- `.venv/bin/python -m unittest discover -s tests` - 35 тестов пройдены.
- `python3 -m json.tool version.json` - manifest валидный JSON.
- Поиск старого имени проекта по рабочему дереву без `.git`, `.venv`, `archive` - совпадений нет.
- `git diff --check -- . ':!archive/**'` - успешно.
- Generated-файлы после тестов (`__pycache__`, `.pyc`, `docs/TakSklad.log`) перенесены в `archive/repo-cleanup-20260529/generated-after-main-split/`.

**Что не получилось проверить здесь:**

- Ручной Windows-smoke не выполнен в macOS-среде разработки. Его нужно пройти на рабочем Windows-компьютере или Windows runner перед выпуском архива.

### Переименование GitHub-репозитория и повторные проверки

**Дата:** 2026-05-30.

**Цель:** привести внешний GitHub-репозиторий к официальному имени TakSklad, чтобы будущая линия автообновления смотрела в корректный URL.

**Сделано:**

- GitHub-репозиторий переименован со старого исторического имени на `1fear/TakSklad`.
- Локальный `origin` переключен на `https://github.com/1fear/TakSklad.git`.
- Проверено, что `gh repo view 1fear/TakSklad` открывает новый репозиторий, default branch остается `main`.
- Проверено, что `git ls-remote --heads origin main` возвращает текущий `main`.
- Старый GitHub URL пока редиректится на новый репозиторий; это штатное поведение GitHub после rename.

**Локальные проверки 2026-05-30:**

- `.venv/bin/python -m py_compile main.py sitecustomize.py taksklad/__init__.py src/taksklad/*.py tests/*.py` - успешно.
- `.venv/bin/python -m unittest discover -s tests` - 35 тестов пройдены.
- `python3 -m json.tool version.json` - manifest валидный JSON.
- `git diff --check -- . ':!archive/**'` - успешно.
- Поиск старого имени проекта по рабочему дереву без `.git`, `.venv`, `archive` - совпадений нет.

**Автообновление:**

- `version.json` не повышался и остается закрепленным на `1.1.7`.
- Release/tag/workflow-сборка не запускались.
- Push-уведомление на рабочие компьютеры не готовилось.

### Desktop-стабилизация без релиза: ошибки Google/SkladBot и долгие обновления

**Дата:** 2026-05-30.

**Цель:** снизить риск зависаний и технических ошибок в UI без выкладки нового Windows-архива на склад.

**Сделано:**

- Расширена классификация Google Sheets ошибок: `403`, `invalid_grant`, `429/quota`, DNS/connection/timeout/SSL теперь превращаются в понятные сообщения для оператора.
- Неудачное обновление списка заказов больше не считается критической ошибкой приложения: UI показывает мягкий fallback и оставляет последний загруженный список доступным.
- Повторное нажатие `Обновить` во время фонового обновления показывает, сколько секунд оно уже идёт, и поясняет, что можно работать с уже загруженным списком.
- Для долгого фонового обновления добавлен статус-таймер: каждые 15 секунд UI подтверждает, что обновление ещё идёт, а интерфейс не завис.
- SkladBot ошибки нормализованы: неверный токен, `429`, timeout/network и некорректный JSON дают понятные сообщения.
- SkladBot-синхронизация больше не пробрасывает исключение наружу, если не удалось прочитать `data` или записать результаты в Google Sheets; список заказов не блокируется.
- При падении фонового SkladBot UI показывает предупреждение в статусе, но не открывает критическое окно и не сбивает сканирование.

**Что не менялось:**

- `version.json` не повышался и остается закрепленным на `1.1.7`.
- Release/tag/workflow-сборка не запускались.
- Windows-архив не собирался.

**Локальные проверки 2026-05-30:**

- `.venv/bin/python -m py_compile main.py sitecustomize.py taksklad/__init__.py src/taksklad/*.py tests/*.py` - успешно.
- `.venv/bin/python -m unittest discover -s tests` - 42 теста пройдены.
- `python3 -m json.tool version.json` - manifest валидный JSON.
- `git diff --check -- . ':!archive/**'` - успешно.

**Что не получилось проверить здесь:**

- Ручной Windows-smoke и реальные боевые интеграции Google/SkladBot/Telegram/печать не запускались в этой macOS-среде.

### VDS-релизная подготовка: импорт, backup и staging smoke

**Дата:** 2026-05-30.

**Цель:** довести серверную часть до состояния, где ее можно проверять как staging-кандидат перед подключением desktop-приложения.

**Сделано:**

- Реализован backend-импорт заказов через `POST /api/v1/imports`.
- Добавлена история импортов через `GET /api/v1/imports`.
- Импорт создает `orders` и `order_items`, группирует товары одного клиента/адреса/даты/оплаты/заявки SkladBot в один заказ.
- Повторный импорт той же позиции не создает дубль.
- Невалидные строки возвращаются в `errors`, а итог импорта пишется в `imports` и `audit_log`.
- Добавлены ручные скрипты backup/restore Postgres.
- На VDS обновлен backend staging.
- В `deploy/vds/docker-compose.yml` явно указана сеть Traefik через `traefik.docker.network=${TRAEFIK_NETWORK:-traefik}` для backend/adminer.

**Почему добавлена явная сеть Traefik:**

- После пересоздания backend-контейнера внешний `/health` начал зависать: TLS принимался, но ответ от backend не доходил.
- Причина: backend подключен к двум сетям (`taksklad-internal` и `traefik`), и Traefik мог выбрать не ту сеть для проксирования.
- Исправление закрепляет публичный route на сети `traefik`.

**VDS smoke 2026-05-30:**

- `/health` - `200`.
- `/api/v1/orders/active` без Bearer-токена - `401`.
- Импорт временного заказа - `201`.
- Повторный импорт - `201`, дубль позиции не создает новую запись.
- Завершение недосканированного заказа - `409`.
- Первый scan - `201`.
- Повторный scan того же КИЗ - `409`.
- Второй scan - `201`.
- Завершение после частичного скана - `409`.
- Scan второй позиции - `201`.
- Завершение после полного скана - `200`.
- История импортов - `200`.
- Ручной backup Postgres создал backup-файл.
- Smoke-данные удалены, проверка staging БД показала `orders=0 imports=0` для временного `vds-release-smoke`.

**Локальные проверки 2026-05-30:**

- `.venv/bin/python -m unittest discover -s tests` - успешно.
- `.venv/bin/python -m py_compile main.py sitecustomize.py taksklad/__init__.py src/taksklad/*.py tests/*.py backend/app/*.py` - успешно.
- `docker compose --env-file deploy/vds/.env -f deploy/vds/docker-compose.yml config` - успешно.
- `docker compose --env-file deploy/traefik/.env.example -f deploy/traefik/docker-compose.yml config` - успешно.
- `bash -n deploy/vds/backup_postgres.sh` - успешно.
- `bash -n deploy/vds/restore_postgres.sh` - успешно.
- `git diff --check -- . ':!archive/**'` - успешно.

**Что не готово для production:**

- DNS `api.taksklad.uz` еще не направлен на VDS.
- Desktop еще не подключен к backend через feature flag.
- SkladBot worker еще не перенесен на сервер.
- Restore-drill еще не проводился.

### PowerVPS, Worker-Ключи И DNS-Блокер

**Дата:** 2026-05-30.

**Сделано:**

- на VDS загружены server-side ключи Telegram и SkladBot без вывода секретов в логи;
- `skladbot-worker` и `telegram-worker` пересобраны/перезапущены на VDS;
- SkladBot API отвечает `200`;
- Telegram worker запущен с allowlist chat_id;
- в Telegram worker отключены `httpx/httpcore` INFO-логи, чтобы transport-слой не писал полный URL с токеном;
- проверена панель PowerVPS: там управляется только VDS, DNS-зоны `taksklad.uz` нет;
- повторно проверен `WHOIS taksklad.uz`: домен не найден в базе `.uz`;
- добавлен [switch_backend_host.sh](/Users/anton/Documents/work/TakSklad/deploy/vds/switch_backend_host.sh) для быстрого переключения VDS на `api.taksklad.uz` после регистрации домена.

**Итог:**

- временный staging URL `https://api.135.181.245.84.sslip.io/health` работает;
- `api.taksklad.uz` нельзя включить, пока домен `taksklad.uz` не зарегистрирован у `.uz`-регистратора;
- после регистрации нужна A-запись `api -> 135.181.245.84`, затем на VDS: `./deploy/vds/switch_backend_host.sh api.taksklad.uz`.

### Регистрация taksklad.uz И DNS-Ожидание

**Дата:** 2026-05-30.

**Сделано:**

- домен `taksklad.uz` зарегистрирован/оплачен через Hostmaster;
- включен DNS manager для домена;
- добавлена A-запись `api.taksklad.uz -> 135.181.245.84`;
- авторитетный DNS Hostmaster (`ns1.hostmaster.uz`) уже возвращает `135.181.245.84` для `api.taksklad.uz`;
- `WHOIS taksklad.uz` показывает статус `ACTIVE` и NS `ns1.hostmaster.uz` / `revers.hostmaster.uz`.

**Текущий блокер:**

- публичная зона `.uz` пока не делегирует `taksklad.uz`: `dig +trace api.taksklad.uz A` доходит до `.uz` и получает отрицательный ответ;
- публичные DNS (`1.1.1.1`, `8.8.8.8`) пока не возвращают A-запись `api.taksklad.uz`;
- из-за этого пока нельзя выпускать Let’s Encrypt сертификат и переключать VDS на `api.taksklad.uz`.
- запрос на активацию домена отправлен в Hostmaster, но активация выполняется по рабочему графику Hostmaster: понедельник-пятница, 09:00-18:00.

**Следующее действие:**

1. Дождаться появления делегации в публичной зоне `.uz`.
2. Проверить `dig @1.1.1.1 api.taksklad.uz A +short`.
3. После появления `135.181.245.84` выполнить на VDS:

```bash
cd /opt/taksklad/app
./deploy/vds/switch_backend_host.sh api.taksklad.uz
```

4. Проверить `https://api.taksklad.uz/health`.

### Черновой Web-Frontend На VDS

**Дата:** 2026-05-30.

**Сделано:**

- создан черновой React/Vite frontend в папке `frontend/`;
- добавлены рабочие экраны: активные заказы, карточка выбранного заказа, сканирование КИЗ, завершение заказа, дневной отчет, история импортов;
- frontend собирается отдельным Docker-контейнером через nginx;
- frontend больше не требует ручного ввода backend service token в браузере;
- запросы браузера идут на same-origin `/api`, а nginx внутри frontend-контейнера добавляет backend Bearer token на серверной стороне;
- публичный frontend закрыт Traefik basic-auth;
- пароль basic-auth сохранён локально в `~/.taksklad/frontend-basic-auth.env`;
- VDS compose расширен сервисом `frontend`;
- временный frontend поднят по адресу `https://app.135.181.245.84.sslip.io`;
- backend CORS настроен через `TAKSKLAD_CORS_ORIGINS` для прямых проверок API с frontend-origin;
- на VDS добавлен origin `https://app.135.181.245.84.sslip.io`;
- `frontend/node_modules`, `frontend/dist` и `frontend/tsconfig.tsbuildinfo` исключены из git/Docker context.

**Проверки:**

- `npm run build` в `frontend` - успешно;
- `.venv/bin/python -m unittest discover -s tests` - 59 тестов OK;
- `docker compose --env-file deploy/vds/.env.example -f deploy/vds/docker-compose.yml config` - успешно;
- VDS `backend-api` и `frontend` пересобраны и запущены;
- `https://app.135.181.245.84.sslip.io` без basic-auth возвращает `401`;
- `https://app.135.181.245.84.sslip.io` с basic-auth возвращает frontend HTML;
- CORS preflight с origin frontend на `https://api.135.181.245.84.sslip.io/api/v1/orders/active` возвращает `200` и `access-control-allow-origin`;
- `https://app.135.181.245.84.sslip.io/api/v1/orders/active` с basic-auth возвращает `200` через frontend-proxy без ручного service token в браузере.

**Ограничения:**

- это черновой frontend, не production UI;
- полноценной пользовательской auth-модели пока нет, стоит временный basic-auth;
- домен `taksklad.uz` еще ожидает финальную публичную делегацию Hostmaster, поэтому frontend/API временно работают на `sslip.io`;
- `version.json` не менялся, desktop push-уведомления не отправлялись.

### Telegram Import, Логистика, SkladBot Matching И КИЗ По Файлам

**Дата:** 2026-05-31.

**Контекст:**

- SmartUp/Excel не обязан содержать отдельный файл или поле даты отгрузки; это закрывается тем, что менеджер задаёт дату вручную в Telegram.
- Менеджер задаёт актуальную дату отгрузки в Telegram перед отправкой Excel-файлов или указывает дату в подписи к файлу.
- SkladBot работает в блоках, а Excel может приходить в штуках/пачках; сравнение со SkladBot делается только по блокам.
- Название товара в SkladBot может быть длиннее, поэтому товар нормализуется до цвета и формата.
- Адрес не является жёстким критерием SkladBot-сопоставления.
- Для логистики нужен файл именно с координатами, а не просто адресом.

**Сделано:**

- Добавлена точка восстановления перед доработками: `restore-2026-05-31_before_mvp_updates_003050`.
- Telegram worker получил нижнее меню: `Дата отгрузки`, `Отчёт логистики`, `КИЗ по файлам`.
- Telegram import ставит Excel-файлы в очередь и применяет дату отгрузки из состояния чата или подписи к файлу.
- Excel importer поддерживает координаты, цену, сумму строки и пересчёт в блоки.
- Если сумма в файле не указана, считается `Кол-во блок * 240000`.
- Backend сохраняет координаты заказа и сумму/цену позиции в Postgres.
- Добавлен `GET /api/v1/logistics/dates` для выбора доступной даты отгрузки.
- Добавлен `GET /api/v1/logistics/report` для одного логистического Excel-файла по выбранной дате.
- Логистический отчёт заполняет координаты в отдельные поля и в широту/долготу.
- SkladBot matching сужен до заявок типа `3PL отгрузка`; `Возврат 3PL` не должен матчиться как отгрузка.
- SkladBot matching сравнивает дату выгрузки, клиента, оплату, нормализованный товар и количество блоков.
- Адрес больше не является жёстким блокером SkladBot-сопоставления.
- Добавлен `GET /api/v1/reports/kiz/source-files`: список исходных Excel-файлов, где все позиции завершены.
- Добавлен `GET /api/v1/reports/kiz/source-file`: Excel с КИЗами по выбранному завершённому исходному файлу.

**Проверки:**

- `py_compile` для новых backend-модулей прошёл.
- `python -m unittest tests.test_backend_telegram_import tests.test_backend_api_persistence tests.test_backend_skladbot_worker` - 22 теста OK.
- `python -m unittest discover -s tests` - 74 теста OK.

**Что не сделано в этом шаге:**

- Реальный Telegram smoke и реальный SkladBot match были проверены позднее отдельным шагом, см. блок ниже.
- Автоматическое создание заявок в SkladBot не реализовывалось.
- Windows-архив и desktop-релиз не собирались.
- `version.json` не повышался, push-уведомления не отправлялись.

### VDS Smoke После Telegram/Logistics/SkladBot Доработок

**Дата:** 2026-05-31.

**Сделано:**

- На VDS создана точка восстановления перед обновлением:
  - `/opt/taksklad/restore_points/server_20260530T194938Z/app-files.tar.gz`;
  - `/opt/taksklad/backups/postgres/taksklad-postgres-20260530T194941Z.sql.gz`.
- На VDS выложен обновлённый backend-код.
- Пересобраны Docker images `backend-api`, `telegram-worker`, `skladbot-worker`.
- Во время выкладки `telegram-worker` и `skladbot-worker` были остановлены, потом запущены обратно.

**Проверки:**

- `https://api.135.181.245.84.sslip.io/health` вернул `200`.
- Внутри backend-контейнера выполнен smoke:
  - создан тестовый импорт `SMOKE_MVP_20260531_0052.xlsx`;
  - заказ отсканирован двумя тестовыми КИЗами;
  - заказ завершён;
  - логистический Excel сформирован;
  - Excel `КИЗ по файлам` сформирован;
  - тестовые строки очищены из Postgres.
- Проверка очистки подтвердила `orders=0` и `imports=0` для smoke-маркеров.
- Внешний protected endpoint `/api/v1/logistics/dates` с server-side токеном вернул `200`.
- Telegram token проверен через `getMe`; бот: `SkladKis_bot`.
- Telegram menu установлен командами `date`, `logistics`, `kiz_files`.
- SkladBot one-shot worker получил ответ `200` от SkladBot API. На VDS не было активных backend-заказов, поэтому результат: `requests=0 orders=0 matched=0 not_found=0 multiple=0`.

**Ограничения:**

- Полный входящий Telegram import от пользовательского аккаунта не проверен. Через Bot API бот не может сам создать себе входящее пользовательское сообщение.
- SkladBot matching на реальной заявке проверен позднее отдельным безопасным smoke без создания новой заявки в WMS, см. блок ниже.
- `version.json` не менялся, desktop push-уведомления не отправлялись.

### Дополнительный VDS Smoke: Telegram Файл И Реальный SkladBot Match

**Дата:** 2026-05-31.

**Что уточнено по Telegram:**

- Найдена причина ошибок `getUpdates`: long polling был дольше HTTP timeout клиента.
- Добавлен отдельный короткий timeout для polling: `TELEGRAM_WORKER_POLL_TIMEOUT_SECONDS=15`.
- Ошибки Telegram worker теперь не раскрывают bot token в тексте.
- После перезапуска worker повторяющиеся ошибки `getUpdates` не появились.

**Telegram file smoke:**

- Создан тестовый Excel-файл `/tmp/taksklad_telegram_smoke_20260531.xlsx`.
- Файл загружен в Telegram через Bot API, получен реальный `file_id`.
- Основной `telegram-worker` был временно остановлен, чтобы не было гонки.
- One-shot worker скачал файл из Telegram API по `file_id`, поставил импорт в очередь и обработал его.
- Дата отгрузки применена как `2026-05-31`.
- Импорт создал тестовый заказ, затем тестовые данные были полностью удалены.
- Проверка очистки: `tg_smoke_orders=0`, `tg_smoke_imports=0`, `telegram_pending=0`.

**Что уточнено по SkladBot:**

- Worker больше не обращается к SkladBot API, если в backend нет активных заказов для сопоставления.
- Добавлена обработка `429 Too Many Requests`: задержка, повтор и пропуск проблемной детали без падения worker.
- Исправлена логика фильтра даты: для отбора используется `unloading_date` заявки SkladBot, а не только `created_at`.
- Это важно, потому что заявка может быть создана раньше, но отгрузка стоит на сегодня/вчера.

**SkladBot real-match smoke:**

- В SkladBot использована уже существующая реальная заявка без создания новой заявки:
  - `request_id=190961`;
  - `request_number=WH-R-190960`;
  - тип: `Отгрузка 3PL`;
  - дата выгрузки: `2026-05-29`;
  - клиент: `NICE SHOP`;
  - оплата: `Терминал`;
  - товар: `Chapman Brown OP 20`;
  - количество: `1` блок.
- В backend временно создан тестовый заказ с совпадающими полями.
- One-shot `skladbot-worker` нашёл совпадение:
  - `requests=1`;
  - `orders=1`;
  - `matched=1`;
  - `not_found=0`;
  - `multiple=0`.
- В заказ записались `skladbot_request_number=WH-R-190960` и `skladbot_request_id=190961`.
- Тестовые данные были удалены, основной `skladbot-worker` запущен обратно.
- Проверка очистки: `orders_total=0`, `smoke_skladbot_orders=0`, `smoke_skladbot_imports=0`, `telegram_pending=0`.

**Ограничения:**

- Новая заявка в SkladBot не создавалась специально, чтобы не менять WMS/остатки.
- Windows desktop UI физически не проверялся в этой среде.

### Контрольный Прогон После Уточнения Рисков

**Дата:** 2026-05-31.

**Что зафиксировано:**

- Smartup/Excel без даты отгрузки не считается блокером: дату задаёт менеджер в Telegram.
- Для SkladBot все количества сравниваются только в блоках.
- Длинные названия товаров SkladBot нормализуются до цвета и формата.
- Адрес остаётся мягким критерием и не блокирует совпадение.
- Логистический отчёт должен опираться на координаты.

**Проверки текущего состояния:**

- `.venv/bin/python -m unittest discover -s tests` - 74 теста OK.
- `.venv/bin/python -m py_compile backend/app/*.py tests/*.py` - OK.
- `git diff --check` - OK.
- `npm run build` в `frontend/` - OK.
- `docker compose --env-file deploy/vds/.env.example -f deploy/vds/docker-compose.yml config` - OK.
- Быстрый поиск секретов по рабочим файлам не нашёл реальных токенов/паролей, только placeholder/env-названия.

**Что остаётся вне автоматической проверки:**

- входящее Telegram-сообщение от реального пользовательского аккаунта;
- физическая Windows-приёмка desktop UI;
- сборка и проверка Windows-архива.

**Текущее состояние VDS после checkpoint:**

- `backend-api`, `frontend`, `postgres`, `telegram-worker`, `skladbot-worker` работают.
- Server restore `/opt/taksklad/restore_points/server_20260530T194938Z` на месте.
- Postgres backup `taksklad-postgres-20260530T194941Z.sql.gz` на месте.
- `https://api.135.181.245.84.sslip.io/health` вернул `200`.
- `https://app.135.181.245.84.sslip.io/` без basic-auth вернул `401`, доступ закрыт.
- Открыт draft PR без релиза: `https://github.com/1fear/TakSklad/pull/1`.
- GitHub checks для ветки пустые, потому что push не запускает Windows release workflow.
- VDS логи workers после простоя проверены: SkladBot worker корректно пропускает API без активных заказов, новых падений Telegram worker в проверенном окне не видно.

### Web Frontend UI Smoke На VDS

**Дата:** 2026-05-31.

**Цель:** проверить не только backend API, но и реальный web-интерфейс VDS: выбор заказа, сканирование КИЗов и завершение заказа.

**Проверка:**

- Через backend API создан временный заказ `WEB_UI_SMOKE_20260531_0118`.
- В заказе 2 позиции и 3 блока:
  - `Chapman Brown OP 20` - 2 блока;
  - `Chapman Gold SSL 20` - 1 блок.
- Через web-frontend `https://app.135.181.245.84.sslip.io/` выполнено:
  - вход через basic-auth;
  - поиск заказа;
  - выбор первой позиции;
  - запись 2 КИЗов;
  - выбор второй позиции;
  - запись 1 КИЗа;
  - завершение заказа;
  - проверка, что заказ исчез из активного списка.
- Перед очисткой БД подтвердила:
  - order status `completed`;
  - обе позиции status `completed`;
  - scanned/planned: `2/2` и `1/1`.
- После проверки smoke-данные удалены:
  - `orders=0`;
  - `imports=0`;
  - `import_files=0`;
  - `pending_events=0`.

**Ограничение:**

- Это проверка web-frontend на VDS, а не Windows desktop UI.

### Acceptance Cleanup Script

**Дата:** 2026-05-31.

**Цель:** после ручного Telegram/Windows acceptance можно безопасно проверить и удалить тестовые данные по маркеру, не трогая реальные заказы.

**Сделано:**

- Добавлен `deploy/vds/cleanup_acceptance_marker.sh`.
- Скрипт по умолчанию работает в dry-run.
- Удаление требует явный флаг `--apply`.
- Защита от случайного запуска: marker должен содержать `ACCEPTANCE`, `WEB_UI_SMOKE` или `SMOKE_MVP`.
- Runbook обновлён командами dry-run и apply.

**Проверки:**

- `bash -n deploy/vds/cleanup_acceptance_marker.sh` - OK.
- Небезопасный marker `BAD_MARKER` отклонён.
- VDS dry-run по `ACCEPTANCE TELEGRAM 20260531` успешно подключился к backend-api и вернул нули по `orders/imports/import_files/pending_events/audit_log`.

### Финальная Фиксация Рисков Chapman-Процесса

**Дата:** 2026-05-31.

**Что зафиксировано после уточнения Антона:**

- Smartup/Excel не обязан содержать отдельный файл отгрузки: дату отгрузки задаёт менеджер в Telegram.
- Для SkladBot все количества приводятся к блокам; пачки/штуки напрямую со SkladBot не сравниваются.
- Товар сравнивается по нормализованным признакам Chapman: цвет `brown`/`red`/`gold` и формат `OP`/`SSL`.
- Адрес остаётся мягким признаком, не главным блокирующим критерием SkladBot-матчинга.
- В логистический отчёт должны попадать координаты доставки, не адрес.

**Документы обновлены:**

- `docs/project-knowledge-base.md` - добавлены утверждённые правила Chapman-процесса.
- `docs/project-architecture.md` - добавлен ADR-012 и риск логистического отчёта без координат.
- `docs/product-mvp-2.0-plan.md` - правила добавлены в обязательный scope MVP 2.0.

**Проверки:**

- `.venv/bin/python -m unittest discover -s tests` - 74 теста OK.
- `.venv/bin/python -m py_compile backend/app/*.py tests/*.py` - OK.
- `git diff --check` - OK.
- `npm run build` в `frontend/` - OK.
- `bash -n deploy/vds/*.sh` для рабочих deploy/backup/restore/cleanup скриптов - OK.
- `docker compose --env-file deploy/vds/.env.example -f deploy/vds/docker-compose.yml config` - OK.

### Доработка После Финального Брифа Chapman

**Дата:** 2026-05-31.

**Что усилено в коде:**

- `src/taksklad/skladbot.py`: адрес SkladBot больше не является блокирующим условием для desktop-синхронизации номеров заявок.
- `src/taksklad/skladbot.py`: тип заявки принимается гибко для вариантов `Отгрузка 3PL` и `3PL отгрузка`.
- `src/taksklad/geocoding.py`: адрес из Яндекс Геокодера очищается от страны `Узбекистан`.
- `backend/app/logistics_service.py`: логистический отчёт не формируется без координат и нормализует координаты до пары `lat,lon`.
- `backend/app/kiz_reports_service.py`: в КИЗ-отчёт по исходному файлу добавлен лист `Сводка` с суммой заказа, планом и фактом блоков.

**Проверка реальных Excel-файлов из Telegram:**

- `заказы 29.05 3 часть.xlsx`: 27 строк, 88 блоков, координаты есть, предупреждений 0.
- `заказы 29.05. 2 часть.xlsx`: 41 строка, 74 блока, координаты есть, предупреждений 0.
- `Шаблон_отправки_заказов_на_склад_26_05_2026_2ч.xlsx`: 21 строка, 78 блоков, координаты есть, предупреждений 0.
- `Шаблон_отправки_заказов_на_склад_26_05_2026_1ч.xlsx`: 13 строк, 24 блока, координаты есть, предупреждений 0.
- `Шаблон_отправки_заказов_на_склад_26_05_2026_1ч_терминал.xlsx`: 23 строки, 49 блоков, координаты есть, предупреждений 0.

**Проверки:**

- `.venv/bin/python -m unittest discover -s tests` - 79 тестов OK.
- `.venv/bin/python -m py_compile backend/app/*.py src/taksklad/*.py tests/*.py` - OK.
- `git diff --check` - OK.
- `npm run build` в `frontend/` - OK.
- `docker compose --env-file deploy/vds/.env.example -f deploy/vds/docker-compose.yml config` - OK.

**VDS smoke после деплоя:**

- VDS пересобран и поднят с обновлёнными `backend-api`, `telegram-worker`, `skladbot-worker`, `frontend`.
- Создан smoke-заказ `SMOKE_MVP_CHAPMAN_20260531_0154`: 2 позиции, 3 блока, координаты `41.214609,69.223027,15`.
- Логистический отчёт по `2026-05-31` отдал 2 строки с координатами `41.214609,69.223027`.
- Через API записаны 3 КИЗа.
- КИЗ-отчёт по исходному файлу сформирован, лист `Сводка` показал 3/3 блока и сумму `720000`.
- Cleanup-скрипт удалил smoke-данные: `orders=1`, `imports=1`, `audit_log=1`; после удаления остаток `0`.
- `https://api.135.181.245.84.sslip.io/health` вернул `200`.
- Все VDS-сервисы после smoke в состоянии `running`.

### Пост-Чек VDS После Финального Push

**Дата:** 2026-05-31.

**Проверено:**

- GitHub branch и checkpoint-тег обновлены до `bce4f8a`.
- `version.json`, Windows-архив и GitHub Release не трогались.
- `https://api.135.181.245.84.sslip.io/health` вернул `200`.
- VDS-сервисы `backend-api`, `frontend`, `postgres`, `skladbot-worker`, `telegram-worker` находятся в состоянии `running`.
- Dry-run cleanup по маркерам `ACCEPTANCE TELEGRAM 20260531` и `SMOKE_MVP_CHAPMAN_20260531_0154` показал нули по `orders/imports/import_files/pending_events/audit_log`.
- Свежие логи backend не содержат ошибок после smoke.
- `skladbot-worker` корректно пишет `no active backend orders, skip SkladBot API`.

**Что всё ещё не закрыто автоматикой:**

- Реальная отправка Excel-файла в Telegram-бота от разрешённого пользовательского аккаунта.
- Физическая Windows-приёмка desktop-приложения с backend flags.

### Повторяемый VDS Smoke-Скрипт

**Дата:** 2026-05-31.

**Сделано:**

- Добавлен `deploy/vds/smoke_mvp_chapman.sh`.
- Скрипт создаёт только тестовый заказ с маркером `SMOKE_MVP...`.
- Проверяет импорт, логистический отчёт, запрет досрочного завершения, сканы КИЗов, запрет дубля КИЗа, завершение заказа и КИЗ-сводку по исходному файлу.
- После проверки автоматически удаляет smoke-данные через `cleanup_acceptance_marker.sh`.

**Результат запуска на VDS:**

- Маркер: `SMOKE_MVP_CHAPMAN_20260530T210739Z`.
- Дата отгрузки: `2026-05-30`.
- Импортировано строк: `2`.
- Создано заказов: `1`.
- Логистический отчёт: `2` строки.
- Сканов КИЗ: `3`.
- Дубль КИЗа отклонён.
- Заказ завершён.
- КИЗ-сводка: сумма `720000`.
- Cleanup удалил: `orders=1`, `imports=1`, `audit_log=4`; после удаления остаток `0`.

**Проверки:**

- `bash -n deploy/vds/*.sh` - OK.
- `.venv/bin/python -m unittest discover -s tests` - 79 тестов OK.
- `docker compose --env-file deploy/vds/.env.example -f deploy/vds/docker-compose.yml config` - OK.

### Усиление Автотестов Desktop Backend Bridge

**Дата:** 2026-05-31.

**Зачем:**

Физическая Windows-приёмка всё ещё нужна, но часть риска можно проверить автоматикой: локальная очередь backend-событий должна защищать склад от дублей и временной недоступности backend.

**Что добавлено в `tests/test_backend_bridge.py`:**

- pending scan дедуплицируется;
- pending scan code попадает в список занятых КИЗов;
- отмена последнего КИЗа удаляет pending scan;
- pending `order_complete` отправляется в backend;
- неизвестное событие не держит очередь.

**Проверки:**

- `.venv/bin/python -m unittest tests.test_backend_bridge` - 7 тестов OK.
- `.venv/bin/python -m unittest discover -s tests` - 83 теста OK.
- `.venv/bin/python -m py_compile src/taksklad/*.py tests/*.py backend/app/*.py` - OK.
- `git diff --check` - OK.

### Read-Only Acceptance Verifier

**Дата:** 2026-05-31.

**Сделано:**

- Добавлен `deploy/vds/verify_acceptance_marker.sh`.
- Скрипт ничего не удаляет и ничего не меняет в базе.
- По безопасному маркеру показывает `orders`, `items`, `planned_blocks`, `scanned_blocks`, `scan_codes`, `imports`, `pending_events`, `source_files`, `order_dates`, `missing_coordinates`, `incomplete_items`.
- Поддерживает проверки:
  - `--expect-orders N`;
  - `--expect-scans N`;
  - `--expect-completed`.
- Встроен в `deploy/vds/smoke_mvp_chapman.sh` перед cleanup.

**Проверки на VDS:**

- `verify_acceptance_marker.sh "ACCEPTANCE TELEGRAM 20260531"` вернул `status=ok` и нули по текущему пустому acceptance-маркеру.
- Smoke `SMOKE_MVP_CHAPMAN_20260530T211424Z` перед cleanup показал:
  - `orders=1`;
  - `imports=1`;
  - `items=2`;
  - `planned_blocks=3`;
  - `scanned_blocks=3`;
  - `scan_codes=3`;
  - `completed_orders=1`;
  - `active_orders=0`;
  - `status=ok`.
- Cleanup после smoke удалил тестовые строки, остаток `0`.

### Генератор Acceptance Excel

**Дата:** 2026-05-31.

**Сделано:**

- Добавлен `tools/generate_acceptance_excel.py`.
- Добавлен тест `tests/test_acceptance_excel_generator.py`.
- Тестовый файл `outputs/taksklad_acceptance/TakSklad_Telegram_Acceptance_2026-05-31.xlsx` пересобран этим генератором.

**Что генерируется:**

- клиент `ACCEPTANCE TELEGRAM 20260531`;
- дата отгрузки `31.05.2026`;
- 2 позиции;
- 3 блока;
- координаты `41.311081, 69.240562`;
- сумма `720000`.

**Проверки:**

- Генератор создал временный `.xlsx`.
- Backend parser прочитал `2` строки, `3` блока, сумму `720000`, warnings `[]`.
- `.venv/bin/python -m unittest tests.test_acceptance_excel_generator` - OK.
- `.venv/bin/python -m unittest discover -s tests` - 84 теста OK.
- `.venv/bin/python -m py_compile tools/*.py src/taksklad/*.py tests/*.py backend/app/*.py` - OK.

### Windows Backend Acceptance Helper

**Дата:** 2026-05-31.

**Сделано:**

- Добавлен `tools/windows_backend_acceptance.ps1`.
- Helper проверяет VDS backend перед запуском Windows-приложения:
  - `GET /health`;
  - `GET /api/v1/orders/active` с service token.
- Helper включает backend flags только для текущего PowerShell-процесса и дочернего запуска `TakSklad.exe` или `main.py`.
- Token не сохраняется в git, файл, реестр или документацию.
- Добавлен `-CheckOnly` для проверки VDS без запуска приложения.
- Добавлен `-Clear` для быстрого удаления backend env из текущего PowerShell-процесса.

**Зачем:**

Физическая Windows-приёмка всё ещё нужна, но теперь запуск тестовой копии будет повторяемым: меньше ручных env-команд, меньше риск забыть флаг или случайно оставить backend token в открытом терминале.

**Проверки:**

- Добавлен тест `tests/test_windows_acceptance_helper.py`.
- `tests.test_windows_acceptance_helper` - 2 теста OK.
- `.venv/bin/python -m unittest discover -s tests` - 86 тестов OK.
- `.venv/bin/python -m py_compile tools/*.py src/taksklad/*.py tests/*.py backend/app/*.py` - OK.
- `git diff --check` - OK.
- PowerShell runtime `pwsh` в текущей macOS-среде не установлен, поэтому сам `.ps1` не исполнялся локально. Финальная проверка helper должна пройти на Windows.

### Acceptance Kit Для Telegram И Windows Проверки

**Дата:** 2026-05-31.

**Сделано:**

- Добавлен `tools/prepare_acceptance_kit.py`.
- Acceptance kit лежит в `outputs/taksklad_acceptance/`:
  - `TakSklad_Telegram_Acceptance_2026-05-31.xlsx`;
  - `acceptance_manifest.json`;
  - `README.md`.
- Manifest содержит marker, дату отгрузки, ожидаемые заказы/строки/позиции/блоки/сумму, test-КИЗы, SHA-256 Excel и команды Telegram/Windows/VDS verification.
- Safety-флаги в manifest фиксируют: без `version.json`, без release archive, без GitHub Release, без push-уведомлений и без создания реальной заявки SkladBot.
- Acceptance Excel теперь нормализуется как `.xlsx` ZIP-архив, чтобы SHA-256 был стабильным между повторными генерациями.

**Проверки:**

- `.venv/bin/python tools/prepare_acceptance_kit.py` - OK.
- Повторная генерация дала тот же SHA-256 Excel: `a5abc62efebcd2d87e26e92dfbb990d22fbf72e86ae74914b0dbf9b6f8de285e`.
- `tests.test_acceptance_excel_generator` - 3 теста OK.
- `.venv/bin/python -m unittest discover -s tests` - 88 тестов OK.
- `.venv/bin/python -m py_compile tools/*.py src/taksklad/*.py tests/*.py backend/app/*.py` - OK.

### Wait Acceptance Verifier

**Дата:** 2026-05-31.

**Сделано:**

- Добавлен `deploy/vds/wait_acceptance_marker.sh`.
- Скрипт в цикле запускает read-only `verify_acceptance_marker.sh`.
- Используется для двух оставшихся ручных гейтов:
  - дождаться появления заказа после Telegram import;
  - дождаться 3 сканов и completed-статуса после Windows acceptance.
- Скрипт не пишет в БД и не удаляет тестовые данные.
- Команды ожидания добавлены в `outputs/taksklad_acceptance/README.md` и `acceptance_manifest.json`.

**Проверки:**

- `bash -n deploy/vds/*.sh` - OK.
- `deploy/vds/wait_acceptance_marker.sh --help` - OK.
- Небезопасный marker `BAD_MARKER` отклонён сразу, без ожидания timeout.
- `tests.test_acceptance_excel_generator` проверяет наличие `telegram_wait` и `windows_wait` в manifest.

### VDS Acceptance Kit Sync

**Дата:** 2026-05-31.

**Сделано:**

- На VDS в `/opt/taksklad/app` загружены только acceptance-файлы и документация:
  - `deploy/vds/wait_acceptance_marker.sh`;
  - `deploy/vds/verify_acceptance_marker.sh`;
  - `deploy/vds/cleanup_acceptance_marker.sh`;
  - `outputs/taksklad_acceptance/*`;
  - `tools/prepare_acceptance_kit.py`;
  - `tools/generate_acceptance_excel.py`;
  - runbook/audit/report docs.
- `.env`, Postgres, контейнеры и `version.json` не менялись.
- VDS рабочая копия не является git checkout, поэтому обновление сделано точечным `rsync`.

**Проверки на VDS:**

- `bash -n deploy/vds/*.sh` - OK.
- `deploy/vds/wait_acceptance_marker.sh --help` - OK.
- Небезопасный marker `BAD_MARKER` отклонён с exit `2`.
- `wait_acceptance_marker.sh "ACCEPTANCE TELEGRAM 20260531" --timeout 5 --interval 1` - OK, текущий marker пустой и read-only verifier вернул `status=ok`.
- `verify_acceptance_marker.sh "ACCEPTANCE TELEGRAM 20260531"` - OK, текущие `orders/imports/scan_codes/pending_events` равны `0`.
- Excel SHA-256 на VDS: `a5abc62efebcd2d87e26e92dfbb990d22fbf72e86ae74914b0dbf9b6f8de285e`.
- Backend health: `{"status":"ok","service":"taksklad-backend","version":"0.1.0","environment":"staging"}`.
- VDS `version.json` остался на стабильной линии `1.1.7`, без release/update rollout.

### Acceptance Status Check

**Дата:** 2026-05-31.

**Сделано:**

- Добавлен `deploy/vds/acceptance_status.sh`.
- Скрипт read-only, ничего не пишет в БД и не меняет файлы.
- Проверяет одним запуском:
  - валидность `acceptance_manifest.json`;
  - SHA-256 acceptance Excel;
  - `version.json`;
  - Docker Compose services;
  - backend health;
  - состояние acceptance marker через `verify_acceptance_marker.sh`.
- Команды добавлены в acceptance kit:
  - `vds_status`;
  - `telegram_status`;
  - `windows_status`.

**Проверки:**

- `bash -n deploy/vds/*.sh` - OK.
- `deploy/vds/acceptance_status.sh --help` - OK.
- `tests.test_acceptance_excel_generator` проверяет наличие status-команд в manifest.

**Проверки на VDS после загрузки:**

- `bash -n deploy/vds/*.sh` - OK.
- `acceptance_status.sh --help` - OK.
- Acceptance Excel SHA-256 совпал с manifest: `a5abc62efebcd2d87e26e92dfbb990d22fbf72e86ae74914b0dbf9b6f8de285e`.
- `acceptance_status.sh` вернул `status=ok`.
- Сервисы `backend-api`, `frontend`, `postgres`, `skladbot-worker`, `telegram-worker` в состоянии `running`.
- Backend health вернул `status=ok`.
- Acceptance marker пока пустой: `orders=0`, `imports=0`, `scan_codes=0`, `pending_events=0`.
- VDS `version.json`: `latest_version=1.1.7`, `mandatory=false`, download URL пустой.
- Был один временный SSH timeout сразу после `rsync`; повторная SSH-проверка прошла успешно, backend по HTTPS всё время отвечал `ok`.

### TakSklad 2.0 Workflow/UI Alignment

**Дата:** 2026-05-31.

**Сделано:**

- Desktop UI приведён ближе к утверждённому рабочему экрану склада:
  - список переименован в `Заказы для КИЗов`;
  - заказы визуально разделяются по датам отгрузки;
  - убраны видимые кнопки `Импорт Excel`, `Товары`, `Контроль` с основного складского экрана;
  - добавлена отдельная кнопка `Возвраты`;
  - кнопка финального отчёта переименована в `Закрыть смену`;
  - кнопки переведены на округлённый canvas-вид и палитру TakSklad (`#F0E68C` + чёрный).
- Печать осталась только в сценарии завершения заказа: отдельной кнопки печати на рабочем экране склада нет.
- Возвраты добавлены в backend/Desktop MVP:
  - поиск закрытой заявки по номеру/ID SkladBot или external id;
  - фиксация статуса `returned`;
  - запись даты возврата и audit log;
  - returned-заказы исключаются из активного списка.
- `Закрыть смену` теперь формирует КИЗ-отчёты по датам отгрузки:
  - если за смену закрыты разные даты, формируется несколько файлов;
  - повторное закрытие по той же дате получает `ч1`, `ч2` и так далее;
  - каждый файл отправляется в Telegram.
- Старый автоматический таймер дневного отчёта в desktop больше не запускается. Отчёт КИЗов уходит при закрытии смены.
- Telegram worker оставлен только с пользовательским нижним меню:
  - `Дата отгрузки`;
  - `Отчёт логистики`;
  - `КИЗ по файлам`.
- Старый Telegram `/report` убран из пользовательского workflow. `/health` и `/imports` оставлены как скрытый админский fallback.
- SkladBot matching исправлен:
  - окно `сегодня/вчера` применяется к дате создания/обновления заявки;
  - `Дата выгрузки` больше не используется как фильтр свежести и остаётся строгим полем совпадения с датой отгрузки заказа;
  - если в list response нет поля `type`, заявка не отбрасывается до загрузки detail, потому что `type_id` уже сужает выборку.
- Интервал SkladBot worker выставлен на 60 секунд для более быстрого подтягивания номеров заявок.
- Проверены реальные Excel-шаблоны из Telegram:
  - `заказы 29.05 3 часть.xlsx`;
  - `заказы 29.05. 2 часть.xlsx`;
  - `Шаблон_отправки_заказов_на_склад_26_05_2026_1ч.xlsx`;
  - `Шаблон_отправки_заказов_на_склад_26_05_2026_1ч_терминал.xlsx`;
  - `Шаблон_отправки_заказов_на_склад_26_05_2026_2ч.xlsx`.
- `api.taksklad.uz` переключён на VDS:
  - DNS резолвится в `135.181.245.84`;
  - `https://api.taksklad.uz/health` возвращает `ok`;
  - VDS `.env` обновлён через `switch_backend_host.sh`;
  - `version.json` не менялся.
- На VDS загружены backend/deploy-изменения и пересобраны:
  - `backend-api`;
  - `telegram-worker`;
  - `skladbot-worker`.
- Добавлена read-only диагностика SkladBot matching:
  - `backend/app/skladbot_diagnostic.py`;
  - `deploy/vds/diagnose_skladbot_match.sh`;
  - показывает ближайшие SkladBot-заявки и причины несовпадения `date/client/payment/products`.

**Проверки:**

- Локально:
  - `tests.test_backend_skladbot_worker`;
  - `tests.test_backend_telegram_import`;
  - `tests.test_daily_report`;
  - `tests.test_backend_api_persistence`;
  - всего 29 тестов OK в targeted run.
- Реальные шаблоны Excel разобраны: строки, даты, блоки, суммы и типы оплаты определяются.
- VDS:
  - `curl https://api.taksklad.uz/health` - OK;
  - `acceptance_status.sh` - `status=ok`;
  - Telegram API `getMyCommands` вернул только `date`, `logistics`, `kiz_files`;
  - `diagnose_skladbot_match.sh --help` - OK;
  - `smoke_mvp_chapman.sh` - OK: import 2 rows, 3 scans, duplicate rejected, order completed, logistics rows 2, KIZ summary total 720000, cleanup выполнен.

**Что ещё не закрыто до релиза 2.0:**

- Реальный Telegram upload test через боевой чат на копии рабочего Excel.
- Проверка SkladBot matching на живой активной заявке `3PL отгрузка`.
- Ручная Windows-приёмка desktop с backend flags, печатью и сканером.
- Сборка Windows archive и обновление `version.json` только после приёмки.

### SkladBot Diagnostic Limit

- Read-only диагностика SkladBot matching дополнительно ограничена:
  - если по маркеру нет активных backend-заказов, она не обращается к SkladBot API;
  - добавлен параметр `--request-limit`, чтобы acceptance-проверка не проходила по большому списку заявок;
  - команда в runbook обновлена до `--limit 5 --request-limit 20`.
- Правка загружена на VDS и проверена:
  - `https://api.taksklad.uz/health` - OK;
  - `acceptance_status.sh` - `status=ok`;
  - `diagnose_skladbot_match.sh --marker "ACCEPTANCE TELEGRAM 20260531" --limit 5 --request-limit 20` вернул `active_orders=0`, `candidate_requests=0`;
  - зависших процессов диагностики на VDS не осталось.

### Desktop Print Window Sizes

- Окно печати сводного листа обновлено:
  - показывает доступные системные принтеры, если ОС отдаёт список;
  - поддерживает размеры этикеток `100x100`, `100x150`, `75x50`, `58x40`;
  - сохраняет выбранный принтер и размер;
  - `Enter` подтверждает печать, `Esc` отменяет.
- Печать остаётся прямой через ОС: браузер для сводного листа не открывается.
- Добавлен тест `tests.test_printing`: проверяет парсер размеров и фактический размер PNG для выбранной этикетки.

### Backend Diagnostics Logs

- Добавлен endpoint `GET /api/v1/diagnostics/logs`.
- Endpoint формирует текстовый diagnostic-файл:
  - failed/error события очередей;
  - импорты со статусами `failed` и `completed_with_errors`;
  - последние служебные audit-события `orders_imported`, `skladbot_worker_sync`, `order_returned`.
- Обычные события сканирования КИЗов не попадают в файл, чтобы не засорять его складскими дублями и кодами.
- Очевидные токены/секреты в тексте маскируются.
- В Telegram добавлена скрытая команда `/logs`, которая отправляет этот файл. Нижнее пользовательское меню не изменилось.
- Покрыто тестами:
  - `test_diagnostics_logs_include_failed_events_import_errors_and_redact_secrets`;
  - `test_telegram_worker_handles_hidden_logs_command`.
- Проверено:
  - локально `97` тестов OK;
  - `py_compile` OK;
  - `bash -n deploy/vds/*.sh` и `git diff --check` OK;
  - VDS пересобран с `backend-api`, `telegram-worker`, `skladbot-worker`;
  - `https://api.taksklad.uz/health` - OK;
  - `acceptance_status.sh` - `status=ok`;
  - `/api/v1/diagnostics/logs` на VDS вернул `200` и файл `TakSklad_backend_diagnostics_*.txt`.

### Yandex Geocoder Secret Cleanup

- Ключ Яндекс Геокодера удалён из `src/taksklad/config.py`.
- `src/taksklad/geocoding.py` теперь читает ключ только из:
  - env `YANDEX_GEOCODER_API_KEY`;
  - локального `yandex_geocoder_key.txt`.
- Если ключ не настроен, импорт не падает: строка получает предупреждение `не указан ключ Яндекс Геокодера`, как и раньше при недоступном геокодинге.
- `yandex_geocoder_key.txt` уже находится в `.gitignore`.
- Добавлены регрессионные тесты `tests/test_geocoding.py` на env/file/missing-key.
- Проверено:
  - `tests.test_geocoding` - 3 теста OK;
  - полный `unittest discover -s tests` - 100 тестов OK;
  - `py_compile` для изменённых модулей - OK;
  - `bash -n deploy/vds/*.sh` и `git diff --check` - OK.
- Старый ключ нужно ротировать отдельно перед боевым релизом.

### Desktop Log Rotation

- Desktop logging вынесен в `src/taksklad/logging_setup.py`.
- `docs/TakSklad.log` теперь пишется через `RotatingFileHandler`.
- Дефолтная политика:
  - основной файл до `5 MB`;
  - до `5` архивных файлов.
- Добавлены env-настройки:
  - `TAKSKLAD_LOG_MAX_BYTES`;
  - `TAKSKLAD_LOG_BACKUP_COUNT`.
- Добавлены тесты `tests/test_logging_setup.py`:
  - повторная настройка не добавляет второй handler на тот же файл;
  - большой лог реально ротируется.
- Проверено:
  - `tests.test_logging_setup tests.test_geocoding` - 5 тестов OK;
  - полный `unittest discover -s tests` - 102 теста OK;
  - `py_compile` - OK;
  - `bash -n deploy/vds/*.sh` и `git diff --check` - OK.

### Desktop Startup Self-Check

- Добавлен `src/taksklad/startup_check.py`.
- При запуске desktop пишет в лог строку `Startup self-check`.
- В self-check попадают:
  - версия;
  - frozen/dev режим;
  - hash `SPREADSHEET_ID`;
  - `SHEET_NAME`;
  - источник credentials: `stored`, `file`, `missing`;
  - наличие `TakSklad_data.json`;
  - Telegram enabled/token/chat count;
  - backend flags, backend origin и наличие backend token;
  - наличие ключа Яндекс Геокодера;
  - размеры локальных очередей.
- Секреты, chat_id, token, private key, КИЗы и сам spreadsheet id в лог не выводятся.
- Добавлены тесты `tests/test_startup_check.py` на redaction и fallback credentials из файла.
- Проверено:
  - `tests.test_startup_check tests.test_logging_setup tests.test_geocoding` - 7 тестов OK;
  - полный `unittest discover -s tests` - 104 теста OK;
  - `py_compile` - OK;
  - `bash -n deploy/vds/*.sh` и `git diff --check` - OK.

### Desktop Refresh Diagnostic Summary

- Добавлен `src/taksklad/desktop_diagnostics.py`.
- После успешной загрузки списка заказов desktop пишет `Refresh diagnostic summary`.
- Summary включает только счётчики:
  - источник списка `google/backend`;
  - строки, группы, даты отгрузки;
  - известные КИЗы;
  - очереди `pending_saves`, `pending_prints`, `pending_backend_events`, `pending_telegram`;
  - итоги `sync_pending_saves`;
  - итоги backend queue;
  - итоги SkladBot matching.
- Клиенты, адреса, товары, КИЗы и payload очередей в summary не выводятся.
- Добавлен тест `tests/test_desktop_diagnostics.py` на счётчики и redaction.
- Проверено:
  - `tests.test_desktop_diagnostics tests.test_startup_check tests.test_logging_setup tests.test_geocoding` - 8 тестов OK;
  - полный `unittest discover -s tests` - 105 тестов OK;
  - `py_compile` - OK;
  - `bash -n deploy/vds/*.sh` и `git diff --check` - OK.

### Windows Acceptance Helper DNS/Version Guard

- Обновлён `tools/windows_backend_acceptance.ps1`.
- Основной backend URL теперь `https://api.taksklad.uz`, а не временный `sslip.io`.
- Добавлен запуск исходников через `-UsePython`, чтобы при наличии рядом `TakSklad.exe` можно было принудительно открыть текущий код.
- При запуске `main.py` helper проверяет `APP_VERSION` не ниже `1.1.17`.
- Для исходников helper предпочитает `.venv\Scripts\python.exe`, если виртуальное окружение есть.
- Для exe добавлено предупреждение: версию внутри exe helper надёжно не проверяет, поэтому нельзя брать старый ярлык `1.1.7`.
- Обновлены:
  - `docs/windows-backend-acceptance.md`;
  - `docs/manual-acceptance-runbook.md`;
  - `docs/deploy-rollback-runbook.md`;
  - `tools/prepare_acceptance_kit.py`;
  - acceptance kit README/manifest.
- Проверено:
  - `tests.test_windows_acceptance_helper tests.test_acceptance_excel_generator` - 5 тестов OK;
  - полный `unittest discover -s tests` - 105 тестов OK;
  - `py_compile` для `main.py`, `src/taksklad/*.py`, `backend/app/*.py`, `tools/*.py` - OK;
  - `bash -n deploy/vds/*.sh` и `git diff --check` - OK;
  - `version.json` валиден и не изменён.
- Ограничение: PowerShell parser локально не проверен, потому что `pwsh` на macOS не установлен. Синтаксис нужно дополнительно проверить на Windows или в среде с PowerShell.

### Windows Test Archive Helper

- Добавлен `tools/build_windows_test_archive.ps1`.
- Назначение: собрать свежую тестовую Windows-сборку для приёмки 2.0 без GitHub Release, без изменения публичного `version.json` и без push-уведомлений.
- Helper:
  - проверяет `APP_VERSION` и минимальную версию `1.1.17`;
  - проверяет, что `version.json` не имеет локальных изменений;
  - по умолчанию требует, чтобы `version.json` был закреплён на стабильной `1.1.7`;
  - опционально устанавливает зависимости через `-InstallDependencies`;
  - запускает тесты, если не передан `-SkipTests`;
  - собирает PyInstaller `--onedir`;
  - добавляет в пакет `windows_backend_acceptance.ps1` и acceptance kit;
  - копирует содержимое PyInstaller-папки в `TakSklad\` и проверяет наличие `TakSklad.exe`;
  - проверяет, что в test package не попали локальные runtime/secret-файлы: `TakSklad_data.json`, `credentials.json`, `telegram_settings.json`, `yandex_geocoder_key.txt`, `pending_*.json`;
  - пишет `build_manifest.json`, `README_TEST_BUILD.md`, ZIP и SHA256.
- Обновлены:
  - `docs/windows-backend-acceptance.md`;
  - `docs/manual-acceptance-runbook.md`;
  - `docs/product-mvp-2.0-plan.md`;
  - `docs/vds-release-readiness.md`;
  - `tools/prepare_acceptance_kit.py`;
  - acceptance kit README/manifest.
- Проверено:
  - `tests.test_windows_test_build_helper tests.test_acceptance_excel_generator tests.test_windows_acceptance_helper` - 7 тестов OK;
  - полный `unittest discover -s tests` - 107 тестов OK;
  - `py_compile` для `main.py`, `src/taksklad/*.py`, `backend/app/*.py`, `tools/*.py` - OK;
  - `bash -n deploy/vds/*.sh` и `git diff --check` - OK;
  - `version.json` не изменён.
- Ограничение: сам Windows archive локально не собран, потому что текущая среда macOS. Helper нужно запускать на Windows.

### Local Release Preflight

- Добавлен `tools/release_preflight.py`.
- Назначение: перед ручной приёмкой одной локальной командой проверить, что проект находится в безопасном состоянии для acceptance.
- Проверяет:
  - обязательные helper/runbook-файлы;
  - `version.json`: закреплён на `1.1.7`, `mandatory=false`, download URL пустые, git diff отсутствует;
  - acceptance kit: manifest, Excel, SHA256, marker `ACCEPTANCE`, safety-флаги;
  - tracked runtime/secret-файлы в Git;
  - публичный backend health `https://api.taksklad.uz/health`.
- Поддерживает `--skip-network` для локального теста без сетевого запроса.
- Добавлены тесты `tests/test_release_preflight.py`.
- Проверено:
  - `tests.test_release_preflight tests.test_acceptance_excel_generator` - 7 тестов OK;
  - `.venv/bin/python tools/release_preflight.py` - `status=ok`, публичный `api.taksklad.uz/health` ответил `status=ok`;
  - `py_compile` для `tools/release_preflight.py` - OK.
  - полный `unittest discover -s tests` - 111 тестов OK;
  - `py_compile` для `main.py`, `src/taksklad/*.py`, `backend/app/*.py`, `tools/*.py` - OK;
  - `bash -n deploy/vds/*.sh` и `git diff --check` - OK;
  - `version.json` не изменён.
- Обновлены:
  - `tools/prepare_acceptance_kit.py`;
  - `outputs/taksklad_acceptance/README.md`;
  - `outputs/taksklad_acceptance/acceptance_manifest.json`;
  - `docs/manual-acceptance-runbook.md`;
  - `docs/vds-release-readiness.md`;
  - `docs/product-mvp-2.0-plan.md`.

### Acceptance Result Template

- В acceptance kit добавлен `ACCEPTANCE_RESULTS_TEMPLATE.md`.
- Шаблон фиксирует:
  - preflight;
  - Telegram import;
  - SkladBot matching;
  - Windows desktop acceptance;
  - cleanup;
  - defects/known issues;
  - итоговое решение `GO/NO-GO`.
- `tools/release_preflight.py` теперь проверяет наличие result template.
- Обновлены `docs/manual-acceptance-runbook.md`, `docs/vds-release-readiness.md`, `docs/product-mvp-2.0-plan.md`.
- Проверено после добавления шаблона:
  - `tests.test_release_preflight tests.test_acceptance_excel_generator` - 8 тестов OK;
  - `.venv/bin/python tools/release_preflight.py --skip-network` - `status=ok`;
  - `.venv/bin/python tools/release_preflight.py` - `status=ok`, публичный `https://api.taksklad.uz/health` ответил `status=ok`;
  - полный `unittest discover -s tests` - 112 тестов OK;
  - `py_compile` для `main.py`, `src/taksklad/*.py`, `backend/app/*.py`, `tools/*.py` - OK;
  - `bash -n deploy/vds/*.sh` и `git diff --check` - OK;
  - `version.json` не изменён и остаётся закреплён на стабильной `1.1.7`.

### Logistics Report Blocks And Prices

- Проверен реальный шаблон логистики `Список_заказов_на_доставку_Чапамана_на_29_05_2026.xlsx`.
- Зафиксирован риск: колонка `Кол-во` в логистическом файле должна отражать блоки, а не пачки/штуки.
- Исправлен `backend/app/logistics_service.py`:
  - `Кол-во` теперь заполняется из `quantity_blocks`;
  - `Цена` теперь заполняется ценой за блок;
  - если цена за блок не пришла из импорта, используется `240000`;
  - `Цена заказа` остаётся общей суммой позиции.
- Это закрывает кейс Smartup: `200` пачек в импорте превращаются в `20` блоков в логистике, цена становится `240000`, сумма `4800000`.
- Проверено:
  - `tests.test_backend_api_persistence.BackendApiPersistenceTests.test_logistics_report_uses_shipment_date_coordinates_and_prices` - OK;
  - `tests.test_backend_api_persistence.BackendApiPersistenceTests.test_logistics_report_requires_coordinates` - OK;
  - `tests.test_backend_api_persistence.BackendApiPersistenceTests.test_logistics_report_normalizes_three_part_coordinates` - OK;
  - полный `unittest discover -s tests` - 112 тестов OK;
  - `py_compile` для `main.py`, `src/taksklad/*.py`, `backend/app/*.py`, `tools/*.py` - OK;
  - `.venv/bin/python tools/release_preflight.py` - `status=ok`;
  - `bash -n deploy/vds/*.sh` и `git diff --check` - OK;
  - `version.json` не изменён.

### Returns List In Backend And Desktop

- Закрыт локальный разрыв по возвратам: после отметки возврата не было отдельного списка уже принятых возвратов.
- Добавлен backend endpoint `GET /api/v1/returns`.
- В `OrderRead` добавлены безопасные поля возврата:
  - `return_status`;
  - `returned_at`;
  - `return_reference`.
- Окно `Возвраты` в desktop теперь показывает блок `Последние возвраты`.
- После успешного `Принять возврат` список обновляется сразу.
- Возврат по-прежнему ищется только среди закрытых/архивных заказов по номеру или ID заявки SkladBot.
- Проверено:
  - `tests.test_backend_api_persistence.BackendApiPersistenceTests.test_return_lookup_and_mark_returned_excludes_order_from_active_list` - OK;
  - полный `unittest discover -s tests` - 112 тестов OK;
  - `py_compile` для `main.py`, `src/taksklad/*.py`, `backend/app/*.py`, `tools/*.py` - OK;
  - `.venv/bin/python tools/release_preflight.py` - `status=ok`;
  - `bash -n deploy/vds/*.sh` и `git diff --check` - OK;
  - `version.json` не изменён.

### Return Duplicate Guard And Acceptance Checklist

- Закрыт риск повторного принятия одного и того же возврата.
- Backend `POST /api/v1/returns/{order_id}` теперь возвращает `409`, если заказ уже в статусе `returned` или `return_status=returned`.
- Desktop при поиске уже возвращённой заявки показывает, что возврат уже принят, и блокирует кнопку `Принять возврат`.
- Acceptance result template дополнен проверками возвратов:
  - открыть окно `Возвраты`;
  - найти завершённую заявку по ШК/номеру;
  - принять возврат;
  - увидеть его в `Последние возвраты`;
  - убедиться, что повторное принятие запрещено.
- Acceptance kit пересобран через `tools/prepare_acceptance_kit.py`.
- Проверено:
  - `tests.test_backend_api_persistence.BackendApiPersistenceTests.test_return_lookup_and_mark_returned_excludes_order_from_active_list` - OK;
  - `tests.test_acceptance_excel_generator` - OK;
  - полный `unittest discover -s tests` - 112 тестов OK;
  - `py_compile` для `main.py`, `src/taksklad/*.py`, `backend/app/*.py`, `tools/*.py` - OK;
  - `.venv/bin/python tools/release_preflight.py` - `status=ok`, acceptance kit SHA обновлён и совпадает с manifest;
  - `bash -n deploy/vds/*.sh` и `git diff --check` - OK;
  - `version.json` не изменён.

### SkladBot Vendor Code Product Matching

- Закрыт риск SkladBot matching по товарам, если SkladBot отдаёт товар как vendor code без пробелов, например `CHPMBrownOP20UZ`.
- Backend worker и desktop fallback теперь извлекают Chapman SKU не только из токенов, но и из compact-строки:
  - цвет `brown`, `red`, `gold`;
  - формат `OP`, `SSL`.
- Это сохраняет строгую бизнес-логику: совпадение всё равно идёт по цвету, формату и блокам, но не ломается из-за отсутствия пробелов/тире в коде.
- Добавлены регрессионные проверки:
  - `Chapman Brown OP 20` совпадает с `CHPMBrownOP20UZ`;
  - `Chapman Gold SSL 100\`20` совпадает с `CHPMGoldSSL20UZ`;
  - `Brown OP` не совпадает с `Red OP`.
- Проверено:
  - `tests.test_backend_skladbot_worker` - OK;
  - `tests.test_skladbot_sync.SkladBotSyncTests.test_product_match_accepts_concatenated_vendor_code` - OK;
  - полный `unittest discover -s tests` - 114 тестов OK;
  - `py_compile` для `main.py`, `src/taksklad/*.py`, `backend/app/*.py`, `tools/*.py` - OK;
  - `.venv/bin/python tools/release_preflight.py` - `status=ok`;
  - `bash -n deploy/vds/*.sh` и `git diff --check` - OK;
  - `version.json` не изменён.

### Desktop UI Contract Guard

- После проверки локального запуска зафиксирован риск: можно случайно запускать старую рабочую линию `1.1.7` и принять её за текущий dev-интерфейс.
- Добавлен тест `tests/test_desktop_ui_contract.py`, который защищает основной складской экран 2.0:
  - на главном экране должны быть `Заказы для КИЗов`, `Возвраты`, `Текущая позиция`, `Сканирование кода`, `Завершить заказ`, `Закрыть смену`;
  - старые складские кнопки `Импорт Excel`, `Товары`, `Контроль` не должны возвращаться на главный экран;
  - палитра TakSklad закреплена вокруг `#F0E68C` и чёрного;
  - `AppButton` должен оставаться округлённым.
- Проверено:
  - `.venv/bin/python -m unittest tests.test_desktop_ui_contract` - 3 теста OK.
  - `.venv/bin/python -m unittest discover -s tests` - 117 тестов OK.
  - `git diff --check` - OK.
  - `version.json` не изменён.

### Windows Acceptance Old Exe Guard

- Усилен Windows acceptance helper, чтобы не повторить ситуацию, когда вместо текущей тестовой линии запускается старый рабочий `TakSklad.exe` `1.1.7`.
- `tools/windows_backend_acceptance.ps1` теперь:
  - при запуске из исходников по-прежнему проверяет `APP_VERSION` не ниже `1.1.17`;
  - при запуске `.exe` ищет `build_manifest.json` рядом с test archive;
  - сверяет `app_version` из manifest с ожидаемой версией;
  - останавливает запуск exe без manifest, если явно не передан `-SkipAppVersionCheck`.
- `tools/build_windows_test_archive.ps1` теперь кладёт в test archive `ACCEPTANCE_RESULTS_TEMPLATE.md`, чтобы результаты Windows/Telegram/SkladBot приёмки можно было заполнить прямо из комплекта.
- Обновлены инструкции:
  - `docs/windows-backend-acceptance.md`;
  - `docs/manual-acceptance-runbook.md`.
- Проверено:
  - `.venv/bin/python -m unittest tests.test_windows_acceptance_helper tests.test_windows_test_build_helper` - 4 теста OK.
  - `.venv/bin/python -m unittest discover -s tests` - 117 тестов OK.
  - `.venv/bin/python tools/release_preflight.py` - `status=ok`.
  - `bash -n deploy/vds/*.sh` и `git diff --check` - OK.
  - `version.json` не изменён.

### Release Preflight Windows Acceptance Guard

- `tools/release_preflight.py` расширен проверкой `windows_acceptance_flow`.
- Preflight теперь перед ручной приёмкой проверяет не только `version.json`, backend health и acceptance kit, но и то, что:
  - `windows_backend_acceptance.ps1` содержит guard по `build_manifest.json`;
  - helper умеет остановить exe без проверяемого manifest;
  - `build_windows_test_archive.ps1` кладёт `ACCEPTANCE_RESULTS_TEMPLATE.md`;
  - test archive build по-прежнему проверяет, что `version.json` закреплён на стабильной `1.1.7`;
  - package не должен содержать runtime/secret-файлы.
- Acceptance kit пересобран через `tools/prepare_acceptance_kit.py`; README теперь описывает проверку exe через `build_manifest.json`.
- Исправлена повторяемость acceptance Excel: `tools/generate_acceptance_excel.py` теперь фиксирует `docProps/core.xml` modified timestamp, поэтому повторная генерация `.xlsx` даёт одинаковые байты и SHA.
- Проверено:
  - `.venv/bin/python -m unittest tests.test_release_preflight tests.test_acceptance_excel_generator` - 11 тестов OK.
  - текущий SHA-256 acceptance Excel: `204b932a704b39294b513a95964844db1ed74d028e3daff13beef3ab09ec98fd`.
  - `.venv/bin/python -m unittest discover -s tests` - 120 тестов OK.
  - `.venv/bin/python tools/release_preflight.py` - `status=ok`, включая `windows_acceptance_flow`.
  - `bash -n deploy/vds/*.sh` и `git diff --check` - OK.
  - `version.json` не изменён.

### VDS Acceptance Status Rollout Guard

- Усилен `deploy/vds/acceptance_status.sh`, чтобы серверная read-only проверка была такой же строгой, как локальный preflight.
- Теперь VDS status дополнительно:
  - проверяет наличие `result_template` из acceptance manifest;
  - падает, если `version.json` уже не закреплён на `1.1.7`;
  - падает, если `mandatory=true` или заполнены download URL до приёмки;
  - проверяет safety-флаги manifest: без изменения `version.json`, без GitHub Release, без push-уведомлений и без секретов.
- Добавлен тест `tests/test_vds_acceptance_scripts.py`:
  - защищает rollout guards в `acceptance_status.sh`;
  - проверяет, что verifier/cleanup scripts по-прежнему отказываются работать с небезопасным marker.
- Проверено:
  - `.venv/bin/python -m unittest tests.test_vds_acceptance_scripts` - 2 теста OK.
  - `.venv/bin/python -m unittest discover -s tests` - 122 теста OK.
  - `.venv/bin/python tools/release_preflight.py` - `status=ok`.
  - `bash -n deploy/vds/*.sh` и `git diff --check` - OK.
  - `version.json` не изменён.

### VDS Acceptance Kit Sync After Rollout Guard

- На VDS синхронизированы только read-only acceptance/preflight файлы, без rebuild и без рестарта контейнеров:
  - `deploy/vds/acceptance_status.sh`;
  - `deploy/vds/verify_acceptance_marker.sh`;
  - `deploy/vds/wait_acceptance_marker.sh`;
  - `deploy/vds/cleanup_acceptance_marker.sh`;
  - `outputs/taksklad_acceptance/README.md`;
  - `outputs/taksklad_acceptance/acceptance_manifest.json`;
  - `outputs/taksklad_acceptance/ACCEPTANCE_RESULTS_TEMPLATE.md`;
  - `outputs/taksklad_acceptance/TakSklad_Telegram_Acceptance_2026-05-31.xlsx`.
- На VDS выполнен read-only `./deploy/vds/acceptance_status.sh`.
- Результат VDS status:
  - `status=ok`;
  - backend health: `status=ok`;
  - контейнеры `backend-api`, `frontend`, `postgres`, `skladbot-worker`, `telegram-worker` running;
  - acceptance Excel SHA совпал: `204b932a704b39294b513a95964844db1ed74d028e3daff13beef3ab09ec98fd`;
  - acceptance marker пока пустой: `orders=0`, `scan_codes=0`, `pending_events=0`;
  - VDS `version.json`: `latest_version=1.1.7`, `min_supported_version=1.1.7`, `mandatory=false`, download URL пустые.
- Что не делалось:
  - `.env` не менялся;
  - БД не менялась;
  - контейнеры не перезапускались;
  - `version.json` не менялся;
  - release/archive/push-уведомления не запускались.

### Windows Acceptance Minimum Version Guard

- Уточнена проверка версии в `tools/windows_backend_acceptance.ps1`.
- Раньше helper был привязан к точной тестовой версии `1.1.17`; это могло заблокировать будущую сборку `2.0.0`, хотя она новее и подходит для приёмки.
- Теперь по умолчанию проверяется минимальная версия `MinAppVersion = 1.1.17`.
- Если нужно проверить строго конкретную сборку, можно явно передать `-ExpectedAppVersion`.
- Для `.exe` правило осталось строгим по безопасности: запуск разрешён только из fresh test archive с `build_manifest.json`, либо через явный `-SkipAppVersionCheck`.
- Обновлены:
  - `tools/release_preflight.py`;
  - `tools/prepare_acceptance_kit.py`;
  - `docs/windows-backend-acceptance.md`;
  - `docs/manual-acceptance-runbook.md`;
  - связанные unit tests.
- Проверено:
  - `.venv/bin/python -m unittest tests.test_windows_acceptance_helper tests.test_release_preflight tests.test_windows_test_build_helper` - 11 тестов OK.
  - `version.json` не изменён.

### Acceptance Kit Regeneration And VDS Status After Minimum Guard

- Acceptance kit пересобран после перехода Windows helper на минимальную версию.
- Локально проверено:
  - `.venv/bin/python -m unittest discover -s tests` - 122 теста OK;
  - `.venv/bin/python tools/release_preflight.py` - `status=ok`;
  - `git diff --check` - OK;
  - `bash -n deploy/vds/*.sh` - OK;
  - `version.json` без git diff.
- На VDS повторно синхронизированы только read-only acceptance-файлы:
  - acceptance scripts;
  - acceptance manifest/README/result template;
  - acceptance Excel.
- На VDS выполнен `./deploy/vds/acceptance_status.sh`.
- Результат:
  - `status=ok`;
  - backend health OK;
  - контейнеры `backend-api`, `frontend`, `postgres`, `skladbot-worker`, `telegram-worker` running;
  - acceptance Excel SHA совпал: `204b932a704b39294b513a95964844db1ed74d028e3daff13beef3ab09ec98fd`;
  - marker пока пустой: `orders=0`, `scan_codes=0`, `pending_events=0`;
  - VDS `version.json`: `latest_version=1.1.7`, `min_supported_version=1.1.7`, `mandatory=false`, download URL пустые.
- Не делалось:
  - релизный Windows archive не собирался;
  - GitHub Release не создавался;
  - push-уведомления и автообновление не запускались;
  - контейнеры не перезапускались.

### Telegram Update Isolation Guard

- Закрыт риск Telegram worker: одна ошибка при обработке кнопки или отчёта могла уронить весь `poll_once`.
- Это было опасно для согласованного сценария, где менеджер отправляет несколько Excel-файлов подряд: сбой на одном update мог помешать обработке следующих сообщений из той же пачки.
- Теперь каждый Telegram update обрабатывается отдельно:
  - ошибка логируется;
  - пользователю отправляется понятное сообщение с причиной;
  - следующий update продолжает обрабатываться;
  - offset сохраняется после пачки updates.
- Добавлен регрессионный тест:
  - первый update с кнопкой `Отчёт логистики` падает из-за временной backend-ошибки;
  - второй update с Excel-файлом всё равно ставится в очередь импорта;
  - offset сохраняется на последнем update.
- Проверено:
  - `.venv/bin/python -m unittest tests.test_backend_telegram_import` - 11 тестов OK.
  - `.venv/bin/python -m unittest discover -s tests` - 123 теста OK.
  - `.venv/bin/python tools/release_preflight.py` - `status=ok`.
  - `git diff --check` - OK.
  - `version.json` не изменён.

### Import Address Country Prefix Cleanup

- Закрыт мелкий риск по адресам из Excel/Smartup/геокодера.
- По утверждённому ТЗ адреса для логистики должны храниться без страны: город и адрес, но не `Узбекистан`.
- Backend Excel importer уже чистил русское `Узбекистан`, но не чистил латинские варианты.
- Теперь при импорте адресов удаляются префиксы:
  - `Узбекистан, ...`;
  - `Uzbekistan, ...`;
  - `O'zbekiston, ...`;
  - `Oʻzbekiston, ...`.
- Добавлен регрессионный тест на импорт Excel с адресами `Uzbekistan, Tashkent...` и `O'zbekiston, Toshkent...`.
- Проверено:
  - `.venv/bin/python -m unittest tests.test_backend_telegram_import` - 12 тестов OK.
  - `.venv/bin/python -m unittest discover -s tests` - 124 теста OK.
  - `.venv/bin/python tools/release_preflight.py` - `status=ok`.
  - `git diff --check` - OK.
  - `bash -n deploy/vds/*.sh` - OK.
  - `version.json` не изменён.

### Backend Address Geocoding For Telegram Import

- Закрыт пробел по ТЗ логистики: backend Telegram import теперь может получить координаты по адресу, если Excel-файл не содержит колонку координат.
- В `backend/app/excel_importer.py` добавлено:
  - чтение ключа из env `YANDEX_GEOCODER_API_KEY`;
  - запрос к Яндекс Геокодеру по адресу;
  - преобразование ответа Яндекса из `longitude latitude` в формат `latitude, longitude`;
  - cache на один импорт, чтобы одинаковые адреса не били API повторно;
  - предупреждение в meta, если координаты получить не удалось.
- В VDS compose проброшены:
  - `YANDEX_GEOCODER_API_KEY` в `backend-api` и `telegram-worker`;
  - `TAKSKLAD_DEFAULT_BLOCK_PRICE` в `telegram-worker`.
- `.env.example` обновлён без секретов.
- Добавлены регрессионные тесты:
  - импорт Excel без координат вызывает geocoder и сохраняет координаты;
  - VDS compose содержит env для геокодера и цены блока.
- Проверено:
  - `.venv/bin/python -m unittest tests.test_backend_telegram_import tests.test_vds_acceptance_scripts` - 16 тестов OK.
  - `docker compose --env-file deploy/vds/.env.example -f deploy/vds/docker-compose.yml config` - OK.
  - `.venv/bin/python -m unittest discover -s tests` - 126 тестов OK.
  - `.venv/bin/python tools/release_preflight.py` - `status=ok`.
  - `git diff --check` - OK.
  - `bash -n deploy/vds/*.sh` - OK.
  - `version.json` не изменён.

### VDS Backend Refresh After Geocoding Update

- На VDS доставлены актуальные файлы backend/import/VDS без секретов:
  - `backend/`;
  - `deploy/vds/`;
  - `outputs/taksklad_acceptance/`.
- В `deploy/vds/.env` проверены безопасные runtime-переменные:
  - `TAKSKLAD_DEFAULT_BLOCK_PRICE=240000` есть;
  - `YANDEX_GEOCODER_API_KEY` пока пустой, поэтому реальный геокодинг на VDS включится только после добавления ключа.
- Пересобраны и перезапущены только сервисы приложения:
  - `backend-api`;
  - `telegram-worker`;
  - `skladbot-worker`.
- Postgres data, frontend, `version.json`, GitHub Release и Windows release archive не трогались.
- VDS status после перезапуска:
  - `./deploy/vds/acceptance_status.sh` - `status=ok`;
  - публичный `https://api.taksklad.uz/health` - `200`, `status=ok`;
  - `backend-api`, `frontend`, `postgres`, `skladbot-worker`, `telegram-worker` - running;
  - acceptance Excel SHA совпал: `204b932a704b39294b513a95964844db1ed74d028e3daff13beef3ab09ec98fd`;
  - VDS `version.json`: `latest_version=1.1.7`, `min_supported_version=1.1.7`, `mandatory=false`, download URL пустые.
- Локальные проверки после доставки:
  - `.venv/bin/python -m unittest discover -s tests` - 126 тестов OK;
  - `.venv/bin/python tools/release_preflight.py` - `status=ok`;
  - `git diff --check` - OK;
  - `bash -n deploy/vds/*.sh` - OK;
  - `version.json` не изменён.

### Local Desktop 2.0 Source Launcher

- Добавлен `tools/run_desktop_local.sh`.
- Назначение: открыть текущую desktop-ветку из исходников, чтобы не запускать старый Windows-ярлык/старый exe `1.1.7`.
- Скрипт:
  - запускается из корня проекта;
  - использует `.venv/bin/python`, если virtualenv есть;
  - выставляет `PYTHONPATH=src`;
  - запускает `python -m taksklad.main`.
- Это не релизная сборка, не GitHub Release и не автообновление.
- В `docs/manual-acceptance-runbook.md` добавлена команда локального запуска:
  - `./tools/run_desktop_local.sh`.

### Telegram Hidden Admin Commands Guard

- Закрыт риск лишнего пользовательского шума и случайного доступа к служебным командам.
- Нижнее меню Telegram не изменилось:
  - `Дата отгрузки`;
  - `Отчёт логистики`;
  - `КИЗ по файлам`.
- Системное меню команд Telegram по-прежнему содержит только:
  - `/date`;
  - `/logistics`;
  - `/kiz_files`.
- Скрытые команды `/health`, `/imports`, `/logs` не попадают в `setMyCommands`.
- Добавлен env `TELEGRAM_ADMIN_CHAT_IDS`.
- Если `TELEGRAM_ADMIN_CHAT_IDS` задан, скрытые команды доступны только указанным chat_id; остальные разрешённые пользователи получают сообщение `Команда доступна только администратору`.
- Если `TELEGRAM_ADMIN_CHAT_IDS` пустой, сохраняется прежнее поведение для разрешённых chat_id, чтобы не потерять аварийную диагностику до настройки админов.
- На VDS доставлены:
  - `backend/app/telegram_worker.py`;
  - `deploy/vds/docker-compose.yml`;
  - `deploy/vds/.env.example`.
- В серверный `.env` добавлена пустая строка `TELEGRAM_ADMIN_CHAT_IDS=`, без секретов.
- Пересобраны `telegram-worker` и зависимый `backend-api`; Postgres data, frontend, `version.json`, Windows archive и GitHub Release не трогались.
- VDS status после перезапуска:
  - `./deploy/vds/acceptance_status.sh` - `status=ok`;
  - `https://api.taksklad.uz/health` - `200`, `status=ok`;
  - контейнеры running.
- Проверено:
  - `.venv/bin/python -m unittest tests.test_backend_telegram_import tests.test_vds_acceptance_scripts` - 17 тестов OK;
  - `docker compose --env-file deploy/vds/.env.example -f deploy/vds/docker-compose.yml config` - OK;
  - `git diff --check` - OK;
  - `bash -n deploy/vds/*.sh tools/run_desktop_local.sh` - OK.

### VDS Runtime Guard For Telegram Admins And SkladBot Interval

- Проверены VDS runtime-настройки без вывода секретов:
  - `TELEGRAM_ALLOWED_CHAT_IDS` задан;
  - `TELEGRAM_ADMIN_CHAT_IDS` был пустой;
  - `SKLADBOT_WORKER_INTERVAL_SECONDS` был `600`;
  - `YANDEX_GEOCODER_API_KEY` пока пустой.
- На VDS обновлены безопасные runtime-настройки:
  - `TELEGRAM_ADMIN_CHAT_IDS` зафиксирован равным текущим разрешённым chat_id, чтобы скрытые команды не оставались открытым fallback для будущих новых пользователей;
  - `SKLADBOT_WORKER_INTERVAL_SECONDS=60`, чтобы новые заявки SkladBot подтягивались быстрее.
- В коде `skladbot-worker` дефолтный интервал также снижен до 60 секунд, но ниже 60 секунд не опускается.
- В `deploy/vds/.env.example` `SKLADBOT_WORKER_INTERVAL_SECONDS` изменён с `600` на `60`.
- В `deploy/vds/docker-compose.yml` `env_file` сделан переключаемым через `TAKSKLAD_ENV_FILE`:
  - на VDS по умолчанию остаётся `.env`;
  - для локальных проверок можно использовать `.env.example`, не подмешивая локальные секреты.
- Проверка clean compose config с `.env.example` больше не подтягивает локальный `.env`; `SKLADBOT_WORKER_INTERVAL_SECONDS` в config равен `60`.
- На VDS синхронизированы `backend/app/skladbot_worker.py`, `backend/app/telegram_worker.py`, `deploy/vds/docker-compose.yml`, `deploy/vds/.env.example`.
- Перезапущены/подтверждены running:
  - `backend-api`;
  - `skladbot-worker`;
  - `telegram-worker`.
- Не трогались:
  - Postgres data;
  - frontend;
  - `version.json`;
  - Windows release archive;
  - GitHub Release;
  - push-уведомления.
- VDS status:
  - `TELEGRAM_ALLOWED_CHAT_IDS`: задано 2 chat_id;
  - `TELEGRAM_ADMIN_CHAT_IDS`: задано 2 chat_id;
  - `SKLADBOT_WORKER_INTERVAL_SECONDS=60`;
  - `YANDEX_GEOCODER_API_KEY_SET=False`;
  - `./deploy/vds/acceptance_status.sh` - `status=ok`;
  - `https://api.taksklad.uz/health` - `200`, `status=ok`.

### Release GO/NO-GO Machine Gate

- Добавлен `tools/release_go_no_go.py`.
- Назначение: не позволить назвать 2.0 готовым только по ощущениям или частичным тестам.
- Скрипт читает заполненный файл `outputs/taksklad_acceptance/ACCEPTANCE_RESULTS.md`.
- Для `GO` обязательны:
  - принят Telegram import;
  - принят SkladBot matching;
  - принята Windows desktop acceptance;
  - нет критичных дефектов;
  - rollback понятен;
  - `version.json` всё ещё не менялся;
  - строка `GO к подготовке release 2.0` отмечена;
  - строка `NO-GO, релиз откладывается` не отмечена.
- Раздел дефектов проверяется отдельно: незакрытый `critical`/`blocker`/`p0`/`p1` переводит результат в `no_go`.
- `tools/release_preflight.py` теперь требует наличие `tools/release_go_no_go.py`.
- `tools/build_windows_test_archive.ps1` кладёт `release_go_no_go.py` в test archive.
- `tools/prepare_acceptance_kit.py` добавляет в шаблон приёмки команду:
  - скопировать `ACCEPTANCE_RESULTS_TEMPLATE.md` в `ACCEPTANCE_RESULTS.md`;
  - заполнить фактические результаты;
  - запустить `release_go_no_go.py`.
- На VDS синхронизированы GO/NO-GO gate и acceptance kit.
- Проверено:
  - `.venv/bin/python -m unittest discover -s tests` - 138 тестов OK;
  - `.venv/bin/python tools/release_preflight.py` - `status=ok`;
  - `tools/release_go_no_go.py` на незаполненном шаблоне возвращает `status=no_go`, как и должен до ручной приёмки;
  - VDS `acceptance_status.sh` - `status=ok`;
  - `version.json` не изменён.

### Desktop Final Position Finish Flow

- Закрыт UX-разрыв в складском интерфейсе 2.0.
- Было: после полного скана последней позиции приложение всё ещё просило нажать `Следующая позиция`, а уже после этого открывало завершение заказа.
- Стало: если сотрудник досканировал последнюю позицию заказа, активируется `ЗАВЕРШИТЬ ЗАКАЗ`; печать сводного листа открывается после этой кнопки.
- Для непоследней позиции логика не менялась: после выполнения позиции активна `Следующая позиция`.
- Если позиция уже была полностью отсканирована при загрузке заказа, кнопки выставляются по той же логике.
- Проверено:
  - `.venv/bin/python -m unittest tests.test_desktop_ui_contract tests.test_printing tests.test_daily_report` - 8 тестов OK;
  - `version.json` не изменён;
  - Windows release archive и push-уведомления не запускались.

### Telegram Bottom Keyboard Regression Guard

- Усилен тестовый контракт Telegram-интерфейса.
- Теперь `tests/test_backend_telegram_import.py` проверяет:
  - все три пользовательские кнопки нижней панели: `Дата отгрузки`, `Отчёт логистики`, `КИЗ по файлам`;
  - `resize_keyboard=True`;
  - `is_persistent=True`;
  - клавиатура остаётся в `reply_markup` при отправке пользователю Excel-документа.
- Это защищает согласованное ТЗ: менеджер работает через нижнюю панель Telegram, а не через видимые админские команды.
- Проверено:
  - `.venv/bin/python -m unittest tests.test_backend_telegram_import` - 16 тестов OK;
  - `version.json` не изменён.

### Release GO/NO-GO Section Gate

- Усилен `tools/release_go_no_go.py`.
- Был риск: можно было вручную отметить финальные галочки `GO`, но оставить пустыми реальные разделы приёмки.
- Теперь gate требует:
  - наличие разделов `1. Preflight`, `2. Telegram Import`, `3. SkladBot Matching`, `4. Windows Desktop Acceptance`, `5. Cleanup`, `6. Defects / Known Issues`, `7. Go / No-Go`;
  - все чекбоксы в разделах `1-5` должны быть отмечены;
  - финальные GO-чекбоксы должны быть отмечены;
  - `NO-GO` должен быть не отмечен;
  - незакрытые критичные дефекты по-прежнему переводят результат в `no_go`.
- Это делает acceptance gate ближе к реальному релизному решению: нельзя перейти к release 2.0 без preflight, Telegram, SkladBot, Windows и cleanup.
- Проверено:
  - `.venv/bin/python -m unittest tests.test_release_go_no_go tests.test_acceptance_excel_generator tests.test_release_preflight` - 18 тестов OK;
  - `version.json` не изменён.

### SkladBot Safe Partial Product Match

- Закрыт риск ложного `Не найдено`, когда TakSklad-группа содержит часть товаров, а SkladBot-заявка уже содержит полный набор.
- Новое правило: все товары и блоки из TakSklad должны совпасть; лишние товары в SkladBot-заявке допускаются.
- Если несколько SkladBot-заявок подходят по partial-match, номер не пишется и статус остаётся `multiple` / `Несколько совпадений`.
- Пустая группа товаров не матчится.
- Изменены backend worker и desktop fallback:
  - `backend/app/skladbot_worker.py`;
  - `src/taksklad/skladbot.py`.
- Добавлены регрессии для backend и desktop:
  - SkladBot-заявка с лишним товаром матчится;
  - пустая группа товаров не матчится;
  - две подходящие partial-match заявки дают `multiple`.
- Проверено:
  - `.venv/bin/python -m unittest tests.test_backend_skladbot_worker tests.test_skladbot_sync` - 30 тестов OK;
  - `.venv/bin/python -m unittest discover -s tests` - 142 теста OK;
  - `.venv/bin/python tools/release_preflight.py` - `status=ok`;
  - `git diff --check` - OK;
  - `tools/release_go_no_go.py` на незаполненном acceptance template вернул `status=no_go` и exit code `3`, как и должен до ручной приёмки;
  - `version.json` не изменён.

### VDS SkladBot Worker Sync After Partial Match

- На VDS обнаружено расхождение `backend/app/skladbot_worker.py` с локальной веткой после partial-match фикса.
- Синхронизирован только файл `backend/app/skladbot_worker.py` на `/opt/taksklad/app`.
- Пересобран и перезапущен только сервис `skladbot-worker`.
- Не трогались:
  - Postgres data volume;
  - `backend-api`;
  - `telegram-worker`;
  - frontend;
  - `version.json`;
  - Windows archive;
  - GitHub Release/push-обновления.
- Проверено на VDS:
  - SHA256 `backend/app/skladbot_worker.py` совпадает с локальным: `63445d4a84fcb92126e7a14448002b628c1d809541bab2d1c669d5cad46ae78c`;
  - `deploy/vds/acceptance_status.sh` вернул `status=ok`;
  - `skladbot-worker` running;
  - worker log: `no active backend orders, skip SkladBot API`;
  - acceptance marker пустой: `orders=0`, `scan_codes=0`, `pending_events=0`;
  - VDS `version.json` остался `1.1.7`, без download URL.

### Desktop MVP 2.0 Version Marker

- Добавлен визуальный маркер ветки в desktop: нижняя строка теперь показывает `Версия: 1.1.17 · MVP 2.0`.
- Зачем: чтобы сразу отличать свежий локальный/test запуск от старого рабочего ярлыка `1.1.7`.
- Публичный `version.json` не менялся, auto-update не включался.
- Добавлено в startup self-check поле `build_label=MVP 2.0`, без секретов.
- Изменены:
  - `src/taksklad/config.py`;
  - `src/taksklad/startup_check.py`;
  - `src/taksklad/main.py`;
  - `tests/test_startup_check.py`;
  - `tests/test_desktop_ui_contract.py`.
- Проверено:
  - `.venv/bin/python -m unittest tests.test_startup_check tests.test_desktop_ui_contract` - 7 тестов OK.
  - `.venv/bin/python -m unittest discover -s tests` - 143 теста OK.
  - `.venv/bin/python tools/release_preflight.py` - `status=ok`.
  - `git diff --check` - OK.

### Windows Acceptance Build Label Guard

- Усилена защита от запуска старой или неправильной Windows-сборки во время приёмки 2.0.
- `tools/build_windows_test_archive.ps1` теперь:
  - читает `APP_BUILD_LABEL` из `src/taksklad/config.py`;
  - по умолчанию требует `MVP 2.0`;
  - записывает `app_build_label` в `build_manifest.json`;
  - показывает build label в `README_TEST_BUILD.md`.
- `tools/windows_backend_acceptance.ps1` теперь:
  - при запуске из исходников проверяет `APP_BUILD_LABEL = MVP 2.0`;
  - при запуске `TakSklad.exe` требует `build_manifest.json` со свежим `app_build_label`;
  - останавливает старый `1.1.7` exe или архив без маркера 2.0 до запуска приложения.
- Обновлены acceptance README/runbook и release preflight, чтобы этот guard был обязательным.
- Зачем: пользователь уже столкнулся с запуском локальной `1.1.7`; теперь Windows-приёмка не даст принять старый интерфейс за MVP 2.0.
- Проверено:
  - `.venv/bin/python -m unittest tests.test_windows_test_build_helper tests.test_windows_acceptance_helper tests.test_release_preflight tests.test_startup_check tests.test_desktop_ui_contract` - 18 тестов OK.
  - `version.json` не менялся, push-обновления не включались.

### Desktop Party Summary UI

- В рабочий экран склада добавлена общая статистика выбранной партии.
- После выбора заказа сотрудник видит:
  - количество позиций;
  - общий план в блоках;
  - общую сумму заказа/партии;
  - дату отгрузки;
  - номер заявки SkladBot или пометку `без номера SkladBot`.
- При сбросе выбора текст возвращается в `Партия не выбрана`.
- Исправлена читаемость жёлтой кнопки `СЛЕДУЮЩАЯ ПОЗИЦИЯ`: текст теперь чёрный, под утверждённую палитру `#F0E68C + чёрный`.
- Зачем: склад должен видеть не только текущую позицию, но и общий контекст партии, без лишних админских кнопок и без открытия дополнительных окон.
- Проверено:
  - `.venv/bin/python -m unittest tests.test_desktop_ui_contract tests.test_startup_check` - 8 тестов OK;
  - `.venv/bin/python -m py_compile src/taksklad/main.py` - OK.

### Telegram Date Display Polish

- Улучшено отображение дат в Telegram worker.
- Backend по-прежнему хранит и отдаёт даты в ISO-формате `YYYY-MM-DD`, но пользователю в Telegram теперь показывается `DD.MM.YYYY`.
- Обновлены:
  - кнопки выбора даты логистического отчёта: `Логистика 29.05.2026`;
  - список файлов в `КИЗ по файлам`: даты отображаются как `29.05.2026`, а не `2026-05-29`.
- API-контракты не менялись.
- Зачем: менеджер работает из Telegram, поэтому даты в кнопках должны быть человеческими, без технического ISO-формата.
- Проверено:
  - `.venv/bin/python -m unittest tests.test_backend_telegram_import` - 18 тестов OK;
  - `.venv/bin/python -m py_compile backend/app/telegram_worker.py` - OK.

### VDS Telegram Worker Date Display Sync

- Локальная Telegram-правка с пользовательским форматом дат доставлена на VDS.
- Синхронизирован файл:
  - `backend/app/telegram_worker.py`.
- Пересобран и перезапущен `telegram-worker`.
- Docker Compose также пересоздал `backend-api` как зависимость сборки, но Postgres volume и данные не трогались.
- Не трогались:
  - Postgres data volume;
  - frontend;
  - `skladbot-worker`;
  - Windows archive;
  - `version.json`;
  - GitHub Release/push-обновления.
- Проверено на VDS:
  - SHA256 `backend/app/telegram_worker.py` совпадает с локальным: `16835844a4e37c7e59b39aefa07e721bc9846ab6bf3d571d6386ccbd5964b756`;
  - `https://api.taksklad.uz/health` вернул `status=ok`;
  - `deploy/vds/acceptance_status.sh` вернул `status=ok`;
  - `telegram-worker` running;
  - VDS `version.json` остался `1.1.7`, без download URL.

### SkladBot Window Documentation Clarified

- Проверены runtime-настройки `skladbot-worker` на VDS:
  - `SKLADBOT_CUSTOMER_ID=6211`;
  - `SKLADBOT_SHIPMENT_TYPE_ID=3389`;
  - `SKLADBOT_SYNC_LOOKBACK_DAYS=1`;
  - `SKLADBOT_REQUESTS_LIMIT=100`;
  - `SKLADBOT_WORKER_INTERVAL_SECONDS=60`;
  - `SKLADBOT_API_TIMEOUT_SECONDS=8`;
  - `SKLADBOT_API_MAX_RETRIES=2`.
- Документация уточнена: VDS worker работает узко и быстро по вчера/сегодня, а 14-дневное окно относится только к desktop fallback.
- Зачем: в docs был старый общий текст про 14 дней, который противоречил согласованной серверной оптимизации.

### Backend Duplicate KIZ Conflict Guard

- Закрыт риск параллельной работы двух ПК по одному backend.
- Раньше любой ответ backend `409 Code already scanned` desktop-очередь считала уже синхронизированным событием.
- Это было удобно для повторной отправки после сетевого сбоя, но опасно для настоящего дубля: если другой ПК уже записал этот КИЗ в другую позицию, локальное событие могло исчезнуть из очереди.
- Теперь backend:
  - повтор того же кода в той же позиции возвращает успешный `ScanRead` без повторного увеличения счётчика;
  - тот же код в другой позиции возвращает `409` с причиной `Code already scanned in another order item`.
- Desktop backend queue больше не удаляет такой конфликт как успешно синхронизированный.
- Зачем: при ручной Windows-приёмке и работе двух ПК дубли КИЗов не должны тихо исчезать из очереди.
- Проверено:
  - `.venv/bin/python -m unittest tests.test_backend_api_persistence.BackendApiPersistenceTests.test_scan_create_is_idempotent_for_same_item_and_rejects_cross_order_duplicate tests.test_backend_bridge.BackendBridgeTests.test_backend_queue_keeps_ambiguous_duplicate_scan_conflict` - OK;
  - `.venv/bin/python -m py_compile backend/app/orders_service.py src/taksklad/backend_events.py tests/test_backend_api_persistence.py tests/test_backend_bridge.py` - OK;
  - `.venv/bin/python -m unittest discover -s tests` - 150 тестов OK;
  - `.venv/bin/python tools/release_preflight.py` - `status=ok`.
- Доставлено на VDS и применено через rebuild только `backend-api`.
- Проверено на VDS:
  - `https://api.taksklad.uz/health` - `status=ok`;
  - `./deploy/vds/acceptance_status.sh` - `status=ok`, `version_json=1.1.7`, `release_go_no_go.status=no_go`.

### Desktop Backend Status Indicator

- На складской desktop-экран добавлен отдельный статус backend в блок статистики.
- Возможные состояния:
  - `Backend: выключен`;
  - `Backend: не настроен`;
  - `Backend: ожидает проверки`;
  - `Backend: online, список из VDS`;
  - `Backend: online, запись включена`;
  - `Backend: очередь N`;
  - `Backend: ошибка, очередь N`.
- Статус обновляется после загрузки списка, фоновой синхронизации backend queue и ошибок backend queue.
- Зачем: в плане 2.0 был отдельный пункт про понятный backend online/offline/sync pending. На Windows-приёмке оператор должен видеть, что тестовая копия работает через VDS, без открытия служебных окон.
- Обновлены Windows acceptance checklist и acceptance kit: добавлена проверка `Backend: online, список из VDS`.
- Проверено:
  - `.venv/bin/python -m unittest tests.test_desktop_ui_contract tests.test_backend_bridge` - 15 тестов OK;
  - `.venv/bin/python -m py_compile src/taksklad/main.py src/taksklad/app_day_end.py tests/test_desktop_ui_contract.py` - OK;
  - `.venv/bin/python -m unittest discover -s tests` - 152 теста OK;
  - `.venv/bin/python tools/release_preflight.py` - `status=ok`;
  - `.venv/bin/python tools/release_go_no_go.py --results outputs/taksklad_acceptance/ACCEPTANCE_RESULTS.md` - ожидаемо `status=no_go`.

### Release GO/NO-GO Template Coverage Guard

- Усилен `tools/release_go_no_go.py`: gate теперь сверяет `ACCEPTANCE_RESULTS.md` с соседним `ACCEPTANCE_RESULTS_TEMPLATE.md`.
- Если обязательный чекбокс из шаблона удалили из файла результата, релиз остаётся `no_go` с явной причиной `required acceptance checkbox is missing`.
- Если чекбокс есть, но не отмечен, релиз остаётся `no_go` с причиной `required acceptance checkbox is not checked`.
- Зачем: нельзя случайно или вручную "пройти" приёмку 2.0, удалив неудобный пункт из `ACCEPTANCE_RESULTS.md`.
- Проверено:
  - `.venv/bin/python -m unittest tests.test_release_go_no_go tests.test_vds_acceptance_scripts tests.test_acceptance_excel_generator` - 16 тестов OK;
  - `.venv/bin/python -m unittest discover -s tests` - 153 теста OK;
  - `.venv/bin/python tools/release_preflight.py` - `status=ok`;
  - `.venv/bin/python tools/release_go_no_go.py --results outputs/taksklad_acceptance/ACCEPTANCE_RESULTS.md` - ожидаемо `status=no_go`, ручная Telegram/SkladBot/Windows-приёмка ещё не закрыта;
  - `git diff -- version.json` - без изменений;
  - `git diff --check` - OK.
- Доставлено на VDS:
  - `tools/release_go_no_go.py`;
  - `tests/test_release_go_no_go.py`;
  - связанные docs/отчёт;
  - `ACCEPTANCE_RESULTS.md` и `ACCEPTANCE_RESULTS_TEMPLATE.md`.
- Проверено на VDS:
  - `python3 -m unittest tests.test_release_go_no_go` - 8 тестов OK;
  - `./deploy/vds/acceptance_status.sh` - `status=ok`;
  - `./deploy/vds/acceptance_status.sh --require-go` - ожидаемо exit `3`, причина `release GO/NO-GO is not go: no_go`;
  - контейнеры running, `version_json=1.1.7`, download URL не задан.

### Telegram Logistics Report Error Message

- Улучшена обработка ошибки кнопки `Отчёт логистики` в Telegram.
- Если backend не может собрать отчёт, например из-за отсутствующих координат, worker больше не уходит только в общий fallback `Не удалось выполнить действие Telegram`.
- Теперь менеджер получает конкретное сообщение: `Не удалось выгрузить отчёт логистики за <дата>: <причина backend>`.
- Зачем: логистический отчёт должен быть рабочим управленческим действием менеджера, поэтому ошибки по координатам/датам должны быть понятны без чтения логов.
- Проверено:
  - `.venv/bin/python -m unittest tests.test_backend_telegram_import tests.test_backend_api_persistence` - 33 теста OK;
  - `.venv/bin/python -m py_compile backend/app/telegram_worker.py tests/test_backend_telegram_import.py` - OK;
  - `.venv/bin/python -m unittest discover -s tests` - 154 теста OK;
  - `.venv/bin/python tools/release_preflight.py` - `status=ok`;
  - `.venv/bin/python tools/release_go_no_go.py --results outputs/taksklad_acceptance/ACCEPTANCE_RESULTS.md` - ожидаемо `status=no_go`;
  - `git diff -- version.json` - без изменений;
  - `git diff --check` - OK.
- Доставлено на VDS:
  - `backend/app/telegram_worker.py`;
  - `tests/test_backend_telegram_import.py`;
  - docs/отчёт.
- Пересобран `telegram-worker`; Docker Compose также пересоздал `backend-api` как зависимость образа.
- Не трогались:
  - Postgres volume;
  - frontend;
  - `skladbot-worker`;
  - `version.json`;
  - Windows archive;
  - GitHub Release/push-обновления.
- Проверено на VDS:
  - `docker compose exec telegram-worker python -m py_compile /app/app/telegram_worker.py` - OK;
  - `https://api.taksklad.uz/health` - `status=ok`;
  - `./deploy/vds/acceptance_status.sh` - `status=ok`;
  - `backend-api`, `telegram-worker`, `skladbot-worker`, `frontend`, `postgres` running;
  - `version_json=1.1.7`, download URL не задан.
- Прямой запуск `python3 -m unittest tests.test_backend_telegram_import` на VDS-хосте не используется как доказательство: системный Python сервера без `openpyxl`, runtime проверен внутри контейнера.

### SkladBot Request Type And Address Diagnostic Guard

- Ужесточён фильтр типа заявки SkladBot.
- Теперь заявки с возвратными словами (`возврат`, `return`, `returned`) не проходят matching даже если в названии есть `3PL` и `отгрузка`.
- Поддержка рабочих вариантов `3PL отгрузка` и `Отгрузка 3PL` сохранена.
- В read-only диагностике SkladBot добавлено поле `address_soft_match`.
- Адрес остаётся мягким признаком: он виден в диагностике, но не блокирует совпадение, если дата, клиент, оплата, товар и блоки совпали.
- Зачем: финальное ТЗ требует матчить только отгрузочные 3PL-заявки и не делать адрес жёстким условием.
- Проверено:
  - `.venv/bin/python -m unittest tests.test_backend_skladbot_worker tests.test_skladbot_sync` - 33 теста OK;
  - `.venv/bin/python -m py_compile backend/app/skladbot_worker.py backend/app/skladbot_diagnostic.py src/taksklad/skladbot.py tests/test_backend_skladbot_worker.py tests/test_skladbot_sync.py` - OK;
  - `.venv/bin/python -m unittest discover -s tests` - 156 тестов OK;
  - `.venv/bin/python tools/release_preflight.py` - `status=ok`;
  - `git diff -- version.json` - без изменений;
  - `git diff --check` - OK.

### TakSklad 2.0.0 Release Version Alignment

- Поднята версия desktop-приложения до `2.0.0`.
- Поднята версия backend API до `2.0.0`, чтобы `/health` больше не показывал техническую `0.1.0`.
- Поднята версия frontend package до `2.0.0`.
- Windows acceptance helpers теперь требуют минимум `2.0.0`, а не промежуточную `1.1.17`.
- На VDS восстановлены рабочие env-значения после неудачной синхронизации:
  - домен backend возвращён на `api.taksklad.uz`;
  - frontend временно размещён на том же домене `https://api.taksklad.uz/`, потому что DNS `app.taksklad.uz` пока не существует;
  - backend route ограничен путями `/api` и `/health`, frontend занимает корень домена;
  - placeholder-секреты заменены на новые значения вне git, локальная копия сохранена в ignored-файл `.env.taksklad-vds-2.0.generated.json`;
  - пароль пользователя Postgres синхронизирован с новым `DATABASE_URL`.
- Публичный `version.json` пока не менялся на этом шаге: сначала нужна GitHub Release-сборка и SHA256 артефактов.
- Проверено:
  - `.venv/bin/python -m unittest discover -s tests` - 156 тестов OK;
  - `npm run build` в `frontend/` - OK;
  - `.venv/bin/python tools/release_preflight.py` - `status=ok`;
  - `git diff --check` - OK.
- Проверено на VDS:
  - `https://api.taksklad.uz/health` - `status=ok`, `version=2.0.0`;
  - internal `GET /api/v1/orders/active` с service token - HTTP 200;
  - `https://api.taksklad.uz/` без BasicAuth - HTTP 401;
  - `https://api.taksklad.uz/` с BasicAuth - HTML frontend;
  - `./deploy/vds/acceptance_status.sh` - `status=ok`, GO/NO-GO ожидаемо `no_go` до ручной приёмки.

### GitHub Release 2.0.0 And Public Update Manifest

- Создан GitHub Release `v2.0.0`.
- GitHub Actions `Build Windows Release` успешно собрал и загрузил:
  - `TakSklad.exe`;
  - `TakSklad-windows-x64.zip`;
  - SHA256-файлы для обоих артефактов.
- Публичный `version.json` переключён на `latest_version = 2.0.0`.
- Режим обновления выбран staged:
  - `package_type = onefile_exe`;
  - `mandatory = false`;
  - `min_supported_version = 1.1.7`;
  - `download_url_onedir` и `sha256_onedir` сохранены в manifest для ручной/следующей onedir-переходной стадии.
- Зачем: рабочие ПК на `1.1.7` получат безопасный onefile update до 2.0 без принудительной блокировки смены; onedir ZIP уже доступен в релизе.
- Проверено:
  - GitHub Actions run `26712547457` - success;
  - `python3 -m json.tool version.json` - OK;
  - SHA256 скачанного `TakSklad.exe` совпадает с manifest;
  - SHA256 скачанного `TakSklad-windows-x64.zip` совпадает с manifest.

### Backend Import Export To Google Sheets Data

- Причина: Telegram import успешно писал Excel в backend/Postgres, но не дописывал строки в Google Sheets `data`, поэтому менеджер видел `completed`, а лист оставался пустым.
- Добавлен backend-экспорт импортированных строк в Google Sheets после `/api/v1/imports`.
- Формат записи совпадает с desktop-логикой:
  - рабочие колонки `Дата отгрузки`, `Тип оплаты`, `Клиент`, `Адрес`, `Торговый представитель`, `Товары`, `Кол-во ШТ`, `Кол-во блок`, `Отсканированные коды`, `Статус`;
  - служебные колонки начинаются с `AA`: `ID заказа`, `ID импорта`, `Источник файла`, `Строка файла`, `Дата импорта`, SkladBot-поля.
- Дубликаты фильтруются по `ID импорта`, `ID заказа` и бизнес-ключу строки, чтобы повторная отправка файла не плодила строки в `data`.
- Важное поведение для восстановления: если файл уже есть в backend, но раньше не попал в Google Sheets, повторный import всё равно отдаёт валидные строки в Google Sheets export. Postgres-дубли не создаются, а Sheets дописывает только отсутствующие строки.
- Если Google Sheets недоступен, backend import не откатывается: Postgres остаётся источником истины, а результат `google_sheets.status=error` сохраняется в истории импорта.
- Telegram-ответ после импорта теперь показывает отдельную строку `Google Sheets: ...`, чтобы сразу было видно, дошли ли строки до листа `data`.
- Для VDS добавлены env-настройки:
  - `TAKSKLAD_GOOGLE_SPREADSHEET_ID`;
  - `TAKSKLAD_GOOGLE_SHEET_NAME`;
  - `TAKSKLAD_GOOGLE_CREDENTIALS_JSON_BASE64`.
- Проверено:
  - `.venv/bin/python -m unittest tests.test_backend_api_persistence tests.test_backend_telegram_import` - 35 тестов OK;
  - `.venv/bin/python -m unittest discover -s tests` - 158 тестов OK;
  - `.venv/bin/python -m py_compile backend/app/imports_service.py backend/app/schemas.py backend/app/telegram_worker.py backend/app/google_sheets_exporter.py` - OK.
- Доставлено на VDS:
  - обновлены `backend-api` и `telegram-worker`;
  - добавлен `gspread` в backend image;
  - в серверный `.env` добавлены Google Sheets env-параметры без вывода секретов в лог.
- Проверено на VDS:
  - `https://api.taksklad.uz/health` - `status=ok`, `version=2.0.0`;
  - контейнер `backend-api` видит `gspread`, Google credentials и spreadsheet id;
  - повторный import файла `Шаблон_отправки_заказов_на_склад_26_05_2026_2ч.xlsx` вернул `duplicate_rows=21`, `items_created=0`, `google_sheets.status=completed`, `google_sheets.imported=21`;
  - лист Google Sheets `data`: было 0 строк данных, стало 21;
  - свежие логи `backend-api`/`telegram-worker` без ошибок Google Sheets export.

### Reverse Geocode Empty Import Addresses

- Причина: в шаблоне `Шаблон_отправки_заказов_на_склад_26_05_2026_2ч.xlsx` колонка `Адрес доставки*` пустая, но заполнена колонка `Координаты`; из-за этого после backend export в Google Sheets `data` попадал текст `Адрес не указан`.
- Решение:
  - если адрес в Excel пустой, но координаты есть, backend делает reverse geocode через Яндекс Геокодер;
  - полученный адрес очищается от страны в начале строки (`Узбекистан`, `Uzbekistan`, `O'zbekiston`, `Oʻzbekiston`);
  - очищенный адрес пишется в поле `Адрес`;
  - если reverse geocode временно не сработал, вместо пустого адреса сохраняется `Координаты: ...`, чтобы оператор видел полезный ориентир.
- Для повторного импорта уже загруженного файла добавлена защита:
  - backend не создаёт дубль позиции, если изменился адрес, но `ID импорта` тот же;
  - Google Sheets export умеет обновлять существующую строку по `ID импорта`/`ID заказа`, если старый адрес был пустой или `Адрес не указан`, а новый адрес получен из координат.
- Telegram-ответ по import теперь показывает не только записанные строки и повторы, но и `адреса обновлены N`.
- Проверено:
  - `.venv/bin/python -m unittest tests.test_backend_api_persistence tests.test_backend_telegram_import` - 38 тестов OK;
  - `.venv/bin/python -m unittest discover -s tests` - 161 тест OK;
  - `.venv/bin/python -m py_compile backend/app/excel_importer.py backend/app/google_sheets_exporter.py backend/app/imports_service.py backend/app/schemas.py backend/app/telegram_worker.py` - OK.
- Доставлено на VDS:
  - обновлены `backend-api` и `telegram-worker`;
  - `https://api.taksklad.uz/health` - `status=ok`.
- Блокер для фактического reverse geocode на VDS:
  - в контейнере `backend-api` переменная `YANDEX_GEOCODER_API_KEY` пустая;
  - без ключа Яндекс не вернёт адрес по координатам;
  - после добавления ключа в серверный `.env` нужно пересоздать `backend-api` и повторить import/backfill.
- Блокер снят:
  - старый ключ Яндекс Геокодера найден в локальном restore point старой версии `config.py`;
  - ключ перенесён в локальный `deploy/vds/.env` и серверный `/opt/taksklad/app/deploy/vds/.env` без вывода секрета в лог;
  - `backend-api` и `telegram-worker` пересозданы;
  - проверка в контейнере `telegram-worker`: ключ виден, reverse geocode возвращает адрес, страна `Узбекистан` удалена из начала строки.
- Backfill текущего файла:
  - повторно прогнан `Шаблон_отправки_заказов_на_склад_26_05_2026_2ч.xlsx`;
  - `duplicate_rows=21`, `items_created=0`, то есть дубли в Postgres не созданы;
  - `meta_geocoded_count=21`, `meta_geocode_failed_count=0`;
  - `google_sheets.imported=0`, `google_sheets.duplicates=21`, `google_sheets.updated=21`;
  - в Google Sheets `data`: было 21 строка с `Адрес не указан`, стало 0; все 21 строки получили адреса.

### Backend Address Backfill For Existing Imports

- Причина: Google Sheets уже получил адреса через Яндекс Геокодер, но desktop-приложение читает список заказов из backend, а не напрямую из Google Sheets.
- Проблема проявлялась так:
  - повторный import находил строки как дубликаты по `ID импорта`;
  - Google Sheets обновлял адреса в `data`;
  - backend не менял уже созданный `Order.address`, поэтому приложение после `Обновить` продолжало видеть `Адрес не указан`.
- Решение:
  - при повторном import backend ищет существующую позицию по `ID импорта`, затем по `item_key`;
  - если новая строка содержит реальный адрес, а старый `Order.address` пустой или `Адрес не указан`, backend обновляет адрес заказа;
  - координаты сохраняются в `Order.raw_payload`;
  - в `Order.raw_payload` фиксируются `address_backfilled_at` и `address_backfill_source`;
  - дубли заказов и позиций в Postgres не создаются.
- Telegram-ответ после import теперь показывает отдельную строку `Адреса в backend обновлены: N`.
- Проверено локально:
  - `.venv/bin/python -m unittest tests.test_backend_api_persistence tests.test_backend_telegram_import` - 38 тестов OK;
  - `.venv/bin/python -m py_compile backend/app/imports_service.py backend/app/schemas.py backend/app/telegram_worker.py` - OK.
- Дополнительная проверка:
  - `.venv/bin/python -m unittest discover -s tests` - 161 тест OK.
- Доставлено на VDS:
  - перед заменой создан restore point `/opt/taksklad/restore_points/pre-backend-address-backfill-20260531T162615Z`;
  - обновлены `backend-api` и `telegram-worker`;
  - `https://api.taksklad.uz/health` - `status=ok`, `version=2.0.0`.
- Боевой backfill:
  - повторно прогнан файл `Шаблон_отправки_заказов_на_склад_26_05_2026_2ч.xlsx`;
  - до backfill: активных заказов в backend - 8, с пустым адресом - 8;
  - результат import: `items_created=0`, `orders_created=0`, `duplicate_rows=21`, `backend_address_updates=8`;
  - после backfill: активных заказов в backend - 8, с пустым адресом - 0;
  - значит desktop-приложение после обычного `Обновить` должно подтянуть адреса из backend.

### Google Sheets To Backend Sync Worker

- Причина: после перехода desktop на backend-режим приложение читает активные заказы из Postgres, а не напрямую из Google Sheets.
- Проблема: если менеджер вручную меняет в листе `data` количество блоков, адрес, дату, клиента или товар, приложение не видит правку, пока backend не синхронизируется с Google Sheets.
- Решение:
  - добавлен отдельный backend-worker `app.google_sheets_sync_worker`;
  - worker читает лист `data`, ищет строки по `ID импорта`, затем fallback по `ID заказа`;
  - обновляет только активные backend-заказы;
  - обновляет поля заказа: `Дата отгрузки`, `Тип оплаты`, `Клиент`, `Адрес`, `Торговый представитель`;
  - обновляет поля позиции: `Товары`, `Кол-во ШТ`, `Кол-во блок`;
  - переносит SkladBot-поля из Google Sheets в `Order.raw_payload`, если они заполнены;
  - пишет sync metadata в `raw_payload`: `google_sheet_synced_at`, `google_sheet_row_number`;
  - пишет общий audit `google_sheets_backend_sync`.
- Защита от опасных правок:
  - завершённые заказы не обновляются;
  - завершённые позиции не обновляются;
  - если в Google Sheets новое `Кол-во блок` меньше уже отсканированного количества, backend не меняет план и пишет audit `google_sheets_backend_sync_conflict`;
  - если товар меняют после начала сканирования, backend не меняет товар и пишет conflict.
- Для VDS добавлен сервис `google-sheets-sync-worker` в `deploy/vds/docker-compose.yml`.
- Настройка интервала:
  - `GOOGLE_SHEETS_SYNC_INTERVAL_SECONDS=60`;
  - минимальный интервал в коде - 30 секунд.
- Проверено локально:
  - `.venv/bin/python -m unittest tests.test_google_sheets_sync_worker` - 3 теста OK;
  - `.venv/bin/python -m unittest tests.test_vds_acceptance_scripts tests.test_google_sheets_sync_worker tests.test_backend_api_persistence` - 24 теста OK;
  - `.venv/bin/python -m unittest discover -s tests` - 164 теста OK;
  - `.venv/bin/python -m py_compile backend/app/google_sheets_sync_worker.py backend/app/google_sheets_exporter.py` - OK.
- Доставлено на VDS:
  - перед заменой создан restore point `/opt/taksklad/restore_points/pre-google-sheets-sync-worker-20260531T163848Z`;
  - пересобраны и запущены `backend-api`, `telegram-worker`, `skladbot-worker`, `google-sheets-sync-worker`;
  - `https://api.taksklad.uz/health` - `status=ok`, `version=2.0.0`;
  - `google-sheets-sync-worker` запущен отдельным контейнером.
- Первая VDS-синхронизация:
  - лог worker: `rows=21 matched=21 missing=0 orders_updated=0 items_updated=1 conflicts=0`;
  - сверка Google Sheets `data` и backend по активным позициям: 21 строка в Google, 21 позиция в backend, расхождений по `Кол-во ШТ`/`Кол-во блок` нет;
  - текущие количества блоков в Google и backend совпадают: `1, 2, 3, 5, 10`.

### Desktop Refresh Forces All Backend Sources

- Причина: фоновые worker-ы синхронизируют Google Sheets и SkladBot примерно раз в минуту, но при ручном нажатии `Обновить` оператор ожидает максимально свежий список сразу.
- Решение на backend:
  - добавлен endpoint `POST /api/v1/sync/sources`;
  - endpoint запускает принудительную синхронизацию Google Sheets `data` -> backend;
  - затем, если параметр `skladbot=1`, запускает SkladBot -> backend sync в фоне, чтобы кнопка `Обновить` не висела несколько минут на SkladBot API/429;
  - для ручной диагностики оставлен режим `wait_skladbot=1`, который ждёт завершения SkladBot sync в ответе API;
  - endpoint не падает целиком, если один источник временно недоступен: возвращает `completed_with_errors` и результат по каждому источнику;
  - добавлен process lock, чтобы два одновременных нажатия `Обновить` с разных ПК не запускали параллельную тяжёлую синхронизацию.
- Защита SkladBot:
  - ручной sync из кнопки и постоянный `skladbot-worker` используют общий PostgreSQL advisory lock;
  - если один SkladBot sync уже идёт, второй не лезет в API SkladBot и сразу пропускается;
  - это снижает риск 429/долгого зависания, когда склад нажал `Обновить` в момент фоновой синхронизации.
- Решение на desktop:
  - в backend-режиме `Обновить` сначала отправляет накопленную локальную очередь КИЗов/завершений;
  - затем вызывает `POST /api/v1/sync/sources?skladbot=1&wait_skladbot=0`;
  - затем загружает активные заказы через `GET /api/v1/orders/active`;
  - статусная строка показывает, сколько правок пришло из Google, и отдельно пишет, что SkladBot обновляется в фоне.
- Таймаут:
  - обычные backend-запросы остаются на стандартном таймауте;
  - для принудительной синхронизации источников desktop использует увеличенный timeout 45 секунд;
  - SkladBot не блокирует этот timeout, потому что запускается в фоне.
- Проверено локально:
  - `.venv/bin/python -m unittest tests.test_backend_api_persistence.BackendApiPersistenceTests.test_sync_sources_runs_google_sheet_sync_then_skladbot_sync tests.test_backend_api_persistence.BackendApiPersistenceTests.test_sync_sources_can_skip_skladbot tests.test_backend_api_persistence.BackendApiPersistenceTests.test_sync_sources_starts_skladbot_in_background_by_default tests.test_refresh_fallback.RefreshFallbackTests.test_backend_refresh_forces_google_and_skladbot_sync_before_loading_orders` - 4 теста OK;
  - `.venv/bin/python -m unittest tests.test_backend_api_persistence tests.test_refresh_fallback tests.test_google_sheets_sync_worker` - 30 тестов OK;
  - `.venv/bin/python -m unittest discover -s tests` - 168 тестов OK;
  - `.venv/bin/python -m py_compile backend/app/skladbot_worker.py backend/app/main.py src/taksklad/backend_client.py src/taksklad/main.py` - OK.
- Проверено на VDS:
  - пересобраны `backend-api`, `telegram-worker`, `skladbot-worker`, `google-sheets-sync-worker`;
  - `GET https://api.taksklad.uz/health` вернул `ok`;
  - `POST /api/v1/sync/sources?skladbot=1&wait_skladbot=0` вернул `google_sheets.rows=21`, `matched=21`, `conflicts=0`, `skladbot.status=started`;
  - в логах подтверждено, что при уже идущем `skladbot-worker` ручной backend sync не запускает второй параллельный проход: `SkladBot worker: another sync is already running, skip`.

### Google Sheets Quantity Price Recalculation

- Причина: если менеджер вручную менял в Google Sheets `Кол-во блок`, backend обновлял количество, но мог оставить старую `Сумма позиции` из импортированного заказа.
- Пример проблемы: в заказе было 15 блоков и сумма `3 600 000`, в Google Sheets поставили 1 блок, приложение показало план 1 блок, но сумма осталась `3 600 000`.
- Решение:
  - при Google Sheets -> backend sync сумма позиции пересчитывается как `Кол-во блок * Цена за блок`;
  - если в строке Google нет цены за блок, используется сохранённая цена позиции, затем стандартная цена `240000`;
  - старое значение `Сумма позиции` больше не держит backend в неверном состоянии после изменения количества;
  - если новое количество меньше уже отсканированного, конфликт по количеству по-прежнему блокирует изменение позиции.
- Проверено:
  - `.venv/bin/python -m py_compile backend/app/google_sheets_sync_worker.py` - OK;
  - `.venv/bin/python -m unittest tests.test_google_sheets_sync_worker` - 4 теста OK.
  - `.venv/bin/python -m unittest tests.test_backend_api_persistence tests.test_refresh_fallback tests.test_google_sheets_sync_worker` - 31 тест OK;
  - `.venv/bin/python -m unittest discover -s tests` - 169 тестов OK.
- Проверено на VDS:
  - пересобраны `backend-api` и `google-sheets-sync-worker`;
  - `GET https://api.taksklad.uz/health` вернул `ok`;
  - `POST /api/v1/sync/sources?skladbot=0` вернул `google_sheets.rows=21`, `matched=21`, `conflicts=0`;
  - активный заказ `"NILUFAR SANOBAR" MChJ`: `Chapman RED OP 20`, `blocks=1`, `block_price=240000`, `line_total=240000`.

### SkladBot Recent Request Prefilter

- Причина: при тестовом создании свежей заявки SkladBot номер не появился сразу, хотя заявка полностью совпадала с заказом.
- Диагностика:
  - SkladBot API отвечал;
  - свежая заявка `WH-R-191794` совпадала с заказом `"NILUFAR SANOBAR" MChJ` по дате, клиенту, оплате, товару и блокам;
  - старый worker сначала тянул детали до 100 заявок, включая старые, ловил `429`, и только потом фильтровал кандидатов;
  - из-за этого свежая заявка могла подтянуться сильно позже, а в логах было `requests=0 orders=8 matched=0 not_found=8`.
- Решение:
  - до запроса детальной карточки SkladBot добавлен быстрый фильтр по датам из списка заявок: `created_at`/`createdAt`, `updated_at`/`updatedAt`;
  - старые заявки сразу пропускаются;
  - детали запрашиваются только по заявкам за окно `SKLADBOT_SYNC_LOOKBACK_DAYS`, сейчас это сегодня и вчера;
  - если в списке нет дат, код не отбрасывает заявку заранее и проверяет детали как раньше.
- Эффект:
  - новая заявка не ждёт перебора старых 100 заявок;
  - меньше запросов к SkladBot API;
  - ниже риск `429`;
  - ручное `Обновить` быстрее доводит номер заявки до backend.
- Проверено:
  - `.venv/bin/python -m py_compile backend/app/skladbot_worker.py` - OK;
  - `.venv/bin/python -m unittest tests.test_backend_skladbot_worker` - 13 тестов OK;
  - `.venv/bin/python -m unittest tests.test_backend_skladbot_worker tests.test_backend_api_persistence tests.test_refresh_fallback tests.test_google_sheets_sync_worker` - 44 теста OK;
  - `.venv/bin/python -m unittest discover -s tests` - 170 тестов OK.
- Проверено на VDS:
  - пересобраны `backend-api` и `skladbot-worker`;
  - `GET https://api.taksklad.uz/health` вернул `ok`;
  - `POST /api/v1/sync/sources?skladbot=1&wait_skladbot=1` вернул `skladbot.requests=1`, `matched=1`, `not_found=7`, `multiple=0`;
  - активный заказ `"NILUFAR SANOBAR" MChJ` получил `skladbot_request_number=WH-R-191794`, `skladbot_request_id=191794`.

### Mac Close Telegram Lock Import Fix

- Причина: при закрытии desktop-приложения `on_close()` освобождал Telegram poll lock через `telegram_single_listener_lock_enabled()`, но этот helper не был импортирован в `src/taksklad/main.py`.
- Симптом: окно `Ошибка в интерфейсе` с текстом `name 'telegram_single_listener_lock_enabled' is not defined`.
- Решение:
  - в `src/taksklad/main.py` добавлен импорт `telegram_single_listener_lock_enabled` из `telegram_service`;
  - добавлен тест, который проверяет, что `ScanningApp.on_close` видит этот helper в своих globals;
  - добавлен стабильный PyInstaller entrypoint для mac-сборки, чтобы приложение собиралось из пакета `src/taksklad`, а не из временного файла.
- Проверено:
  - `.venv/bin/python -m py_compile src/taksklad/main.py` - OK;
  - `.venv/bin/python -m unittest tests.test_refresh_fallback` - 7 тестов OK;
  - `.venv/bin/python -m unittest discover -s tests` - 171 тест OK;
  - свежий mac bundle `outputs/mac_ready/TakSklad-2.0.0-mac-ready/TakSklad.app` запускается и держится запущенным без traceback.

### Google Sheets Primary Runtime Sync

- Причина: по утверждённому ТЗ Google Sheets `data` должен быть главным операционным листом, а backend/Postgres - вторичным хранилищем. После ручных правок в `data` приложение должно видеть актуальные данные, а после сканирования/закрытия заказа изменения должны попадать обратно в Google Sheets.
- Решение:
  - desktop `Обновить` сначала синхронизирует очередь backend и запускает backend sync источников, но активные заказы читает из Google Sheets `data`;
  - если Google Sheets недоступен, desktop использует backend как fallback, чтобы склад не вставал полностью;
  - при завершении юрлица desktop после печати переносит строки заказа из `data` в `Архив` и ставит статус `Выполнено`;
  - backend Google Sheets sync теперь читает не только активный `data`, но и `Архив`, подтягивает отсканированные КИЗы, статусы и пересчитанные суммы в Postgres;
  - backend sync больше не игнорирует уже закрытые заказы, потому что архивные строки тоже должны поддерживать базу в актуальном состоянии.
- Риск:
  - если сводка уже напечаталась, но Google Sheets в этот момент недоступен, desktop покажет ошибку архивации. Это лучше, чем молча потерять факт закрытия. Повторная обработка должна проверяться по логам и строкам `data`.
- Проверено:
  - `./.venv/bin/python -m unittest tests.test_refresh_fallback tests.test_backend_telegram_import tests.test_google_sheets_sync_worker` - 35 тестов OK;
  - `./.venv/bin/python -m unittest tests.test_backend_api_persistence tests.test_backend_skladbot_worker tests.test_backend_telegram_import tests.test_google_sheets_sync_worker tests.test_refresh_fallback` - 69 тестов OK;
  - `./.venv/bin/python -m unittest discover -s tests` - 178 тестов OK;
  - `./.venv/bin/python -m compileall -q src/taksklad backend/app tests` - OK.

### Telegram Menu Cleanup And Status Button

- Причина: Telegram-кнопки прикреплялись к каждому сообщению и документу, поэтому клавиатура появлялась навязчиво. По ТЗ кнопки должны быть снизу, открываться через панель Telegram и не мешать обычной отправке Excel-файлов.
- Решение:
  - `sendMessage` и `sendDocument` больше не добавляют клавиатуру автоматически;
  - нижняя клавиатура отправляется явно на `/start`;
  - `is_persistent` выключен, поэтому пользователь может скрыть клавиатуру свайпом/кнопкой Telegram;
  - кнопка `КИЗ по файлам` переименована в `Выгрузка КИЗов`;
  - добавлена кнопка и команда `Статус`, которая берёт `/api/v1/reports/day` и показывает заказы, активные/выполненные, блоки, КИЗы и сумму.
- Проверено:
  - покрыто тестами `tests.test_backend_telegram_import`;
  - общий прогон смежных backend/Telegram/Google sync тестов - 69 тестов OK;
  - полный `unittest discover` - 178 тестов OK.

### Google Sheets Primary Returns

- Причина: возвраты в desktop 2.0 сначала работали только через backend. По ТЗ возвраты должны быть видны и управляться через Google Sheets: поиск в `Архив`, отметка строки, копия в `Возвраты`, backend остаётся вторичным зеркалом.
- Решение:
  - добавлен поиск закрытой заявки в листе `Архив` по `Номер заявки SkladBot`, `ID заявки SkladBot` и `ID заказа`;
  - при принятии возврата строки в `Архив` получают `Статус возврата`, `Дата возврата`, `Основание возврата`, `Принял возврат`;
  - эти же строки копируются в лист `Возвраты`;
  - окно `Возвраты` в desktop теперь сначала работает с Google Sheets, а backend использует только как fallback, если Google недоступен;
  - список последних возвратов тоже читается из `Возвраты`.
- Проверено:
  - `./.venv/bin/python -m unittest tests.test_google_sheets_returns tests.test_refresh_fallback tests.test_google_sheets_sync_worker` - 16 тестов OK;
  - `./.venv/bin/python -m unittest discover -s tests` - 178 тестов OK.

### Backend Mirror For Google Sheets Returns

- Причина: после перехода возвратов на Google Sheets primary backend должен оставаться зеркалом. Иначе desktop уже видит возврат в `Архив`/`Возвраты`, а backend продолжает считать заказ просто completed.
- Решение:
  - backend Google Sheets sync теперь читает колонки `Статус возврата`, `Дата возврата`, `Основание возврата`, `Принял возврат`;
  - если в архивной строке стоит `Возврат`, заказ в Postgres получает статус `returned`;
  - поля возврата сохраняются в `order.raw_payload`: `return_status`, `returned_at`, `return_reference`, `returned_by`;
  - позиции заказа остаются completed, потому что возврат относится к закрытой заявке целиком.
- Проверено:
  - `./.venv/bin/python -m unittest tests.test_google_sheets_sync_worker tests.test_google_sheets_returns` - 9 тестов OK;
  - `./.venv/bin/python -m unittest discover -s tests` - 179 тестов OK;
  - `./.venv/bin/python -m compileall -q src/taksklad backend/app tests` - OK;
  - `git diff --check` - OK.

### Idempotent Google Sheets Archiving

- Причина: после завершения заказа desktop переносит строки из `data` в `Архив`. Если Google Sheets успел добавить строки в `Архив`, но удаление из `data` сорвалось, повторная попытка могла продублировать архивные строки.
- Решение:
  - `archive_order_group_to_gsheet()` теперь перед добавлением проверяет `Архив` по `ID заказа`, `ID импорта` и fallback-ключу заказа;
  - если строка уже есть в `Архиве`, она не добавляется повторно, но исходная строка из `data` всё равно удаляется;
  - добавлены тесты на перенос нескольких строк юрлица и повторную архивацию уже архивированной строки.
- Проверено:
  - `./.venv/bin/python -m unittest tests.test_google_sheets_archive tests.test_google_sheets_returns tests.test_google_sheets_sync_worker` - 11 тестов OK;
  - `./.venv/bin/python -m unittest discover -s tests` - 181 тест OK;
  - `./.venv/bin/python -m compileall -q src/taksklad backend/app tests` - OK;
  - `git diff --check` - OK.

### Google Sheets Price Recalculation On Desktop Refresh

- Причина: при ручной правке `Кол-во блок` в листе `data` desktop видел новое количество, но мог показывать старую `Сумма позиции` из Google Sheets. Это ломало прямую связь `data` -> приложение: количество уже актуальное, сумма ещё старая.
- Решение:
  - при чтении строк `data` desktop пересчитывает `Сумма позиции` от текущего `Кол-во блок` и `Цена за блок`;
  - если `Цена за блок` пустая, используется стандартная цена 240000 сум за блок;
  - если в листе есть колонки `Цена за блок`, `Сумма позиции`, `Сумма рассчитанная`, desktop при обновлении сразу записывает туда пересчитанные значения.
- Проверено:
  - `./.venv/bin/python -m unittest tests.test_google_sheets_desktop_read` - OK;
  - `./.venv/bin/python -m unittest tests.test_google_sheets_desktop_read tests.test_refresh_fallback tests.test_google_sheets_archive tests.test_google_sheets_returns tests.test_google_sheets_sync_worker` - 20 тестов OK;
  - `./.venv/bin/python -m unittest discover -s tests` - 182 теста OK;
  - `./.venv/bin/python -m compileall -q src/taksklad backend/app tests` - OK;
  - `git diff --check` - OK.

### Telegram Command Menu Without Reply Keyboard

- Причина: по ТЗ Telegram-кнопки должны открываться через системную кнопку меню рядом с полем ввода, а не появляться как навязчивая reply-клавиатура после `/start`.
- Решение:
  - `/start` теперь отправляет только инструкцию без `reply_markup`;
  - пользовательские действия остаются в `setMyCommands`: `/date`, `/logistics`, `/kiz_files`, `/status`;
  - `setChatMenuButton` оставляет рядом с полем ввода системную кнопку команд Telegram;
  - выбор даты логистического отчёта переведён на inline-кнопки под сообщением;
  - выбор исходного файла для `Выгрузка КИЗов` переведён на inline-кнопки под сообщением;
  - polling теперь принимает `callback_query`, чтобы inline-кнопки обрабатывались без текстового ввода.
- Проверено:
  - `./.venv/bin/python -m unittest tests.test_backend_telegram_import` - 24 теста OK;
  - `./.venv/bin/python -m unittest tests.test_backend_telegram_import tests.test_backend_api_persistence tests.test_refresh_fallback` - 53 теста OK;
  - `./.venv/bin/python -m unittest discover -s tests` - 184 теста OK;
  - `./.venv/bin/python -m compileall -q src/taksklad backend/app tests` - OK;
  - `git diff --check` - OK.

### KIZ Source File Export By Import Instance

- Причина: кнопка `Выгрузка КИЗов` должна выгружать КИЗы по конкретному загруженному Excel-файлу. Если менеджер загрузит файл с таким же названием повторно, отчёт не должен смешивать старый и новый импорт.
- Решение:
  - backend endpoint `/api/v1/reports/kiz/source-file` принимает `source_key`;
  - `source_key` строится от `backend_import_id` и имени исходного файла, которые сохраняются в `raw_payload` каждой позиции при импорте;
  - Telegram хранит `source_key` в состоянии выбора файла и передаёт его при скачивании отчёта;
  - если `source_key` нет, остаётся legacy fallback по `source_file`.
- Проверка:
  - добавлен тест, где два импорта имеют одинаковое имя файла, но выгрузка доступна только по завершённому конкретному импорту;
  - добавлены проверки Telegram-передачи `source_key`.
  - `./.venv/bin/python -m unittest tests.test_backend_api_persistence tests.test_backend_telegram_import` - 48 тестов OK;
  - `./.venv/bin/python -m unittest discover -s tests` - 187 тестов OK;
  - `./.venv/bin/python -m compileall -q src/taksklad backend/app tests` - OK;
  - `git diff --check` - OK.

### Business Timezone For Backend Day Status

- Причина: кнопка Telegram `Статус` и backend дневной отчёт брали дату по UTC. Для склада в Ташкенте это могло показать не тот день после полуночи по местному времени.
- Решение:
  - добавлен env `TAKSKLAD_TIMEZONE`, дефолт `Asia/Tashkent`;
  - `GET /api/v1/reports/day` без `report_date` теперь берёт бизнес-дату в этой timezone;
  - `scanned_today` считает дату скана по бизнес-timezone;
  - API сохраняет исходный `scanned_at` в `scan_codes.raw_payload`, чтобы не потерять timezone, если DB-драйвер вернул timestamp без offset.
- Проверка:
  - добавлен тест на скан `2026-05-31T20:30:00+00:00`, который должен попасть в отчёт `2026-06-01` по Ташкенту.

### Business Timezone For SkladBot Window

- Причина: SkladBot worker отбирает свежие заявки по окну `сегодня + вчера`. Если сервис на VDS работает в UTC, после полуночи по Ташкенту он мог ещё считать предыдущий день и пропускать свежие заявки текущего бизнес-дня.
- Решение:
  - SkladBot worker использует тот же `TAKSKLAD_TIMEZONE`, дефолт `Asia/Tashkent`;
  - `date_in_window()` без явно переданной даты теперь считает бизнес-сегодня в timezone склада;
  - `TAKSKLAD_TIMEZONE` проброшен в docker-compose для `skladbot-worker`.
- Проверка:
  - добавлен тест, что `2026-05-31T20:30:00+00:00` считается `2026-06-01` в `Asia/Tashkent`;
  - проверен compose/env контракт для VDS.

### SkladBot Timestamp Dates Converted To Business Date

- Причина: SkladBot может отдавать `created_at`/`updated_at` как ISO timestamp с timezone. Простое отрезание даты до `T` превращало `2026-05-31T20:30:00+00:00` в `31.05`, хотя для Ташкента это уже `01.06`.
- Решение:
  - `parse_date()` в SkladBot worker сначала пытается разобрать ISO timestamp;
  - timestamp с `T`, пробелом, offset или `Z` переводится в `TAKSKLAD_TIMEZONE`;
  - дополнительно поддержаны распространённые локальные timestamp-форматы `31.05.2026 20:30:00+0000`, `31.05.2026 20:30`;
  - date-only значения `2026-05-31`, `31.05.2026` продолжают работать как раньше.
- Проверка:
  - добавлен тест на `created_at=2026-05-31T20:30:00+00:00`, `created_at=2026-05-31 20:30:00+00:00` и `created_at=31.05.2026 20:30:00+0000`, которые попадают в окно `01.06` при `lookback_days=0`.

### Release Preflight Aligned With Published 2.0.0 Manifest

- Причина: после публикации `version.json` на `2.0.0` старые acceptance/preflight проверки продолжали требовать закреплённый `1.1.7` без download URL. Это давало ложный `failed`, хотя текущая безопасная фаза уже другая: `2.0.0` опубликован, `mandatory=false`, ссылки и SHA заполнены.
- Решение:
  - `tools/release_preflight.py` теперь проверяет staged rollout manifest: `latest_version=2.0.0`, `min_supported_version=1.1.7`, `mandatory=false`, `package_type=onefile_exe`, заполненные URL и SHA для onefile/onedir;
  - `deploy/vds/acceptance_status.sh` использует те же правила для VDS acceptance status;
  - `tools/build_windows_test_archive.ps1` допускает либо старое безопасное состояние `1.1.7`, либо текущий безопасный non-mandatory rollout `2.0.0`;
  - acceptance kit и GO/NO-GO gate заменили старый пункт `version.json не менялся` на `version.json проверен и mandatory=false`.
- Проверено:
  - `./.venv/bin/python -m unittest tests.test_release_preflight tests.test_vds_acceptance_scripts tests.test_windows_test_build_helper tests.test_release_go_no_go` - 21 тест OK;
  - `./.venv/bin/python tools/release_preflight.py` - `status=ok`;
  - `./.venv/bin/python tools/release_go_no_go.py --results outputs/taksklad_acceptance/ACCEPTANCE_RESULTS.md` - ожидаемо `status=no_go`, потому что ручные Telegram/SkladBot/Windows пункты не закрыты.

### VDS Acceptance Kit Synced To 2.0.0 Staged Rollout

- Причина: локальный acceptance/preflight уже был переведён на staged rollout `2.0.0`, а VDS `/opt/taksklad/app` всё ещё держал acceptance kit и локальный `version.json` в старой фазе `1.1.7`. Из-за этого локальная и серверная проверка описывали разные состояния релиза.
- Перед заменой на VDS создан restore point:
  - `/opt/taksklad/restore_points/pre-acceptance-status-2.0-sync-20260531T193545Z`.
- На VDS синхронизированы:
  - `version.json`;
  - `deploy/vds/acceptance_status.sh`;
  - `tools/release_go_no_go.py`;
  - `outputs/taksklad_acceptance/*`.
- Проверено на VDS:
  - SHA256 синхронизированных файлов совпали с локальными;
  - `./deploy/vds/acceptance_status.sh` - `status=ok`, `version_json.latest_version=2.0.0`, `mandatory=false`, URL/SHA заполнены, контейнеры running, backend health `version=2.0.0`;
  - `./deploy/vds/acceptance_status.sh --require-go` - ожидаемо `status=failed` с причиной `release GO/NO-GO is not go: no_go`, потому что ручные Telegram/SkladBot/Windows пункты ещё не закрыты.

### Update Manifest Download Verification

- Причина: preflight проверял, что `version.json` содержит URL и SHA, но не проверял формат URL/SHA и не умел доказать, что опубликованные GitHub-артефакты реально скачиваются и совпадают с manifest.
- Решение:
  - `tools/release_preflight.py` теперь всегда проверяет, что release URL идут по HTTPS и указывают на тег `v2.0.0`;
  - SHA должны быть lowercase SHA256 hex digest длиной 64 символа;
  - добавлен флаг `--verify-downloads`, который скачивает `TakSklad.exe` и `TakSklad-windows-x64.zip` из `version.json` потоково и сверяет SHA256.
- Проверено:
  - `./.venv/bin/python -m unittest tests.test_release_preflight` - 11 тестов OK;
  - `./.venv/bin/python tools/release_preflight.py` - `status=ok`;
  - `./.venv/bin/python tools/release_preflight.py --verify-downloads --timeout 120` - `status=ok`;
  - фактические SHA совпали:
    - onefile `473910481b55ec5e7ebff386b0549879e754fef70d626e13a614fe5b6e304206`;
    - onedir `0ce088d7c7b9f0d4c3a5dea5965a770da35782a5c65a98969f42eb72ce9dcf4e`.
- Синхронизировано на VDS:
  - перед заменой созданы restore points:
    - `/opt/taksklad/restore_points/pre-release-preflight-download-verify-20260531T194411Z`;
    - `/opt/taksklad/restore_points/pre-windows-test-helper-2.0-rollout-20260531T194452Z`;
  - обновлены `tools/release_preflight.py` и `tools/build_windows_test_archive.ps1`;
  - `python3 tools/release_preflight.py --skip-network` на VDS - `status=ok`;
  - `python3 tools/release_preflight.py --verify-downloads --skip-network --timeout 120` на VDS - `status=ok`, SHA обоих GitHub-артефактов совпали.

### Backend To Google Sheets Immediate Export

- Причина: по утверждённому ТЗ Google Sheets остаётся главным рабочим листом для контроля `data`, `Архив` и `Возвраты`. Backend/Postgres хранит данные и даёт API, но действия через backend не должны оставлять Google Sheets устаревшим до следующего фонового sync.
- Решение:
  - после успешного `POST /api/v1/scans` backend best-effort дописывает КИЗы и статус позиции в строку листа `data`;
  - после успешного `POST /api/v1/orders/{id}/complete` backend best-effort переносит строки заказа из `data` в `Архив`, пишет `Выполнено` и сохраняет КИЗы;
  - после успешного `POST /api/v1/returns/{id}` backend best-effort обновляет строку в `Архив` колонками возврата и копирует её в `Возвраты`;
  - ошибки Google Sheets не откатывают складскую операцию в Postgres, но пишутся в `audit_log` как `google_sheets_scan_export`, `google_sheets_archive_export`, `google_sheets_return_export`.
- Зачем:
  - если операция пришла через backend/web/API, менеджер всё равно видит актуальное состояние в Google Sheets;
  - ручные правки Google Sheets продолжают подтягиваться в backend через существующий `google_sheets_sync_worker`;
  - связь становится двусторонней: Google Sheets -> backend и backend -> Google Sheets.
- Проверено:
  - `./.venv/bin/python -m unittest tests.test_backend_google_sheets_exporter` - 3 теста OK;
  - `./.venv/bin/python -m unittest tests.test_backend_api_persistence tests.test_backend_google_sheets_exporter tests.test_google_sheets_sync_worker` - 35 тестов OK;
  - `./.venv/bin/python -m unittest discover -s tests` - 199 тестов OK;
  - `./.venv/bin/python -m compileall -q backend/app src/taksklad tests tools` - OK;
  - `git diff --check` - OK.
- Доставлено на VDS:
  - перед заменой создан restore point `/opt/taksklad/restore_points/pre-backend-google-sheets-export-20260601T060534Z`;
  - синхронизированы `backend/app/google_sheets_exporter.py` и `backend/app/orders_service.py`;
  - пересобран и перезапущен только `backend-api`, без изменения `version.json` и без push-уведомлений;
  - внутри контейнера `backend-api` выполнен `py_compile` обновлённых файлов;
  - публичный `https://api.taksklad.uz/health` вернул `status=ok`, `version=2.0.0`;
  - VDS `./deploy/vds/acceptance_status.sh` вернул `status=ok`;
  - VDS `python3 tools/release_preflight.py --skip-network` вернул `status=ok`;
  - VDS `./deploy/vds/acceptance_status.sh --require-go` ожидаемо завершился exit `3`: release GO/NO-GO остаётся `no_go` до ручных Telegram/SkladBot/Windows проверок.

### Backend Google Sheets Export Timeout Guard

- Причина: после добавления немедленной обратной записи `backend -> Google Sheets` операции `/scans`, `/complete` и `/returns` начали вызывать Google Sheets из backend API. Без явного timeout медленный Google мог задержать API-ответ и создать ощущение зависания склада.
- Решение:
  - backend Google Sheets exporter теперь использует отдельный `GoogleTimeoutHTTPClient`;
  - timeout задаётся через `TAKSKLAD_GOOGLE_API_TIMEOUT_SECONDS`;
  - значение по умолчанию `8` секунд;
  - некорректное env-значение не ломает импорт модуля, fallback остаётся `8`;
  - timeout проброшен в VDS compose для `backend-api` и `google-sheets-sync-worker`;
  - `.env.example` дополнен `TAKSKLAD_GOOGLE_API_TIMEOUT_SECONDS=8`.
- Зачем:
  - Google Sheets остаётся рабочим контролируемым листом;
  - при временной проблеме Google backend-операция быстрее фиксирует ошибку в audit и не висит бесконечно;
  - складская операция в Postgres остаётся сохранённой.
- Проверено:
  - `./.venv/bin/python -m unittest tests.test_backend_google_sheets_exporter tests.test_backend_api_persistence tests.test_google_sheets_sync_worker tests.test_vds_acceptance_scripts` - 39 тестов OK;
  - `docker compose --env-file deploy/vds/.env.example -f deploy/vds/docker-compose.yml config` - OK;
  - `./.venv/bin/python -m compileall -q backend/app tests` - OK.
- Финальная проверка перед доставкой:
  - `./.venv/bin/python -m unittest discover -s tests` - 200 тестов OK;
  - `./.venv/bin/python -m compileall -q backend/app src/taksklad tests tools` - OK;
  - `git diff --check` - OK.
- Доставлено на VDS:
  - перед заменой создан restore point `/opt/taksklad/restore_points/pre-backend-google-timeout-20260601T062001Z`;
  - синхронизированы `backend/app/google_sheets_exporter.py`, `deploy/vds/docker-compose.yml`, `deploy/vds/.env.example`;
  - пересобраны и перезапущены `backend-api` и `google-sheets-sync-worker`;
  - внутри контейнера `backend-api` подтверждено: `timeout=8`, client `GoogleTimeoutHTTPClient`;
  - публичный `https://api.taksklad.uz/health` вернул `status=ok`, `version=2.0.0`;
  - VDS `./deploy/vds/acceptance_status.sh` вернул `status=ok`;
  - VDS `python3 tools/release_preflight.py --skip-network` вернул `status=ok`.

### SkladBot Numbers Exported Back To Google Sheets

- Причина: backend SkladBot sync мог найти номер заявки и записать его в Postgres, но desktop после кнопки `Обновить` читает Google Sheets `data` как главный рабочий лист. Из-за этого номер мог быть найден backend-ом, но не появиться в приложении и в листе `data`.
- Решение:
  - после SkladBot matching backend best-effort обновляет в Google Sheets `data` служебные колонки:
    - `Номер заявки SkladBot`;
    - `ID заявки SkladBot`;
    - `Статус SkladBot`;
    - `Последняя проверка SkladBot`;
  - обновление идёт по `ID импорта` / `ID заказа`, то есть по тем же ключам, по которым backend связывает строки Google Sheets и Postgres;
  - кнопка desktop `Обновить` теперь вызывает backend `/api/v1/sync/sources` с `wait_skladbot=1`, чтобы сначала дождаться SkladBot sync, затем перечитать Google Sheets;
  - если совпадения нет или их несколько, в `Order.raw_payload.skladbot_nearest` сохраняются ближайшие кандидаты и причины несовпадения `date/client/payment/products`.
- Зачем:
  - связь остаётся двухсторонней: Google Sheets -> backend и backend -> Google Sheets;
  - менеджер видит номер заявки в листе `data`;
  - складское приложение после `Обновить` не читает устаревший Google-лист;
  - при проблеме matching можно понять, какое поле не совпало, без ручного просмотра логов SkladBot.
- Проверено:
  - `./.venv/bin/python -m unittest tests.test_backend_skladbot_worker tests.test_backend_google_sheets_exporter tests.test_refresh_fallback tests.test_backend_api_persistence` - 56 тестов OK;
  - `./.venv/bin/python -m compileall -q backend/app src/taksklad tests/test_backend_skladbot_worker.py tests/test_backend_google_sheets_exporter.py tests/test_refresh_fallback.py` - OK.
- Финальная локальная проверка:
  - `./.venv/bin/python -m unittest discover -s tests` - 203 теста OK;
  - `./.venv/bin/python -m compileall -q backend/app src/taksklad tests tools` - OK;
  - `git diff --check` - OK.
- Доставлено на VDS:
  - перед заменой создан restore point `/opt/taksklad/restore_points/pre-skladbot-google-export-20260601T063838Z`;
  - синхронизированы backend SkladBot/Google exporter, desktop refresh-клиент и документация;
  - пересобраны и перезапущены `backend-api`, `skladbot-worker`, `google-sheets-sync-worker`;
  - при первом live-запуске SkladBot sync найден рассинхрон деплоя: серверный `backend/app/settings.py` был старым и не содержал поля `timezone`;
  - создан restore point `/opt/taksklad/restore_points/pre-backend-settings-timezone-20260601T064248Z`;
  - `backend/app/settings.py` синхронизирован на VDS и сервисы пересобраны повторно;
  - внутри контейнера `backend-api` подтверждено: `load_settings().timezone == Asia/Tashkent`;
  - live `update_orders_from_skladbot()` отработал без падения: `requests=1`, `orders=7`, `matched=0`, `not_found=7`, `multiple=0`;
  - `skladbot_google_sheets_export` в audit: `status=completed`, `updated=20`;
  - публичный `https://api.taksklad.uz/health` вернул `status=ok`, `version=2.0.0`;
  - VDS `./deploy/vds/acceptance_status.sh` вернул `status=ok`;
  - VDS `python3 tools/release_preflight.py --skip-network` вернул `status=ok`.

### Dynamic SkladBot Lookback For Active Orders

- Причина: live-проверка показала, что заявки SkladBot могут быть созданы за несколько дней до текущего запуска. При жёстком окне `сегодня/вчера` backend видел только свежую заявку `WH-R-191813`, а активные заказы на 29.05.2026 оставались `без номера SkladBot`.
- Решение:
  - окно поиска SkladBot теперь расширяется динамически по датам активных заказов без номера заявки;
  - базовое окно остаётся `SKLADBOT_SYNC_LOOKBACK_DAYS=1`;
  - максимальный потолок задаётся `SKLADBOT_SYNC_MAX_LOOKBACK_DAYS`, по умолчанию `7`;
  - запас на создание заявки до даты отгрузки задаётся `SKLADBOT_ORDER_CREATE_LEAD_DAYS`, по умолчанию `3`;
  - детальная загрузка заявок ограничена `SKLADBOT_DETAIL_LIMIT`, по умолчанию `30`;
  - если у всех активных заказов уже есть номер SkladBot, API SkladBot не вызывается;
  - если все активные заказы уже нашли кандидата, детальная загрузка останавливается раньше лимита.
- Зачем:
  - не возвращаться к тяжёлому перебору сотен заявок;
  - подтягивать номера для старых активных партий после тестов или задержек;
  - снизить риск `429` от SkladBot;
  - оставить кнопку desktop `Обновить` быстрой и предсказуемой.
- Проверено локально:
  - `./.venv/bin/python -m unittest tests.test_backend_skladbot_worker` - 21 тест OK;
  - `./.venv/bin/python -m unittest tests.test_backend_skladbot_worker tests.test_backend_google_sheets_exporter tests.test_backend_api_persistence tests.test_refresh_fallback tests.test_google_sheets_sync_worker` - 66 тестов OK;
  - `./.venv/bin/python -m compileall -q backend/app src/taksklad tests tools` - OK;
  - `git diff --check` - OK.
- Live-диагностика до фикса:
  - окно `1` день: `matched=0`, `not_found=7`;
  - ручное расширение до `7` дней находило совпадения для 7 активных заказов, но могло упираться в лимиты SkladBot;
  - поэтому выбран динамический lookback с ранней остановкой, а не постоянный широкий поиск.

### SkladBot Dynamic Lookback Config Contract

- Причина: после перехода на dynamic lookback в коде часть документации и VDS env-пример всё ещё описывали только жёсткое `SKLADBOT_SYNC_LOOKBACK_DAYS=1`. Это создавало риск неправильной настройки при следующем деплое.
- Решение:
  - в `deploy/vds/docker-compose.yml` явно добавлены env:
    - `SKLADBOT_SYNC_MAX_LOOKBACK_DAYS`;
    - `SKLADBOT_ORDER_CREATE_LEAD_DAYS`;
    - `SKLADBOT_DETAIL_LIMIT`;
  - в `deploy/vds/.env.example` добавлены значения по умолчанию `7`, `3`, `30`;
  - `docs/product-mvp-2.0-plan.md`, `docs/project-knowledge-base.md`, `docs/project-architecture.md` обновлены под динамическое окно SkladBot;
  - тест VDS compose/env contract теперь проверяет эти переменные.
- Зачем:
  - VDS-настройки явно совпадают с runtime-логикой worker-а;
  - следующий деплой не вернёт старое представление, что worker всегда смотрит только один день;
  - можно безопасно подстроить потолок окна и лимит деталей без правки кода.

### Telegram Menu Live Command Refresh

- Причина: live-проверка `getMyCommands` на VDS показала, что Telegram всё ещё видел старое меню: `date`, `logistics`, `kiz_files` без команды `status`, а описание `kiz_files` оставалось `КИЗ по файлам`.
- Решение:
  - пользовательская команда `kiz_files` переименована в интерфейсе в `Выгрузка КИЗов`;
  - команда `status` остаётся в пользовательском меню;
  - документация и acceptance checklist обновлены под новое название кнопки;
  - `telegram-worker` нужно пересобрать и перезапустить на VDS, чтобы он заново выполнил `setMyCommands`.
- Зачем:
  - Telegram-кнопки должны соответствовать утверждённому ТЗ и не показывать старые названия;
  - пользователь видит нижнее системное меню команд Telegram, а не навязчивую reply-клавиатуру.
- Проверено:
  - `./.venv/bin/python -m unittest tests.test_backend_telegram_import tests.test_release_go_no_go tests.test_acceptance_excel_generator` - 39 тестов OK;
  - `./.venv/bin/python -m compileall -q backend/app/telegram_worker.py tools/prepare_acceptance_kit.py tests/test_backend_telegram_import.py` - OK;
  - `git diff --check` - OK;
  - VDS `telegram-worker` пересобран и перезапущен;
  - Telegram API `getMyCommands` вернул `date`, `logistics`, `kiz_files`, `status`;
  - описание `kiz_files` теперь `Выгрузка КИЗов`;
  - `getChatMenuButton` вернул `type=commands`.

### Telegram Menu Acceptance Gate

- Причина: старое Telegram-меню было видно только live-проверкой Bot API, а `acceptance_status.sh` этого не ловил.
- Решение:
  - добавлен read-only VDS-скрипт `deploy/vds/verify_telegram_menu.sh`;
  - скрипт проверяет `getMyCommands` и `getChatMenuButton`;
  - ожидаемые команды: `/date`, `/logistics`, `/kiz_files`, `/status`;
  - ожидаемое описание `/kiz_files`: `Выгрузка КИЗов`;
  - `acceptance_status.sh` теперь запускает этот скрипт и добавляет блок `telegram_menu` в JSON-ответ.
- Зачем:
  - если Telegram снова покажет старое меню или потеряет кнопку `Статус`, VDS acceptance сразу станет `failed`;
  - это закрывает регрессию, которую раньше можно было заметить только вручную в Telegram.
- Проверено:
  - на VDS создан restore point `/opt/taksklad/restore_points/pre-telegram-menu-verifier-20260601T075628Z`;
  - `deploy/vds/verify_telegram_menu.sh` синхронизирован на VDS и вернул `status=ok`;
  - live `getMyCommands` вернул `/date`, `/logistics`, `/kiz_files`, `/status`;
  - live описание `/kiz_files` вернуло `Выгрузка КИЗов`;
  - live `getChatMenuButton` вернул `type=commands`;
  - VDS `./deploy/vds/acceptance_status.sh` вернул общий `status=ok` и блок `telegram_menu.status=ok`;
  - `release_go_no_go` внутри acceptance остаётся `no_go`, потому что ручные пункты Telegram import, SkladBot matching и Windows desktop acceptance ещё не отмечены как принятые.

### Release Manifest Safety Wording Update

- Причина: после разрешения обновлять `version.json` и публиковать staged rollout в acceptance kit оставался старый флаг `no_push_notifications`, который больше не соответствует текущей линии 2.0.
- Решение:
  - `version.json` оставлен на `latest_version=2.0.0`, `mandatory=false`, с заполненными download URL и SHA;
  - сообщение `version.json` обновлено с `КИЗ по файлам` на `Выгрузка КИЗов`;
  - acceptance manifest теперь фиксирует `push_notifications_allowed=true` и `mandatory_update_disabled=true`;
  - `acceptance_status.sh` проверяет новые safety-флаги вместо старого `no_push_notifications`;
  - инструкция acceptance kit теперь запрещает только `mandatory=true` до ручного GO и новый Windows release поверх 2.0.0 без повторной проверки.
- Зачем:
  - не держать искусственное ограничение на staged обновления;
  - при этом не включать принудительное обновление рабочих ПК до ручной приёмки.
- Проверено:
  - локально `bash -n deploy/vds/verify_telegram_menu.sh deploy/vds/acceptance_status.sh` - OK;
  - локально `./.venv/bin/python -m unittest tests.test_vds_acceptance_scripts tests.test_backend_telegram_import tests.test_release_go_no_go tests.test_acceptance_excel_generator` - 42 теста OK;
  - локально `./.venv/bin/python -m compileall -q backend/app tests tools` - OK;
  - локально `git diff --check` - OK;
  - VDS `./deploy/vds/verify_telegram_menu.sh` - `status=ok`;
  - VDS `./deploy/vds/acceptance_status.sh` - общий `status=ok`;
  - VDS `version.json.message` содержит `Выгрузка КИЗов`;
  - VDS manifest содержит `push_notifications_allowed=true` и `mandatory_update_disabled=true`.

### Release Preflight Safety Flag Alignment

- Причина: `tools/release_preflight.py` всё ещё требовал старый флаг `no_push_notifications`, хотя acceptance manifest уже перешёл на `push_notifications_allowed=true` и `mandatory_update_disabled=true`.
- Решение:
  - preflight теперь проверяет новые safety-флаги;
  - тестовый fixture `tests/test_release_preflight.py` обновлён под ту же модель;
  - старый `no_push_notifications` больше не участвует в preflight gate.
- Проверено:
  - `./.venv/bin/python -m unittest tests.test_release_preflight tests.test_vds_acceptance_scripts tests.test_acceptance_excel_generator` - 19 тестов OK;
  - `./.venv/bin/python tools/release_preflight.py --skip-network` - `status=ok`;
  - `rg no_push_notifications` по preflight/acceptance runtime-файлам не нашёл старых требований;
  - VDS `python3 tools/release_preflight.py --skip-network` - `status=ok`;
  - VDS `./deploy/vds/acceptance_status.sh` - общий `status=ok`, `release_go_no_go.status=no_go` ожидаемо до ручной приёмки.

### SkladBot Google Sheets Re-Export And Diagnostic Window

- Причина: при тестах мог возникнуть рассинхрон, когда backend уже знает номер заявки SkladBot, но Google Sheets ещё не показывает его. В этом случае worker раньше пропускал SkladBot API и не переэкспортировал уже найденные номера обратно в `data`.
- Решение:
  - если все активные backend-заказы уже имеют номер/ID SkladBot, worker всё равно делает best-effort экспорт этих номеров в Google Sheets;
  - `Статус SkladBot` в Google Sheets теперь пишется человекочитаемо: `Найдено`, `Не найдено`, `Несколько совпадений`, `Ошибка синхронизации`;
  - read-only диагностика SkladBot теперь передаёт активные заказы в `fetch_candidate_requests`, поэтому использует то же динамическое окно дат, что и реальный worker.
- Зачем:
  - Google Sheets остаётся главным видимым источником для менеджера и склада;
  - кнопка `Обновить` и фоновый worker могут восстановить номера в таблице без повторного поиска SkladBot, если backend уже их знает;
  - диагностика теперь честнее объясняет, почему заявка не подтянулась: раньше она могла искать SkladBot только за базовое окно, а worker реально расширял окно по датам активных заказов.
- Проверено:
  - `./.venv/bin/python -m unittest tests.test_backend_skladbot_worker tests.test_backend_google_sheets_exporter tests.test_google_sheets_desktop_read tests.test_google_sheets_sync_worker` - 35 тестов OK;
  - VDS read-only проверка Google Sheets показала: `rows=21`, `numbered_rows=21`;
  - VDS `diagnose_skladbot_match.sh` работает и показывает ближайшие несовпадения по `date`, `client`, `payment`, `products`.

### Release Manifest And Update Notifications Unblocked

- Причина: Антон снял старое ограничение "без `version.json` и без push-уведомлений"; текущий релизный процесс должен работать без этого искусственного стопора.
- Фактическое состояние:
  - публичный `version.json` уже указывает на `latest_version=2.0.0`;
  - GitHub Release assets `TakSklad.exe` и `TakSklad-windows-x64.zip` опубликованы;
  - acceptance manifest содержит `push_notifications_allowed=true`;
  - runtime-флага `no_push_notifications` в preflight/acceptance больше нет.
- Важно:
  - `mandatory=false` оставлен осознанно: это не запрет на обновления, а защита от принудительной блокировки рабочих ПК;
  - принудительное обновление `mandatory=true` включается отдельным решением, когда нужно именно заставить все складские ПК обновиться перед работой.
- Проверено:
  - `./.venv/bin/python tools/release_preflight.py --skip-network` - `status=ok`;
  - `./.venv/bin/python tools/release_preflight.py --verify-downloads --timeout 120` - `status=ok`, SHA обоих GitHub assets совпали с `version.json`;
  - `https://api.taksklad.uz/health` - `status=ok`, `version=2.0.0`;
  - VDS `./deploy/vds/acceptance_status.sh` - общий `status=ok`, `telegram_menu.status=ok`, `push_notifications_allowed=true`.

### Google Sheets Backend Sync Acceptance Gate

- Причина: Google Sheets `data` должен оставаться главным видимым источником для менеджера и склада, а backend не должен silently расходиться с таблицей.
- Решение:
  - добавлен read-only verifier `backend/app/google_backend_sync_diagnostic.py`;
  - на VDS добавлен `deploy/vds/verify_google_backend_sync.sh`;
  - общий `deploy/vds/acceptance_status.sh` теперь проверяет соответствие строк `data` и активных backend-позиций;
  - verifier сравнивает source keys, дату отгрузки, оплату, клиента, адрес, ТП, товар, количество, SkladBot номер/ID/статус и расчёт суммы;
  - verifier получил retry/backoff на Google Sheets `429 Quota exceeded`, чтобы acceptance не падал от краткого лимита API.
- Найденная проблема:
  - verifier поймал реальный рассинхрон: backend держал активную позицию `MEROS OYBEK / Chapman Brown OP 20`, которой уже не было в Google Sheets `data`;
  - до исправления такая позиция могла оставаться видимой в приложении, хотя Google-таблица уже была изменена.
- Исправление:
  - `google_sheets_sync_worker` теперь помечает backend-позицию как `removed_from_google_sheet`, если она пропала из Google Sheets и по ней ещё нет сканов;
  - если позиция пропала из Google Sheets, но уже имеет сканы, backend не скрывает её молча и пишет конфликт в audit;
  - активный API больше не отдаёт позиции со статусом `removed_from_google_sheet`;
  - завершённые заказы, которые ещё видны в `data`, worker дополнительно отправляет в архивный экспорт.
- Проверено:
  - локально `./.venv/bin/python -m unittest tests.test_google_sheets_sync_worker tests.test_google_backend_sync_diagnostic tests.test_backend_api_persistence tests.test_vds_acceptance_scripts` - 44 теста OK;
  - локально `./.venv/bin/python -m compileall -q backend/app/google_sheets_sync_worker.py backend/app/google_backend_sync_diagnostic.py backend/app/orders_service.py tests/test_google_sheets_sync_worker.py` - OK;
  - локально `git diff --check` - OK;
  - VDS `./deploy/vds/verify_google_backend_sync.sh` - `status=ok`, `google_rows=19`, `backend_active_items=19`, `matched_items=19`;
  - VDS `./deploy/vds/acceptance_status.sh` - общий `status=ok`, блок `google_backend_sync.status=ok`.
