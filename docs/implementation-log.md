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

### VDS Acceptance Health Retry

- Причина: после `docker compose up -d` контейнер `backend-api` уже может быть в состоянии `running`, но HTTP `/health` ещё 1-2 секунды не слушает порт. Из-за этого `acceptance_status.sh` мог ложно возвращать `status=failed` сразу после redeploy.
- Решение:
  - `deploy/vds/acceptance_status.sh` делает несколько попыток backend health перед тем, как считать проверку проваленной;
  - параметры вынесены в env: `ACCEPTANCE_HEALTH_ATTEMPTS`, `ACCEPTANCE_HEALTH_RETRY_DELAY_SECONDS`;
  - это не скрывает настоящую ошибку backend: если health не поднялся после всех попыток, acceptance остаётся failed.
- Проверено:
  - локально `bash -n deploy/vds/acceptance_status.sh` - OK;
  - локально `./.venv/bin/python -m unittest tests.test_vds_acceptance_scripts` - 3 теста OK;
  - VDS `./deploy/vds/acceptance_status.sh` - общий `status=ok`, backend health `status=ok`, `google_backend_sync.status=ok`.

### SkladBot Coverage Acceptance Gate

- Причина: для рабочего склада важно, чтобы активные заказы, которые уже видны в backend/desktop, имели номер заявки SkladBot. Раньше это проверялось только вручную через диагностику, но не было отдельного release gate.
- Решение:
  - добавлен read-only verifier `backend/app/skladbot_coverage_diagnostic.py`;
  - добавлен VDS-скрипт `deploy/vds/verify_skladbot_coverage.sh`;
  - `deploy/vds/acceptance_status.sh` теперь включает блок `skladbot_coverage` и падает, если активный видимый заказ не имеет `Номер заявки SkladBot` или `ID заявки SkladBot`;
  - verifier игнорирует позиции, скрытые как `removed_from_google_sheet`, чтобы не считать удалённые из Google строки активным складским долгом.
- Проверено:
  - локально `bash -n deploy/vds/verify_skladbot_coverage.sh deploy/vds/acceptance_status.sh` - OK;
  - локально `./.venv/bin/python -m unittest tests.test_skladbot_coverage_diagnostic tests.test_vds_acceptance_scripts tests.test_release_preflight tests.test_acceptance_excel_generator` - 22 теста OK;
  - VDS `./deploy/vds/verify_skladbot_coverage.sh` - `status=ok`, `active_orders=7`, `numbered_orders=7`, `missing_orders=0`;
  - VDS `./deploy/vds/acceptance_status.sh` - общий `status=ok`, `skladbot_coverage.status=ok`.

### Telegram Status Shows Active Shipment Batches

- Причина: кнопка Telegram `Статус` не должна зависеть только от текущей календарной даты. Если склад сегодня собирает заказы на завтра/послезавтра, менеджеру нужен статус именно активной партии по датам отгрузки.
- Решение:
  - `Статус` по-прежнему показывает дневные показатели по КИЗам;
  - дополнительно worker читает `/api/v1/orders/active`;
  - активные заказы группируются по `Дата отгрузки`;
  - по каждой дате показываются заказы, прогресс блоков, остаток, сумма и количество заказов без номера SkladBot;
  - общий итог активной партии показывает количество заказов, позиций, блоков, остаток, сумму и SkladBot-пробелы.
- Проверено:
  - локально `./.venv/bin/python -m unittest tests.test_backend_telegram_import` - 27 тестов OK;
  - локально `./.venv/bin/python -m compileall -q backend/app/telegram_worker.py tests/test_backend_telegram_import.py` - OK;
  - локально `git diff --check` - OK.

### Public Domain Routing Prepared

- Причина: домен `taksklad.uz` активирован, сайт нужно вынести с `api.taksklad.uz` на нормальные публичные host-ы.
- Решение:
  - backend оставлен на `api.taksklad.uz`;
  - frontend переведён на `taksklad.uz` и `www.taksklad.uz`;
  - VDS `.env` обновлён: `TAKSKLAD_FRONTEND_HOST=taksklad.uz`, `TAKSKLAD_FRONTEND_WWW_HOST=www.taksklad.uz`, `TAKSKLAD_PUBLIC_API_URL=https://api.taksklad.uz`;
  - `TAKSKLAD_CORS_ORIGINS` расширен на `https://taksklad.uz`, `https://www.taksklad.uz`, `https://api.taksklad.uz`;
  - Traefik-router frontend теперь принимает два host-а: основной и `www`;
  - `frontend` и `backend-api` пересозданы на VDS.
- Проверено:
  - `https://api.taksklad.uz/health` - `status=ok`, `version=2.0.0`;
  - прямой routed-test через IP VDS для `taksklad.uz` и `www.taksklad.uz` возвращает frontend-router `401 Basic`, значит серверная маршрутизация готова;
  - текущий DNS: `api.taksklad.uz -> 135.181.245.84`, но `taksklad.uz` и `www.taksklad.uz` ещё смотрят на `91.213.99.99`.
- Блокер:
  - Hostmaster не принял известные пароли от PowerVPS/VMmanager, поэтому DNS A-записи через панель пока не изменены.
- Что нужно в DNS:
  - `taksklad.uz A 135.181.245.84`;
  - `www.taksklad.uz A 135.181.245.84` или CNAME на `taksklad.uz`;
  - `adminer.taksklad.uz A 135.181.245.84`, если нужен доступ к Adminer.

### Google Sheets Write-through Queue

- Цель: оставить Google Sheets `data` главным рабочим источником для склада, а PostgreSQL использовать как кэш, backup, audit, защиту от дублей КИЗ и очередь при временной недоступности Google.
- Что изменено:
  - добавлен модуль `backend/app/google_sheets_pending.py` для очереди повторной записи в Google Sheets;
  - сканы КИЗ, завершение заказа, возвраты и Telegram/Excel import теперь не теряются, если Google Sheets временно недоступен;
  - при ошибке Google операция сохраняется в `pending_events` как `google_sheets_export`;
  - `/api/v1/sync/sources` сначала дожимает pending-записи в Google, затем читает Google Sheets `data` обратно в backend;
  - `google-sheets-sync-worker` делает то же самое в фоне перед каждым чтением таблицы.
- Что это даёт пользователю:
  - кнопка `Обновить` и фоновая синхронизация сначала подтягивают актуальную Google-таблицу;
  - если приложение успело принять скан/завершение, но Google дал timeout, запись не пропадает и будет повторена;
  - после восстановления Google backend сам дописывает отложенные изменения.
- Что уже было и остаётся:
  - завершение заказа переносит строки `data -> Архив`;
  - возвраты идут через `Архив -> Возвраты`;
  - если строка удалена из Google и по ней нет сканов, backend скрывает её из активного списка;
  - если строка удалена/изменена, но уже есть сканы, создаётся audit-конфликт, а данные не скрываются молча.
- Проверено:
  - локально `./.venv/bin/python -m unittest tests.test_backend_api_persistence tests.test_google_sheets_sync_worker tests.test_backend_google_sheets_exporter tests.test_refresh_fallback` - 50 тестов OK;
  - локально `./.venv/bin/python -m compileall -q backend/app/google_sheets_pending.py backend/app/orders_service.py backend/app/imports_service.py backend/app/main.py backend/app/google_sheets_sync_worker.py tests/test_backend_api_persistence.py` - OK;
  - VDS backend-сервисы пересобраны и перезапущены;
  - VDS `./deploy/vds/acceptance_status.sh` - `status=ok`, `google_backend_sync.status=ok`, `field_mismatch_count=0`, `skladbot_coverage.status=ok`, `telegram_menu.status=ok`.
- Отдельно исправлено текущее состояние данных:
  - acceptance нашёл старый рассинхрон по одной позиции: backend видел 2 отсканированных блока, Google Sheets видел 1;
  - чтобы не потерять КИЗ, позиция была один раз принудительно дописана backend -> Google;
  - после этого Google и backend снова совпали.

### Windows Ready Archive 2.0.0

- Цель: выдать готовый Windows-архив приложения с рабочими JSON-файлами внутри пакета.
- Что сделано:
  - обновлён пакет `outputs/windows_ready/TakSklad-2.0.0-win-ready`;
  - рядом с `TakSklad.exe` добавлены рабочие runtime JSON: `credentials.json`, `TakSklad_data.json`, `telegram_settings.json`, `version.json`, `.env.taksklad-vds-2.0.generated.json`;
  - `START_BACKEND.ps1` берёт backend service token из `.env.taksklad-vds-2.0.generated.json`, если файл лежит в архиве;
  - в README пакета зафиксировано, что первый запуск Windows-сборки сам создаёт ярлык `TakSklad` на рабочем столе;
  - пересобран архив `outputs/windows_ready/TakSklad-2.0.0-win-ready.zip`;
  - обновлена внешняя SHA256-сумма `outputs/windows_ready/TakSklad-2.0.0-win-ready.zip.sha256.txt`.
- Проверено:
  - `unzip -t outputs/windows_ready/TakSklad-2.0.0-win-ready.zip` - OK;
  - `shasum -a 256 -c outputs/windows_ready/TakSklad-2.0.0-win-ready.zip.sha256.txt` - OK;
  - состав архива проверен: exe, запускные PowerShell-скрипты и runtime JSON присутствуют.
- Важно:
  - архив содержит рабочие ключи и токены, его нельзя отправлять посторонним.

### Desktop Sync Queue Cleanup

- Причина: на рабочем экране склада появилась техническая строка `Backend: ошибка, очередь 1`. В локальной macOS-сборке лежал старый `order_complete`, который backend уже не мог принять и отвечал `404 Order not found`. Приложение считало это ошибкой и повторяло событие сотни раз.
- Что исправлено:
  - backend-очередь больше не держит бесконечно устаревший `order_complete`, если backend вернул `404 Order not found`;
  - Google-очередь больше не держит бесконечно записи с неретрабельной ошибкой вроде `Не найдена строка заказа для записи кодов`;
  - при backend-refresh теперь также обрабатывается локальная Google-очередь, чтобы старые отложенные записи не висели в интерфейсе;
  - рабочий экран склада больше не показывает технические слова `backend` и `очередь записи`, вместо этого выводится `Синхронизация: OK` или понятное сообщение о временной синхронизации.
- Что очищено:
  - в текущей macOS-сборке `outputs/mac_ready/TakSklad-2.0.0-mac-ready/TakSklad.app/Contents/MacOS/TakSklad_data.json` удалены 4 старые Google pending-записи и 1 устаревший backend pending-event;
  - в корневом `TakSklad_data.json` и Windows-ready JSON pending-очереди проверены, сейчас пустые.
- Проверено:
  - `python -m unittest tests.test_backend_bridge tests.test_pending_store tests.test_desktop_ui_contract tests.test_refresh_fallback tests.test_desktop_diagnostics` - 27 тестов OK;
  - `python -m unittest discover -s tests` - 227 тестов OK;
  - `python -m compileall` по изменённым модулям - OK;
  - macOS-приложение пересобрано через PyInstaller и обновлено в `outputs/mac_ready/TakSklad-2.0.0-mac-ready`;
  - `outputs/mac_ready/TakSklad-2.0.0-mac-ready.zip` пересобран и проверен через `unzip -t`;
  - `outputs/windows_ready/TakSklad-2.0.0-win-ready.zip` пересобран с очищенными JSON и проверен через SHA256.

### Direct EXE Backend Runtime Config

- Причина: складскому ПК не должен быть нужен `START_BACKEND.ps1`. Оператор должен запускать обычный `TakSklad.exe` или ярлык на рабочем столе.
- Что изменено:
  - собранная версия приложения теперь читает `.env.taksklad-vds-2.0.generated.json` рядом с `TakSklad.exe`;
  - если в JSON есть `TAKSKLAD_API_TOKEN`, приложение само включает backend-режим, чтение заказов с VDS и URL `https://api.taksklad.uz`;
  - переменные окружения остаются выше по приоритету, то есть скрипты и ручной запуск всё ещё могут переопределить настройки;
  - локальная разработка из исходников не читает этот JSON автоматически, чтобы тесты и VS Code не включали backend случайно.
- Результат для склада:
  - рабочий запуск должен быть через `TakSklad.exe`;
  - `START_BACKEND.ps1` остаётся только как диагностический/приёмочный helper.
- Проверено:
  - добавлены тесты `tests/test_backend_runtime_config.py`;
  - `python -m unittest tests.test_backend_runtime_config tests.test_startup_check tests.test_backend_bridge tests.test_pending_store tests.test_desktop_ui_contract tests.test_refresh_fallback` - 32 теста OK.
- Важно:
  - чтобы это реально попало в Windows `TakSklad.exe`, нужна новая Windows-сборка через GitHub Actions или Windows-машину.

### Windows Release Import Fix 2.0.1

- Причина: на складском ПК Windows-сборка показала `ModuleNotFoundError: No module named 'taksklad'`. Это ошибка упаковки PyInstaller: exe собрался, но пакет `src/taksklad` не попал в runtime.
- Что изменено:
  - desktop-версия поднята до `2.0.1`, чтобы автообновление отличало исправленный exe от уже опубликованного `2.0.0`;
  - в GitHub Actions Windows build добавлен `--collect-submodules taksklad`;
  - в Windows build добавлен smoke-запуск `TakSklad.exe --smoke-import` для onefile и onedir сборок;
  - если пакет `taksklad` снова не попадёт внутрь exe, GitHub Actions теперь упадёт до публикации артефактов.
- Результат для склада:
  - запуск остаётся обычным: `TakSklad.exe`;
  - PowerShell-скрипты для склада не нужны.
- Релиз:
  - опубликован GitHub Release `v2.0.1`;
  - публичный `version.json` переключён на `latest_version = 2.0.1`;
  - пересобран складской архив `outputs/windows_ready/TakSklad-2.0.1-win-ready.zip`;
  - в архиве нет `.ps1`, есть `TakSklad.exe` и рабочие JSON рядом с ним.
- Проверено:
  - GitHub Actions `Build Windows Release` - success;
  - smoke `TakSklad.exe --smoke-import` прошёл для onefile и onedir;
  - SHA GitHub assets сверены локально;
  - `unzip -t outputs/windows_ready/TakSklad-2.0.1-win-ready.zip` - OK;
  - `shasum -a 256 -c outputs/windows_ready/TakSklad-2.0.1-win-ready.zip.sha256.txt` - OK.

### Hostmaster DNS Root Domain Bind

- Причина: frontend-router на VDS уже готов принимать `taksklad.uz` и `www.taksklad.uz`, но DNS корневого домена всё ещё смотрел на старый IP `91.213.99.99`.
- Что сделано:
  - в Hostmaster DNS Manager изменена запись `taksklad.uz. A` на `135.181.245.84`;
  - `api.taksklad.uz. A` оставлена без изменений, она уже смотрела на `135.181.245.84`;
  - `www.taksklad.uz. CNAME taksklad.uz` оставлена без изменений, после смены корня она ведёт на VDS;
  - `adminer.taksklad.uz` не создавался.
- Проверено:
  - после перезагрузки страницы Hostmaster значение `taksklad.uz. A 135.181.245.84` сохранилось;
  - `dig @ns1.hostmaster.uz taksklad.uz A +short` возвращает `135.181.245.84`;
  - `dig @revers.hostmaster.uz taksklad.uz A +short` ещё возвращает старый `91.213.99.99`, SOA serial вторичного NS отстаёт;
  - публичные резолверы могут временно отдавать старый IP до синхронизации вторичного NS и истечения DNS cache;
  - routed-test через VDS IP для `taksklad.uz` и `www.taksklad.uz` возвращает `401 Basic realm="traefik"`, значит frontend-router на сервере принимает оба host-а;
  - `https://api.taksklad.uz/health` продолжает возвращать `status=ok`.
- Важно:
  - HTTPS-сертификат для `taksklad.uz`/`www.taksklad.uz` ещё не выпущен: пока Traefik отдаёт default certificate;
  - после DNS propagation нужно повторно проверить `dig @1.1.1.1 taksklad.uz A +short`, `curl -I https://taksklad.uz` и сертификат Let's Encrypt для root/www.

### Web Panel Read-Only Table MVP

- Причина: нужна web-панель, из которой можно видеть рабочую таблицу, фильтровать заказы, видеть Google/SkladBot/скан-статусы и активность, но без риска случайно выполнить складское действие из браузера.
- Решение этапа 1:
  - добавлен read-only endpoint `GET /api/v1/admin/table`;
  - endpoint возвращает плоскую таблицу: одна строка = одна позиция заказа;
  - в строке есть дата, клиент, адрес, ТП, оплата, товар, план/факт/остаток блоков, сумма, SkladBot номер/статус, Google sync status, источник файла, pending Google exports;
  - в ответ добавлены totals и recent audit activity;
  - текущий `/api/v1/orders/active` не менялся, чтобы не ломать desktop/Telegram;
  - frontend переведён в read-only web panel: убраны UI-действия записи КИЗов и завершения заказа из браузера;
  - добавлены фильтры по дате отгрузки, статусу, сканам, SkladBot, Google и строковый поиск.
- Что сознательно не добавлено:
  - нет web-сканирования КИЗов;
  - нет завершения заказа из web;
  - нет удаления/архивации/отмены на этапе 1;
  - безопасные action endpoints (`archive-without-kiz`, `cancel`, `resync-google`) оставлены на этап 2 после отдельной auth/audit/precondition-логики.
- Проверено:
  - `./.venv/bin/python -m unittest tests.test_backend_api_persistence` - 29 тестов OK;
  - `./.venv/bin/python -m unittest discover -s tests` - 232 теста OK;
  - `npm run build` во `frontend` - OK;
  - `python -m compileall` по изменённым backend/test файлам - OK;
  - `git diff --check` - OK;
  - `frontend/src` проверен на отсутствие старых write-действий `createScan`, `completeOrder`, `POST`, `Записать`, `Завершить`.

### Web Panel Safe Actions MVP

- Причина: web-панели нужна аварийная управляемость без ломки складского сценария. Типовой пример - единоразово закрыть активные заказы без КИЗов, если их нельзя сканировать, но нельзя превращать это в обычное завершение заказа.
- Что добавлено:
  - `POST /api/v1/admin/orders/{order_id}/archive-without-kiz`;
  - `POST /api/v1/admin/orders/{order_id}/cancel`;
  - `POST /api/v1/admin/orders/{order_id}/resync-google`;
  - `POST /api/v1/admin/google/pending/retry`;
  - request body `AdminOrderActionRequest`: reason, actor, idempotency_key, expected_updated_at, dry_run.
- Защита данных:
  - archive-without-kiz и cancel разрешены только для активного заказа без отсканированных КИЗов;
  - действие пишет audit log и причину в `raw_payload`;
  - заказ и его позиции получают отдельные статусы `archived_no_kiz` или `cancelled`;
  - эти статусы не входят в `COMPLETED_STATUSES`, поэтому не считаются обычным выполнением заказа и не доступны как основание возврата;
  - активная выдача `/api/v1/orders/active` больше не показывает `archived_no_kiz` и `cancelled`.
- Google Sheets:
  - обычный `Архив` оставлен только для реально завершенных заказов;
  - заказы без КИЗов переносятся в отдельный лист `Архив без КИЗов`;
  - отмененные заказы переносятся в отдельный лист `Отмененные`;
  - если Google временно недоступен, событие попадает в server-side pending queue и повторяется через retry.
- Frontend:
  - добавлен выбор заказа чекбоксом в web-таблице;
  - action-bar показывает выбранный заказ, план/факт блоков и Google-очередь;
  - доступны действия: ресинк Google, архив без КИЗов, отмена, повтор Google-очереди;
  - опасные действия требуют reason и confirm;
  - web-сканирование КИЗов и обычное завершение заказа в браузер не возвращались.
- Проверено:
  - `./.venv/bin/python -m unittest tests.test_backend_api_persistence tests.test_backend_google_sheets_exporter` - 40 тестов OK;
  - `./.venv/bin/python -m unittest discover -s tests` - 238 тестов OK;
  - `npm run build` во `frontend` - OK;
  - `python -m compileall` по изменённым backend-файлам - OK.
- Доставлено на VDS:
  - перед заменой создан restore point `/opt/taksklad/restore_points/pre-web-safe-actions-20260601T184438Z`;
  - синхронизированы `backend/`, `frontend/`, `deploy/vds/` без серверного `.env`;
  - дополнительно синхронизирован `version.json`, потому что на VDS оставался старый manifest `2.0.0`, а текущая рабочая линия `2.0.1`;
  - пересобраны и перезапущены `backend-api`, `google-sheets-sync-worker`, `frontend`;
  - Postgres volume и данные не трогались.
- Проверено на VDS:
  - `https://api.taksklad.uz/health` вернул `status=ok`;
  - внутри `backend-api` выполнен `py_compile` изменённых backend-файлов;
  - `GET /api/v1/admin/table` внутри контейнера вернул `rows=114`, `active_orders=0`, `pending_google_exports=0`;
  - проверено наличие новых admin routes;
  - routed-test `https://taksklad.uz/` через IP VDS возвращает `401 Basic`, frontend-router отвечает;
  - `./deploy/vds/acceptance_status.sh` вернул `status=ok`.

### Web Login Entry MVP

- Причина: после привязки `taksklad.uz` к VDS нужен нормальный вход в web-панель, а не Traefik BasicAuth и не открытая таблица.
- Архитектурное решение:
  - frontend стал публичной страницей входа;
  - реальные API-данные за `/api/` закрыты nginx `auth_request`;
  - nginx сначала проверяет web-cookie через `GET /api/v1/auth/check`;
  - только после валидной web-сессии nginx добавляет внутренний service token к запросам backend;
  - пароль не хранится во frontend, на VDS лежит только PBKDF2-хеш в `.env`;
  - web-сессия хранится в `HttpOnly`, `Secure`, `SameSite=Lax` cookie.
- Backend:
  - добавлены `POST /api/v1/auth/login`, `POST /api/v1/auth/logout`, `GET /api/v1/auth/session`, `GET /api/v1/auth/check`;
  - добавлен HMAC session token с TTL;
  - добавлен простой rate limit на неверные попытки входа;
  - существующие service-token API не открывались наружу.
- Frontend:
  - добавлен экран входа TakSklad с рабочим оформлением;
  - после входа открывается web-панель с таблицей, фильтрами, безопасными действиями и активностью;
  - logout очищает сессию и возвращает на экран входа.
- Deploy:
  - перед заменой создан restore point `/opt/taksklad/restore_points/pre-web-login-entry-20260601T191258Z`;
  - синхронизированы `backend/`, `frontend/`, `deploy/vds/` без вывода секретов;
  - серверный `.env` обновлен web-auth параметрами;
  - пересобраны и перезапущены `backend-api` и `frontend`;
  - Traefik BasicAuth снят с frontend-router, потому что защиту API теперь выполняет web-cookie gate.
- Проверено:
  - `curl -I https://taksklad.uz/` возвращает `200 text/html`;
  - `GET https://taksklad.uz/api/v1/admin/table` без cookie возвращает `401`;
  - login возвращает `200` и выставляет cookie с `HttpOnly`, `Secure`, `SameSite=Lax`;
  - `GET /api/v1/admin/table` с cookie возвращает `200`;
  - после logout тот же endpoint снова возвращает `401`;
  - `https://api.taksklad.uz/health` возвращает `status=ok`;
  - `https://api.taksklad.uz/docs` и `/openapi.json` снаружи возвращают `404`;
  - `./deploy/vds/acceptance_status.sh` на VDS вернул общий `status=ok`.

### Web Login Fix: same-origin API and HTTPS hardening

- Причина: после первого деплоя пользователь видел `Не защищено` в Chrome и форма входа показывала ошибку на корректные данные.
- Что найдено:
  - backend auth на VDS корректно принимает рабочие данные через `https://taksklad.uz/api/v1/auth/login`;
  - парольный hash в контейнере не поврежден: формат PBKDF2 корректный;
  - публичный сертификат `taksklad.uz` валиден, Let's Encrypt, SAN содержит `taksklad.uz` и `www.taksklad.uz`;
  - `http://taksklad.uz/` уже редиректит на `https://taksklad.uz/`;
  - вероятная причина Chrome `Не защищено` - старый DNS/cache после смены IP с `91.213.99.99` на `135.181.245.84`;
  - реальная причина ошибки входа в web UI - frontend был собран с `VITE_TAKSKLAD_API_URL=https://api.taksklad.uz` и мог уходить напрямую на backend host, минуя same-origin nginx web-gate.
- Исправление:
  - frontend больше не использует `VITE_TAKSKLAD_API_URL` для web-панели;
  - frontend больше не читает старый `taksklad-web-config` из `localStorage`;
  - все web-запросы идут только в same-origin `/api` на текущем host;
  - добавлен `Strict-Transport-Security: max-age=31536000; includeSubDomains`;
  - в Traefik labels добавлен явный HTTP-router для frontend с permanent redirect на HTTPS.
- Доставлено на VDS:
  - синхронизированы `frontend/` и `deploy/vds/` без серверного `.env`;
  - пересобран и перезапущен `frontend`;
  - `backend-api` был пересоздан docker compose во время `up -d --build frontend`, env и данные не менялись.
- Проверено:
  - новый bundle `index-Pkuib_xb.js` не содержит `https://api.taksklad.uz`;
  - `curl -sIL http://taksklad.uz/` возвращает `308` на `https://taksklad.uz/`, затем `200`;
  - `curl -I https://taksklad.uz/` возвращает `Strict-Transport-Security`;
  - login через `https://taksklad.uz/api/v1/auth/login` возвращает `200`;
  - cookie выставляется с `HttpOnly`, `Secure`, `SameSite=Lax`;
  - `GET /api/v1/admin/table` с cookie возвращает `200`;
  - `GET /api/v1/admin/table` без cookie возвращает `401`;
  - `./deploy/vds/acceptance_status.sh` на VDS вернул общий `status=ok`.

### Excel Import Address Fix: repeated coordinates and placeholder addresses

- Причина: два Excel-файла из Telegram не подтянули адреса в Google `data`, хотя координаты в файлах были.
- Файлы:
  - `Шаблон_отправки_заказов_на_склад_01_06_2026_2ч.xlsx`;
  - `Шаблон_отправки_заказов_на_склад_01_06_2026_1ч.xlsx`.
- Что найдено:
  - в обоих файлах нет адресной колонки, адрес должен получаться только через reverse geocode по координатам;
  - в SmartUp/`Конструктор отчетов` заголовок `Координаты клиента` повторяется несколько раз: широта, долгота и полная пара;
  - backend-импорт раньше выбирал первую одноименную колонку, где лежит только широта, поэтому координаты считались некорректными;
  - значения вроде `Адрес не найден` раньше считались реальным адресом, поэтому reverse geocode не запускался;
  - в файле `2ч` две строки содержат `Самовывоз` без числовых координат, их нельзя геокодировать автоматически.
- Исправление:
  - backend importer теперь выбирает координатную колонку с полной парой `lat,lon`;
  - если полной пары нет, importer собирает координаты из соседних колонок широта + долгота;
  - desktop importer получил ту же логику, чтобы ручной импорт не расходился с Telegram/VDS;
  - `Адрес не найден`, `Адреса не найдены`, `Адрес не определен`, `Адрес отсутствует` и `Координаты: ...` считаются отсутствующим адресом;
  - backend/Google backfill теперь может заменять такие заглушки нормальным адресом.
- Перед изменением данных:
  - создан Postgres backup `/opt/taksklad/backups/postgres/taksklad-postgres-20260602T061135Z.sql.gz`;
  - создан restore point `/opt/taksklad/restore_points/pre-excel-address-geocode-fix-20260602T061151Z`;
  - в restore point сохранен снимок Google `data` на 88 строк.
- Деплой:
  - обновлены `backend-api`, `telegram-worker`, `google-sheets-sync-worker`;
  - Postgres volume не трогался;
  - реальные строки обновлялись только повторным импортом тех же двух Excel-файлов.
- Результат повторного импорта:
  - `2ч`: 38 строк распознаны как дубли, новых позиций 0, backend address updates 14, Google updated 36, две строки без координат остались без адреса;
  - `1ч`: 49 строк распознаны как дубли, новых позиций 0, backend address updates 24, Google updated 49;
  - Google pending queue после операции: pending 0.
- Проверено:
  - dry-run `2ч`: 38 rows, 36 coordinate rows, 2 bad addresses;
  - dry-run `1ч`: 49 rows, 49 coordinate rows, 0 bad addresses;
  - Google `data`: `1ч` 49/49 адресов заполнены, `2ч` 36/38 адресов заполнены;
  - backend: `1ч` 24 заказа без пропусков адреса, `2ч` 15 заказов, 1 заказ без адреса из-за самовывоза;
  - `https://api.taksklad.uz/health` вернул `status=ok`;
  - локально `./.venv/bin/python -m unittest discover -s tests` - 244 tests OK;
  - `git diff --check` - OK.
- Важно:
  - `./deploy/vds/acceptance_status.sh` после появления активных заказов вернул failure только по SkladBot coverage: 39 активных заказов без номера SkladBot;
  - Google/backend sync при этом вернул `status=ok`, matched items 87, field mismatches 0.

### Desktop Release 2.0.1: Mac update lock fix and ready archives

- Причина: старая macOS-сборка была собрана как `2.0.0`, а публичный `version.json` уже отдавал `latest_version=2.0.1`. После согласия на обновление macOS-сборка пыталась использовать Windows-only updater, он падал, а интерфейс оставался заблокированным через `update_required`.
- Что изменено:
  - в desktop update mixin добавлена проверка поддерживаемой платформы;
  - на macOS автообновление теперь не запускается и не ставит блокировку, а показывает неблокирующее сообщение о ручной установке свежего архива;
  - добавлен unit-тест на этот сценарий;
  - macOS `.app` пересобрана как `2.0.1`;
  - macOS bundle metadata обновлена до `CFBundleShortVersionString=2.0.1`;
  - macOS PyInstaller entrypoint получил `--smoke-import`;
  - Windows-ready archive `2.0.1` пересобран с корректной внутренней SHA для `TakSklad/TakSklad.exe`.
- Готовые архивы:
  - `outputs/windows_ready/TakSklad-2.0.1-win-ready.zip`;
  - `outputs/mac_ready/TakSklad-2.0.1-mac-ready.zip`.
- Проверено:
  - `outputs/mac_ready/TakSklad-2.0.1-mac-ready/TakSklad.app/Contents/MacOS/TakSklad --smoke-import` - OK;
  - `shasum -a 256 -c outputs/mac_ready/TakSklad-2.0.1-mac-ready.zip.sha256.txt` - OK;
  - `unzip -t outputs/mac_ready/TakSklad-2.0.1-mac-ready.zip` - OK;
  - `shasum -a 256 -c outputs/windows_ready/TakSklad-2.0.1-win-ready.zip.sha256.txt` - OK;
  - `unzip -t outputs/windows_ready/TakSklad-2.0.1-win-ready.zip` - OK;
  - Windows-ready zip не содержит `.ps1`;
  - Windows-ready zip содержит `TakSklad.exe` и рабочие JSON рядом с ним;
  - внутренний checksum `checksums/TakSklad.exe.sha256.txt` совпадает с фактическим exe внутри архива;
  - `./.venv/bin/python tools/release_preflight.py --verify-downloads --timeout 120` - `status=ok`;
  - `./.venv/bin/python -m compileall -q src/taksklad backend/app tools main.py tests` - OK;
  - `./.venv/bin/python -m unittest discover -s tests` - 245 tests OK.

### Desktop Release 2.0.2: Windows PyInstaller packaging correction

- Причина: Windows-ready zip `2.0.1` оказался недействительным для склада. На чистом Windows-компьютере `TakSklad.exe` падал с `ModuleNotFoundError: No module named 'taksklad'`.
- Что найдено:
  - локальный `outputs/windows_ready/TakSklad-2.0.1-win-ready.zip` был собран из старого сломанного onedir-артефакта;
  - опубликованные GitHub assets `v2.0.1` также не содержали `taksklad.main`;
  - старый workflow smoke-тест мог проходить ложно, потому что запускался из checkout-папки с исходниками.
- Исправлено:
  - версия поднята до `2.0.2`;
  - Windows workflow собирает через `pyinstaller_entry.py`;
  - для сборки выставлен `PYTHONPATH=src`;
  - корневой bridge-пакет `taksklad` временно отключается на Windows runner, чтобы PyInstaller брал настоящий пакет из `src/taksklad`;
  - smoke-тест onefile и onedir теперь запускается из чистых временных папок без исходников проекта;
  - публичный `version.json` переведен на `v2.0.2`;
  - macOS bundle пересобран с metadata `2.0.2`.
- Готовый архив для склада:
  - `outputs/windows_ready/TakSklad-2.0.2-win-ready.zip`.
- Готовый архив для macOS:
  - `outputs/mac_ready/TakSklad-2.0.2-mac-ready.zip`.
- Проверено:
  - GitHub Actions `v2.0.2` прошел clean-dir smoke для onefile и onedir;
  - скачанный `TakSklad-windows-x64.zip` имеет SHA256 `7a1a4afd41b6f2f9adf1c9cc5ac3e075ef68539fea77c490feacaa1c25d1e1ed`;
  - публичный onefile `TakSklad.exe` имеет SHA256 `55b37759e9ce876e393de86eef800885b45a4fcf199046c2ac36081308d5610b`;
  - новый ready zip целый, SHA256 `2c2498e57e628bd37b3cb1ae32a22b332ad44e94b2c29cfd0bd668775e0e28a1`;
  - внутренний `TakSklad/TakSklad.exe` имеет SHA256 `87e1637d527879899aba71b94d486a86e745b36aebdfce038de1a43b8d960849`;
  - Mac ready zip целый, SHA256 `f8590b8393cd663d478f90211ff9c3e9c012c22ff4c7adea659c55af8ef56f00`;
  - Mac bundle executable имеет SHA256 `24b84da64e0b28fbffdc83353c593d644b976ddc199d20bf0dd70dfbba18f271`;
  - `TakSklad.app --smoke-import` - OK;
  - `CFBundleShortVersionString` и `CFBundleVersion` равны `2.0.2`;
  - внутри `TakSklad.exe` есть `taksklad.main` и `taksklad.excel_normalizer`;
  - ready zip содержит JSON рядом с exe и не содержит `.ps1`;
  - релизные unit-тесты прошли.
- Важно:
  - Windows `2.0.0` и `2.0.1` не использовать;
  - для склада выдавать только `TakSklad-2.0.2-win-ready.zip`.

### Web HTTPS hardening for taksklad.uz

- Причина: Chrome показывал `Не защищено` при открытии `taksklad.uz`, хотя сертификат Let's Encrypt был действительным. Риск был не в сертификате, а в том, что web-контур не был жестко защищен от HTTP/mixed-content и API не отдавал полный набор security headers.
- Что проверено:
  - `taksklad.uz`, `www.taksklad.uz`, `api.taksklad.uz` указывают на VDS `135.181.245.84`;
  - HTTP для корневого домена перенаправляется на HTTPS;
  - сертификаты Let's Encrypt действительны;
  - frontend bundle не содержит hardcoded `http://taksklad.uz`, `http://api.taksklad.uz` или `https://api.taksklad.uz`;
  - frontend использует same-origin API через `/api/...`.
- Исправлено:
  - nginx frontend теперь отдает `Strict-Transport-Security`, `Content-Security-Policy`, `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`;
  - CSP включает `upgrade-insecure-requests` и `block-all-mixed-content`;
  - nginx proxy больше не передает backend значение `$scheme` от внутреннего HTTP между Traefik и контейнером, а фиксирует `X-Forwarded-Proto=https`;
  - Traefik middleware `taksklad-security-headers` добавлен на frontend, backend API и adminer-router;
  - отдельный Traefik CSP middleware добавлен на frontend-router.
- Деплой:
  - перед заменой создан restore point на VDS: `/opt/taksklad/restore_points/pre-web-https-security-20260602T080353Z`;
  - обновлены `frontend/nginx.conf.template` и `deploy/vds/docker-compose.yml`;
  - пересобраны и пересозданы `frontend` и `backend-api`;
  - случайно поднятый во время recreate `adminer` сразу остановлен и удален, постоянно запущенными остались только рабочие web/backend контейнеры.
- Проверено после деплоя:
  - `http://taksklad.uz/` возвращает `308` на `https://taksklad.uz/`;
  - `https://taksklad.uz/` возвращает `200` и security headers;
  - `https://www.taksklad.uz/` возвращает `200` и security headers;
  - `https://api.taksklad.uz/health` возвращает `200` и security headers;
  - серверный acceptance-smoke: backend health OK, compose running OK, Google/backend sync OK;
  - общий `acceptance_status.sh` сейчас остается `failed` только по не связанным с HTTPS пунктам: 23 активных заказа без номера SkladBot и незакрытые ручные GO/NO-GO чекбоксы релиза;
  - локально `docker compose --env-file deploy/vds/.env.example -f deploy/vds/docker-compose.yml config` - OK;
  - локально `./.venv/bin/python -m unittest discover -s tests` - 247 tests OK;
  - `git diff --check` - OK.
- Остаточный риск:
  - если Chrome продолжит показывать старый индикатор сразу после исправления, вероятная причина - старая вкладка/cache/HSTS state браузера после смены домена и DNS. Серверная часть уже отдает HTTPS и защитные заголовки.

### Mac ready archive 2.0.2: _struct runtime fix

- Причина: запуск `outputs/mac_ready/TakSklad-2.0.2-mac-ready/START_BACKEND.command` на macOS падал до старта приложения:
  - `[PYI-...:ERROR] Module object for struct is NULL!`;
  - `ModuleNotFoundError: No module named '_struct'`.
- Что найдено:
  - проблема была в macOS PyInstaller runtime внутри готового `.app`, а не в backend-настройках и не в Google/складской логике;
  - прямой smoke старого ready-приложения `TakSklad.app/Contents/MacOS/TakSklad --smoke-import` воспроизводил тот же `_struct` crash;
  - `.venv/bin/pyinstaller` в локальной среде имел старый shebang на `/Users/anton/Documents/work/pKIS/.venv/bin/python`, поэтому пересборка выполнялась только через `./.venv/bin/python -m PyInstaller`.
- Исправлено:
  - macOS `.app` пересобрана из `TakSklad.spec` через `./.venv/bin/python -m PyInstaller`;
  - старая сломанная `.app` заменена в `outputs/mac_ready/TakSklad-2.0.2-mac-ready`;
  - `START_BACKEND.command` и `START_LOCAL.command` в ready-пакете теперь передают аргументы в приложение, чтобы можно было проверять именно скриптовый путь запуска через `--smoke-import`;
  - `build_manifest.json` и `README_INSTALL_RU.md` обновлены новым SHA;
  - `TakSklad-2.0.2-mac-ready.zip` пересобран без `.DS_Store`, `__MACOSX` и runtime-лога `TakSklad.log`;
  - `.sha256.txt` пересчитан.
- Готовый архив:
  - `outputs/mac_ready/TakSklad-2.0.2-mac-ready.zip`;
  - SHA256 zip: `d407b0d7f1fbb8bee23e8c6c52becbd33ba39ecf7b881ac175e0d3e43cfb8340`;
  - SHA256 bundle executable: `cff30d8b68638d63751a7792b6b8e6a666123a29e3b1e4fc2622952aba02f36b`.
- Проверено:
  - `TakSklad.app/Contents/MacOS/TakSklad --smoke-import` - OK;
  - `START_BACKEND.command --smoke-import` - OK;
  - `START_LOCAL.command --smoke-import` - OK;
  - чистая распаковка zip в `/tmp` и запуск `START_BACKEND.command --smoke-import` - OK;
  - чистая распаковка zip в `/tmp` и запуск `START_LOCAL.command --smoke-import` - OK;
  - `unzip -t outputs/mac_ready/TakSklad-2.0.2-mac-ready.zip` - OK;
  - `cd outputs/mac_ready && shasum -a 256 -c TakSklad-2.0.2-mac-ready.zip.sha256.txt` - OK;
  - `codesign --verify --deep --strict` для `.app` - OK;
  - в zip есть рабочие JSON рядом с `.app`;
  - в zip нет `.DS_Store`, `__MACOSX`, `TakSklad.log`.

### KIZ reset and scan-flow fixes for 03.06.2026 orders

- Причина: на складском ПК появились связанные проблемы:
  - `Синхронизация: временная ошибка` из-за повторяющегося backend `order_complete` при недосканированном заказе;
  - заказ мог пропасть из desktop-списка, если одна позиция была выполнена, а у другой в Google оставался stale-статус `Выполнено`;
  - печать показывала окно, но фактическое задание могло уходить в неправильную/невалидную очередь принтера;
  - КИЗы могли некорректно обрабатываться из-за GS1-разделителя и разбиения ячейки по запятым.
- Live reset по просьбе оператора:
  - целевая дата: `03.06.2026` (по `02.06.2026` КИЗов в Google не было);
  - Google backup: `outputs/live_backups/2026-06-02-kiz-reset/`;
  - Postgres full dump: `outputs/live_backups/2026-06-02-kiz-reset/postgres_full_dump.sql`;
  - Google `data`: сброшено 85 строк на `Не выполнено`, КИЗы очищены;
  - Google `Архив`: 2 строки за `03.06.2026` возвращены в `data` без КИЗов и удалены из архива;
  - backend: удалено 39 `scan_codes`, сброшено 87 позиций и 39 заказов на `not_completed`;
  - после reset: Google `data` по `03.06.2026` - 85 строк, КИЗов 0, выполненных строк 0; backend - КИЗов 0, completed позиций 0, completed заказов 0.
- Backend deploy:
  - перед заменой создан restore point на VDS: `/opt/taksklad/restore_points/pre-kiz-reset-fixes-20260602T100141Z`;
  - на VDS доставлены `backend/app/google_sheets_sync_worker.py`, `backend/app/google_sheets_exporter.py`, `backend/app/schemas.py`, `backend/app/orders_service.py`;
  - пересобраны и перезапущены `backend-api` и `google-sheets-sync-worker`;
  - `https://api.taksklad.uz/health` - OK.
- Исправления в коде:
  - добавлена единая нормализация/валидация КИЗов: GS1 `\x1d` разрешен, пробелы/таб/переносы запрещены;
  - desktop и backend больше не режут КИЗ по запятой;
  - запись КИЗов в Google идет через `RAW`;
  - `get_today_orders` исправляет stale `Выполнено -> Не выполнено`, если план КИЗов не набран;
  - desktop после завершения убирает из локального списка только фактически завершенные row numbers, а не всю группу целиком;
  - перед `order_complete` desktop повторно ставит scan-события всех кодов текущего заказа в backend queue;
  - backend queue удаляет `order_complete` с `409 Order has incomplete required items` как бизнес-блокировку, а не как вечную временную ошибку;
  - UI показывает `Синхронизация: заказ недосканирован` для такого случая;
  - печать больше не подменяет сохраненный принтер первым из списка, Windows-печать проверяет `PrinterSettings.IsValid` и логирует stdout/stderr.
- Проверено:
  - целевые тесты: 48 tests OK;
  - полный локальный прогон: `./.venv/bin/python -m unittest discover -s tests` - 260 tests OK;
  - VDS backend health OK;
  - контроль Google/backend после перезапуска worker-а: КИЗов по `03.06.2026` нет;
  - финальный контроль после возврата 2 архивных строк в работу: 2 backend-позиции восстановлены из `removed_from_google_sheet` в `not_completed`, `verify_google_backend_sync.sh` вернул `status=ok`, 167 Google rows matched, mismatches 0.
- Windows release:
  - версия desktop поднята до `2.0.3`;
  - создан release/tag `v2.0.3`;
  - GitHub Actions `Build Windows Release` прошел onefile и onedir clean-dir smoke-tests;
  - GitHub onefile SHA256: `1ecc311f01513bc1a234a00a9e9eb4ea94d31b2b88c426a28be7b7394f986430`;
  - GitHub onedir zip SHA256: `b1ef3fb2428642445935b41d141419f64b616372d51a59582975d8107d95f939`;
  - публичный `version.json` переведен на `2.0.3`, staged rollout, `mandatory=false`;
  - готовый складской архив: `outputs/windows_ready/TakSklad-2.0.3-win-ready.zip`;
  - ready zip SHA256: `95f4aa64ac4a79f8d2da0aea77637b70c38272be62949c43ccbe12771cfa3899`;
  - `TakSklad.exe` внутри ready zip SHA256: `52387c51a089e166772997044388caf88985a9ddc2bfc452c22c1947353eddd6`;
  - ready zip содержит JSON рядом с exe и не содержит `.ps1`;
  - internal `TakSklad/version.json` внутри ready zip указывает `app_version=2.0.3`, `release_tag=v2.0.3`.

### Web login and frontend stability fix

- Причина: после пересоздания `backend-api` frontend nginx продолжал проксировать `/api/...` в старый Docker IP backend-контейнера. Поэтому `https://taksklad.uz/api/v1/auth/login` возвращал `502`, а UI ошибочно показывал это как неверный телефон/пароль.
- Исправлено:
  - nginx frontend использует Docker DNS resolver `127.0.0.11` и proxy через переменную `$taksklad_backend`, чтобы не держать старый IP backend после рестартов;
  - web UI различает `401`, `429`, `5xx` и не маскирует server/proxy failure под неправильный пароль;
  - web-панель закреплена на same-origin `/api`, устаревший `VITE_TAKSKLAD_API_URL` удален из Docker/compose;
  - login layout выровнен на широком и мобильном экране;
  - web-таблица получила фиксированные колонки, sticky header и обрезку длинных клиентов/адресов/товаров.
- Деплой:
  - перед заменой создан restore point на VDS: `/opt/taksklad/restore_points/pre-web-login-nginx-fix-20260602T105937Z`;
  - пересобран и пересоздан `frontend`; финальный деплой выполнен с `--no-deps`, без пересоздания backend/Postgres.
- Проверено:
  - `https://taksklad.uz/api/v1/auth/session` без cookie - `200 authenticated=false`;
  - `https://taksklad.uz/api/v1/admin/table` без cookie - `401`;
  - login через `https://taksklad.uz/api/v1/auth/login` - `200`, cookie ставится;
  - `admin/table` с cookie - `200`;
  - logout очищает cookie, `admin/table` снова `401`;
  - `http://taksklad.uz/` редиректит на HTTPS, `https://taksklad.uz/` отдает HSTS/CSP;
  - `https://api.taksklad.uz/health` - OK;
  - локально `npm run build` - OK;
  - локально `./.venv/bin/python -m unittest discover -s tests` - 260 tests OK.

### MVP 2.0 operational stabilization after first live scan

- Причина: первый боевой прогон показал, что Google Sheets нельзя держать в горячем пути сканирования. При лимитах Google запись КИЗов тормозила склад, отмена последнего КИЗа становилась ненадежной, а обратная синхронизация Google -> backend могла помечать активные позиции как `removed_from_google_sheet`.
- Архитектурное решение:
  - Postgres/VDS становится рабочим source of truth для сканов, завершений, сбросов и статусов;
  - Google Sheets остается рабочим окном и проекцией, но запись в него идет через очередь pending events;
  - обратный sync Google -> backend по умолчанию выключен через `TAKSKLAD_GOOGLE_TO_BACKEND_SYNC_ENABLED=false`;
  - если строка пропала из Google, backend больше не удаляет позицию сам, а пишет audit-конфликт.
- Backend:
  - импорт Excel коммитится в Postgres и только ставит экспорт в Google-очередь;
  - сканы, завершение заказа, возвраты, сброс заказа и восстановление заказа не ждут прямой записи Google;
  - Google export queue защищена lock/advisory lock и `FOR UPDATE SKIP LOCKED`;
  - добавлены админ-действия: reset/rescan, restore, resync SkladBot, Google projection queue;
  - Telegram import дедуплицируется по `update_id`/`file_id` и забирает только `pending` события;
  - SkladBot worker больше не пишет Google напрямую, а сохраняет Postgres и ставит Google-проекцию в очередь.
- Desktop:
  - завершить заказ можно только когда все позиции реально отсканированы и сохранены;
  - недосканированный заказ не должен исчезать из списка из-за частично выполненной позиции;
  - отмена последнего КИЗа умеет обновлять queued/Google state;
  - печать не должна скрывать недосканированный заказ как завершенный.
- Web:
  - добавлены действия reset/rescan, restore, resync SkladBot, Google sync и audit log;
  - login state сбрасывается только при реальном `401`, а не при временном `5xx`/proxy/API сбое.
- SkladBot:
  - временно дефолт `SKLADBOT_DETAIL_LIMIT` был поднят с `30` до `500`, чтобы worker не обрывался на первых заявках боевого дня; follow-up ниже вернул актуальный лимит `30`;
  - динамическое окно по датам отгрузки сохранено.
- Проверено локально:
  - `./.venv/bin/python -m unittest discover -s tests` - 280 tests OK;
  - `PYTHONPATH=src ./.venv/bin/python -m py_compile src/taksklad/*.py backend/app/*.py` - OK;
  - `npm run build` - OK.

### Desktop/Web critical follow-up fixes for VDS-first workflow

- Причина: независимая QA-проверка показала, что часть старого workflow всё ещё держала Google как primary:
  - desktop refresh в backend-режиме сначала читал Google;
  - desktop сохранял позиции и архивировал через Google напрямую;
  - отмена сохранённого КИЗа удаляла только локальное pending-событие, но не откатывала уже принятый VDS scan;
  - web-login проходил, но admin endpoints могли требовать Bearer token и сбрасывать web-session;
  - Google exporter склеивал старые коды из Google с кодами из VDS, из-за чего reset/rescan мог оставить stale-КИЗы.
- Исправлено:
  - `/api/v1` теперь принимает либо service Bearer token, либо валидную web httpOnly cookie;
  - desktop в `TAKSKLAD_BACKEND_READ_ORDERS_ENABLED` режиме читает список из VDS, а Google использует только как аварийный fallback;
  - desktop сохранение позиции в VDS-режиме синхронно ждёт принятия backend queue; если backend не принял КИЗы, позиция не считается сохранённой;
  - добавлен backend endpoint `POST /api/v1/scans/undo`, который удаляет scan code, пересчитывает `scanned_blocks`, возвращает позицию в `not_completed`, пишет audit и ставит Google projection в очередь;
  - desktop undo сохранённого КИЗа вызывает backend undo, а не только чистит локальную очередь;
  - завершение заказа в desktop теперь печатает до backend complete: если печать не прошла, VDS-заказ остаётся активным;
  - desktop больше не делает прямой Google archive для VDS-заказов: backend complete сам ставит Google archive projection;
  - Google exporter теперь заменяет КИЗы в строке состоянием из VDS, а restore/reset projection обновляет существующую строку вместо silent duplicate skip;
  - SkladBot resync больше не стирает старый номер заявки до успешной работы worker-а;
  - web reset/rescan заблокирован для возвратов, счётчик Google queue по выбранному заказу не завышается суммированием одинаковых row-level значений.
- Проверено локально:
  - целевые тесты backend/desktop/web/exporter - 83 tests OK;
  - `./.venv/bin/python -m unittest discover -s tests` - 286 tests OK;
  - `PYTHONPATH=src ./.venv/bin/python -m py_compile src/taksklad/*.py backend/app/*.py` - OK;
  - `npm run build` - OK;
  - локальный web screen `http://127.0.0.1:5173/` открыл login layout и основные блоки.

### Web bulk archive, SkladBot throttling and Chapman reconcile

- Причина: после боевого дня нужно было убрать риск массовых ручных действий по одному заказу, вернуть безопасный SkladBot detail-limit и сверить VDS/Google с двумя оригинальными Excel Chapman за `03.06.2026`.
- Backend/web:
  - добавлен `POST /api/v1/admin/orders/bulk/complete-without-kiz`;
  - действие закрывает выбранные активные заказы как `completed`, ставит `google_sheets_archive_export` и работает одной транзакцией;
  - если хотя бы один заказ не активный, имеет сканы или pending Google export, вся пачка отклоняется;
  - web-таблица получила кнопку `Выделить все` для видимых после фильтров заказов и действие `В архив как выполнено`;
  - admin dashboard totals теперь считаются по всем строкам, а не по обрезанному `limit`.
- SkladBot:
  - `SKLADBOT_DETAIL_LIMIT` возвращен к безопасной модели и после live-429 выставлен на `3`;
  - свежие заявки сортируются выше старых по `updated_at/created_at`, чтобы маленький лимит не застревал на старом списке;
  - на VDS выставлен `SKLADBOT_REQUEST_DELAY_SECONDS=20`, чтобы detail-запросы не ловили регулярный 429.
- Данные VDS:
  - перед серверными изменениями создан restore point `/opt/taksklad/restore_points/pre-skladbot-web-bulk-reconcile-20260602T185920Z`;
  - Postgres backup: `/opt/taksklad/backups/postgres/taksklad-postgres-20260602T185921Z.sql.gz`;
  - restore drill прошел OK;
  - добавлен guarded-инструмент `tools/reconcile_chapman_orders.py`;
  - dry-run по двум оригинальным Excel: `87/87` строк найдены в Postgres, `missing_backend=0`;
  - найдено одно расхождение: `"ALCODRINK" MCHJ`, файл `2ч`, строка 25, `Chapman RED OP 20` было `2` блока вместо `1`;
  - точечно исправлено в Postgres: `20 шт/2 блока/480000` -> `10 шт/1 блок/240000`, без удаления КИЗов;
  - Google projection обработан, повторная сверка: `field_mismatches=0`;
  - старая orphan Google pending-задача для завершенного заказа WINTERFELL закрыта как `obsolete`, текущая Google queue: `0`.
- VDS deploy:
  - синхронизированы backend/frontend/compose изменения;
  - пересобраны и запущены `backend-api`, `frontend`, `skladbot-worker`, `telegram-worker`, `google-sheets-sync-worker`;
  - `https://api.taksklad.uz/health` возвращает OK;
  - login через `https://taksklad.uz/api/v1/auth/login` с рабочими данными возвращает `200`, admin table с cookie возвращает `200`.
- Проверено локально:
  - `./.venv/bin/python -m unittest tests.test_backend_api_persistence tests.test_backend_skladbot_worker tests.test_vds_acceptance_scripts` - 78 tests OK;
  - `./.venv/bin/python -m unittest discover -s tests` - 290 tests OK;
  - `./.venv/bin/python -m compileall -q backend/app src/taksklad tools/reconcile_chapman_orders.py` - OK;
  - `npm run build` - OK.

### Chapman transfer totals data repair for 03.06.2026

- Причина: итог по двум оригинальным Excel-файлам Chapman за `03.06.2026` по типу оплаты `Перечисление` должен быть `39` клиентов/заказов, `87` позиций и `395` блоков, но VDS считал `392` из-за двух позиций ALCODRINK со статусом `removed_from_google_sheet`.
- Backup перед правками:
  - VDS order/items backup: `outputs/backups/alcodrink_restore_backup_20260602T195726Z.json`;
  - Google ALCODRINK rows backup: `outputs/backups/google_alcodrink_rows_backup_20260602T200101Z.json`;
  - Google BABILOV rows backup: `outputs/backups/google_babilov_rows_backup_20260602T200626Z.json`.
- Исправлено:
  - в VDS восстановлены две позиции `"ALCODRINK" MCHJ`: `Chapman Brown OP 20` на `1` блок и `Chapman Gold SSL 100\`20` на `2` блока;
  - статус восстановленных позиций выставлен `not_completed`, дата заказа нормализована на `2026-06-03`;
  - в Google `data` оставлены 3 корректные строки ALCODRINK, удалены 2 дубля после restore projection;
  - в Google `Архив` восстановлена отсутствующая строка `"BABILOV RASHID" MChJ`, `Chapman Brown OP 20`, `2` блока.
- Финальная сверка VDS vs Google `data + Архив`:
  - VDS: `39` клиентов/заказов, `87` позиций, `395` блоков;
  - Google: `39` клиентов, `87` позиций, `395` блоков;
  - разбивка совпадает: Brown `208`, Gold `86`, RED `101`;
  - `missing_by_import_count=0`, `extra_by_import_count=0`, pending Google exports `0`.

### Google data cleanup and web action UX fix

- Причина: после боевого дня в Google `data` оставались активные строки, псевдопустые строки со статусом/SkladBot-колонками и часть неподтянутых SkladBot-номеров; в web reset/rescan требовал причину, а bulk-кнопка `В архив как выполнено` серела на заказах со сканами.
- Backup перед data-maintenance:
  - Postgres backup: `/opt/taksklad/backups/postgres/taksklad-postgres-20260602T201802Z.sql.gz`;
  - Google sheets backup на VDS: `/opt/taksklad/backups/google_sheets/google_sheets_maintenance_backup_20260602T201855Z.json`;
  - локальная копия: `outputs/backups/google_sheets_maintenance_backup_20260602T201855Z.json`.
- Google/VDS data-maintenance:
  - все активные VDS-заказы за `03.06.2026` переведены в `completed`;
  - Google `data -> Архив` выполнен пакетно, чтобы не упираться в Google read quota;
  - удалены псевдопустые строки `data`, где были только статус/SkladBot-колонки без бизнес-данных;
  - `data` после cleanup содержит только заголовок;
  - `Архив` за `03.06.2026`: `190` позиций, `955` блоков, все со статусом `Выполнено`;
  - разбивка `03.06.2026`: `Перечисление` - `87` позиций / `395` блоков, `Терминал` - `103` позиции / `560` блоков;
  - pending Google exports: `0`;
  - активных VDS-заказов за дату: `0`.
- SkladBot:
  - SkladBot API во время диагностики начал отдавать `429`, расширенный диагностический проход остановлен, чтобы не забивать API;
  - в архиве осталось `11` строк по `5` заказам без подтвержденного SkladBot-номера; VDS по этим заказам также хранит `skladbot_status=error`, `skladbot_error=sync_incomplete`;
  - одна старая архивная строка MADINA была дозаполнена SkladBot-номером из VDS.
- Web/backend UX:
  - `reset/rescan` больше не требует ввода причины в web и backend;
  - `AdminOrderActionRequest.reason` и `AdminBulkOrderActionRequest.reason` стали необязательными;
  - bulk `В архив как выполнено` больше не блокируется на полностью отсканированных заказах;
  - частично отсканированные позиции по-прежнему блокируют bulk-закрытие, чтобы не закрывать дырявые заказы случайно.
- VDS deploy:
  - обновлены и пересозданы `backend-api` и `frontend`;
  - `https://api.taksklad.uz/health` - OK;
  - web login и `admin/table` с cookie - `200`.
- Проверено:
  - `./.venv/bin/python -m unittest tests.test_backend_api_persistence` - 46 tests OK;
  - `./.venv/bin/python -m unittest discover -s tests` - 291 tests OK;
  - `./.venv/bin/python -m compileall -q backend/app src/taksklad` - OK;
  - `npm run build` - OK.

### Desktop 2.0.4 finalization for warehouse rollout

- Причина: перед решающим складским днем нужно выдать новую Windows-сборку с накопленными исправлениями scan/undo/finish/print/backend queue и убрать пугающий красный статус синхронизации при временной очереди.
- Desktop:
  - версия поднята до `APP_VERSION=2.0.4`;
  - `Синхронизация: временная ошибка` больше не показывается красным, если событие осталось в очереди и будет отправлено повторно;
  - новый спокойный статус: `Синхронизация: ожидает повторной отправки`;
  - реальные блокировки процесса остаются заметными: `Синхронизация: заказ недосканирован`;
  - случай `failed` без pending-очереди остается красным как `Синхронизация: нужна проверка`.
- Проверено локально:
  - `./.venv/bin/python -m unittest tests.test_desktop_ui_contract tests.test_backend_bridge tests.test_pending_store tests.test_desktop_pending_store tests.test_google_error_messages tests.test_printing` - 38 tests OK;
  - `./.venv/bin/python -m unittest discover -s tests` - 291 tests OK;
  - `./.venv/bin/python -m compileall -q backend/app src/taksklad` - OK;
  - `npm run build` - OK;
  - `./.venv/bin/python tools/release_preflight.py --skip-network` - status OK.
- Windows release:
  - создан tag/release `v2.0.4`;
  - GitHub Actions `Build Windows Release` run `26880027531` завершился success;
  - пройдены smoke-test onefile и onedir: `TakSklad.exe --smoke-import` из чистых папок;
  - официальный `TakSklad.exe` SHA256: `4902982669798eb8e7bc982ccf793a7a202d9aa3a2520c4cc51d6cd31a59c0c7`;
  - официальный `TakSklad-windows-x64.zip` SHA256: `c9f6eb8bcbe7767b3c56e966dc472e86c6760c3c7a4aadbb25871be181a49ebd`;
  - готовый складской архив: `outputs/windows_ready/TakSklad-2.0.4-win-ready.zip`;
  - ready archive SHA256: `03babd3c55b8dfd6815fecdec563f00a94297c2a061d644e2e3209ccf548d4d1`;
  - ready archive содержит рядом с `TakSklad.exe`: `.env.taksklad-vds-2.0.generated.json`, `TakSklad_data.json`, `credentials.json`, `version.json`.

### Google address backfill from VDS

- Причина: в Google `data` после импорта остались адреса вида `Координаты: ...`, хотя VDS уже хранил нормальные адреса после геокодирования.
- Backup перед правкой Google:
  - `/opt/taksklad/backups/google_sheets/google_sheets_address_backfill_backup_20260603T112520Z.json`.
- Разовая правка данных:
  - обновлено `92` строки в Google `data`;
  - неоднозначных совпадений не было;
  - после проверки строк с адресом `Координаты: ...` в `data`: `0`.
- Код:
  - `update_missing_sheet_addresses()` теперь сначала обновляет адрес по `ID заказа`/`ID импорта`;
  - если ID изменились между импортами, добавлен fallback по строгому бизнес-ключу: дата, тип оплаты, клиент, торговый, товар, штуки и блоки;
  - fallback применяется только для пустых/технических адресов и пропускает неоднозначные совпадения;
  - `all_rows` обновляется в памяти после backfill, чтобы следующий duplicate-check не добавлял дубль.
- Проверено:
  - `./.venv/bin/python -m unittest tests.test_backend_api_persistence` - 48 tests OK;
  - `./.venv/bin/python -m unittest tests.test_backend_google_sheets_exporter tests.test_google_sheets_sync_worker` - 20 tests OK;
  - `./.venv/bin/python -m unittest discover -s tests` - 293 tests OK;
  - `./.venv/bin/python -m compileall -q backend/app src/taksklad` - OK.

### SkladBot cursor sync and pending status fix

- Причина: SkladBot API ограничивает частые запросы `429`, а VDS worker с `SKLADBOT_DETAIL_LIMIT=3` мог каждый цикл проверять только малую часть карточек. Неполный проход массово записывался как `skladbot_status=error`, из-за чего сайт показывал `Ошибка`, хотя фактически синхронизация просто ждала следующий проход.
- Backend:
  - `skladbot-worker` теперь сохраняет `last_checked_request_id` в audit payload и следующий цикл начинает после него;
  - маленький лимит `SKLADBOT_DETAIL_LIMIT=3` сохранён, но worker проходит список заявок порциями, а не застревает на одном наборе;
  - при `detail_limit_reached` заказы получают статус `pending`, а не `error`;
  - `format_skladbot_status()` показывает `pending` как `Проверяется`;
  - Google-export больше не затирает уже существующий номер/ID SkladBot пустым значением, если backend пришёл без номера.
- Frontend:
  - web-панель показывает `pending` как `Проверяется`;
  - фильтр проблем SkladBot включает `pending`, чтобы такие строки было легко найти.
- VDS:
  - restore point перед деплоем: `/opt/taksklad/restore_points/pre-skladbot-cursor-fix-20260603T121226Z`;
  - пересобраны и перезапущены `backend-api`, `skladbot-worker`, `google-sheets-sync-worker`, `frontend`;
  - runtime `SKLADBOT_WORKER_INTERVAL_SECONDS` исправлен с `600` на `60`, `SKLADBOT_DETAIL_LIMIT=3` оставлен.
- Проверено:
  - `./.venv/bin/python -m unittest tests.test_backend_skladbot_worker tests.test_backend_google_sheets_exporter tests.test_google_sheets_sync_worker` - 53 tests OK;
  - `./.venv/bin/python -m unittest discover -s tests` - 297 tests OK;
  - `./.venv/bin/python -m compileall -q backend/app src/taksklad` - OK;
  - `npm run build` - OK;
  - `https://api.taksklad.uz/health` - OK.

### Active `Перечисление` transfer date correction

- Причина: активные заказы `Перечисление`, которые фактически должны идти на `05.06.2026`, были загружены с датой `03.06.2026`. Это мешало SkladBot matching, потому что дата отгрузки является частью бизнес-сопоставления.
- Backup перед правкой:
  - Postgres: `/opt/taksklad/backups/postgres/taksklad-postgres-20260603T125657Z.sql.gz`;
  - Google: `/opt/taksklad/backups/google_sheets/google_sheets_before_transfer_date_fix_20260603T125658Z.json`.
- Разовая правка данных:
  - VDS: обновлено `33` заказа, `79` позиций, `285` блоков с `03.06.2026` на `05.06.2026`;
  - Google `data`: обновлено `79` строк, `285` блоков;
  - активных `Перечисление` на `03.06.2026` после проверки: `0`.
- Важно:
  - `order_key`, `item_key`, `business_line_key` не пересчитывались, чтобы не потерять связь с уже импортированными строками и pending-очередями;
  - SkladBot matching должен работать по обновлённой дате `05.06.2026`.

### Desktop 2.0.5 backend scan finish idempotency fix

- Причина: в `TakSklad (63).log` версия `2.0.4` успешно сохраняла КИЗы, но при завершении заказа падала с ошибкой `Сводный лист напечатан, но backend не принял все КИЗы. Осталось в очереди: 3`.
- Корень:
  - desktop при backend-завершении повторно ставил уже сохранённые КИЗы в backend-очередь;
  - backend проверял `item already fully scanned` раньше проверки существующего такого же КИЗа;
  - повтор того же самого кода по уже завершённой позиции возвращал `409`, поэтому очередь не схлопывалась.
- Backend:
  - `create_scan()` теперь сначала проверяет существующий `ScanCode`;
  - повтор того же КИЗа по той же позиции считается идемпотентным даже после полного скана позиции;
  - чужой дубль по другой позиции/заказу по-прежнему блокируется.
- Desktop:
  - backend-завершение больше не переочередит КИЗы перед `sync_pending_backend_events()`;
  - backend-режим завершения больше не читает Google для сводки, если данные уже есть в текущем заказе;
  - Google-only режим перед печатью проверяет активный `429` backoff и не запускает печать, пока Google на паузе.
- VDS:
  - restore point перед деплоем: `/opt/taksklad/restore_points/pre-204-scan-idempotency-fix-20260603T131632Z`;
  - Postgres backup: `/opt/taksklad/backups/postgres/taksklad-postgres-20260603T131635Z.sql.gz`;
  - пересобраны и перезапущены `backend-api`, `skladbot-worker`, `google-sheets-sync-worker`, `frontend`;
  - `https://api.taksklad.uz/health` - OK.
- Release:
  - desktop version поднята до `2.0.5`;
  - создан tag/release `v2.0.5`;
  - GitHub Actions `Build Windows Release` run `26888232768` завершился success;
  - smoke-test `TakSklad.exe --smoke-import` прошёл в GitHub Actions;
  - официальный `TakSklad.exe` SHA256: `4b8eded617a21abe1de8717027dd08cde87e0182f327bf314932cf0c045b2733`;
  - официальный `TakSklad-windows-x64.zip` SHA256: `190ad3acbaf8d16224a87b4bd9936f453008fad25dfcf95f110b2bb2b8577a24`;
  - `version.json` обновлён на `2.0.5`, rollout остаётся `mandatory=false`;
  - готовый складской архив: `outputs/windows_ready/TakSklad-2.0.5-win-ready.zip`;
  - ready archive SHA256: `915eb30983b62f9260a555e4f2419dab6f260b478bcf55763b1added75284484`;
  - ready archive содержит рядом с `TakSklad.exe`: `.env.taksklad-vds-2.0.generated.json`, `TakSklad_data.json`, `credentials.json`, `version.json`.
- Проверено:
  - `./.venv/bin/python -m unittest discover -s tests` - 299 tests OK;
  - `./.venv/bin/python -m compileall -q backend/app src/taksklad` - OK;
  - `npm run build` - OK;
  - `git diff --check` - OK.
  - `./.venv/bin/python tools/release_preflight.py --verify-downloads` - download/SHA checks OK; до коммита `version.json` единственный fail был ожидаемый `version.json has local git diff`.

### SkladBot API token pool failover

- Причина: SkladBot API снова начал ограничивать запросы `429`, из-за чего номера заявок подтягивались слишком медленно. Увеличивать `SKLADBOT_DETAIL_LIMIT` нельзя, потому что раньше большой пакет деталей уже давал ошибку.
- Дополнительно найдено: Google-export мог застрять в статусе `busy`, потому что session-level PostgreSQL advisory lock оставался на idle pooled connection после `commit`. Для PostgreSQL глобальный advisory lock убран, обработка pending-событий опирается на уже существующий `SELECT ... FOR UPDATE SKIP LOCKED`.
- Дополнительно по Google `429`: pending-очередь теперь останавливается на первом rate limit, возвращает событие в `pending` и не помечает пачку записей как `failed`.
- Backend:
  - `skladbot-worker` поддерживает пул токенов через `SKLADBOT_API_TOKENS`;
  - при `429` конкретный токен уходит в cooldown, worker переключается на следующий токен;
  - при `401/403` конкретный токен отключается до перезапуска worker-а;
  - при временном `5xx` от SkladBot API worker делает короткую паузу перед повтором, чтобы не забивать detail endpoint;
  - ошибки SkladBot санитизируются, токены не попадают в payload/log;
  - количество попыток теперь покрывает весь пул токенов, поэтому 10-й токен реально достижим даже при стандартном `SKLADBOT_API_MAX_RETRIES`.
- VDS:
  - создан restore point `/opt/taksklad/restore_points/pre-skladbot-token-pool-20260603T141057Z`;
  - в `.env` добавлен пул из `10` SkladBot API-токенов без вывода значений в логи;
  - `SKLADBOT_DETAIL_LIMIT=3` оставлен;
  - `SKLADBOT_WORKER_INTERVAL_SECONDS=60` оставлен;
  - `SKLADBOT_REQUEST_DELAY_SECONDS` снижен с `20` до `2`, чтобы цикл из 3 деталей занимал секунды, а не минуту;
  - пересобран и перезапущен `skladbot-worker`.
- Проверено:
  - `./.venv/bin/python -m unittest tests.test_backend_skladbot_worker` - 43 tests OK;
  - `./.venv/bin/python -m unittest tests.test_backend_google_sheets_pending` - 2 tests OK;
  - `./.venv/bin/python -m unittest discover -s tests` - 312 tests OK;
  - `./.venv/bin/python -m compileall -q backend/app` - OK;
  - `docker compose --env-file deploy/vds/.env.example -f deploy/vds/docker-compose.yml config` - OK;
  - VDS после деплоя: SkladBot details идут с `200 OK`, без `429`; после ускорения цикла pending начал снижаться по `3` совпадения за цикл; выявленная серия `5xx` от SkladBot API обработана дополнительной защитной паузой в коде.

### Desktop 2.0.6 scanning UX, Telegram KIZ by date, Google batch mirror

- Причина: после боевого теста приложение сканирования работает стабильно, но нужны короткие UX/операционные правки:
  - в текущем заказе юрлицо и текущий SKU должны быть заметнее для склада;
  - последний клик `ЗАВЕРШИТЬ ЗАКАЗ` не должен требовать второго нажатия после сохранения последней позиции;
  - Telegram-выгрузка КИЗов должна строиться с VDS по дате отгрузки, а не по локальным сменам разных ПК или исходным Excel-файлам;
  - Google Sheets как зеркало должен догонять VDS быстрее и не тратить quota на один полный проход по листу на каждый КИЗ.
- Desktop:
  - `APP_VERSION` поднята до `2.0.6`;
  - в карточку текущей позиции добавлены отдельные крупные labels для юрлица и SKU;
  - на последней позиции кнопка `ЗАВЕРШИТЬ ЗАКАЗ` после сохранения КИЗов автоматически продолжает завершение и печать;
  - порядок безопасности сохранён: сводный лист печатается до финального backend-complete, чтобы при ошибке печати заказ не закрывался преждевременно.
- Backend/Telegram:
  - добавлены endpoints `GET /api/v1/reports/kiz/dates`, `/api/v1/reports/kiz/date`, `/api/v1/reports/kiz/range`;
  - Telegram-кнопка `Выгрузка КИЗов` теперь показывает даты отгрузки из VDS;
  - добавлены команды `/kiz 05.06.2026` и `/kiz 04.06.2026 05.06.2026`;
  - старый отчет по исходному файлу оставлен как совместимый технический путь.
- Google mirror:
  - `google_sheets_scan_export` теперь обрабатывается batch-ом: несколько scan-событий читают Google `data` один раз и пишутся одним batch update;
  - `ensure_import_sheet_layout()` больше не пишет заголовок в Google, если он уже совпадает;
  - архив/возвраты/отмены не схлопывались, потому что для них важен порядок операций.
- VDS:
  - restore point: `/opt/taksklad/restore_points/pre-kiz-date-google-batch-20260603T175506Z`;
  - Postgres backup: `/opt/taksklad/backups/postgres/taksklad-postgres-20260603T175506Z.sql.gz`;
  - обновлён `backend/app`;
  - пересобраны и перезапущены `backend-api`, `telegram-worker`, `google-sheets-sync-worker`;
  - `https://api.taksklad.uz/health` - OK;
  - новый endpoint `/api/v1/reports/kiz/dates` проверен с service-token на VDS.
- Наблюдение после деплоя:
  - pending Google снизился примерно с `200` до `170` после первого batch-прохода;
  - Google снова вернул `429` по read quota, worker корректно остановил batch до следующего цикла;
  - состав очереди после деплоя: `pending scan 82`, `pending archive 56`, `pending skladbot 1`, `failed scan 23`, `failed archive 9`, `processing scan 1`.
- Проверено:
  - `./.venv/bin/python -m unittest tests.test_desktop_ui_contract tests.test_backend_telegram_import tests.test_backend_google_sheets_pending tests.test_backend_api_persistence` - 96 tests OK;
  - `./.venv/bin/python -m unittest discover -s tests` - 313 tests OK;
  - `./.venv/bin/python -m py_compile src/taksklad/main.py backend/app/kiz_reports_service.py backend/app/main.py backend/app/telegram_worker.py backend/app/google_sheets_exporter.py backend/app/google_sheets_pending.py` - OK;
  - `docker compose --env-file deploy/vds/.env.example -f deploy/vds/docker-compose.yml config` - OK;
  - `git diff --check` - OK.
- Release:
  - создан tag/release `v2.0.6`;
  - GitHub Actions `Build Windows Release` run `26903757412` завершился success;
  - smoke-test `TakSklad.exe --smoke-import` прошёл в GitHub Actions;
  - официальный `TakSklad.exe` SHA256: `0ec39f25faa5c5e66b92963be859e4505c02292eb4a54f489382077de6788cf0`;
  - официальный `TakSklad-windows-x64.zip` SHA256: `cb4783d0300e4008b90fe24d09e319a91ac00bfc7ae6d9bade5bb52d6a7d8c3d`;
  - `version.json` обновлён на `2.0.6`, rollout остаётся `mandatory=false`;
  - готовый складской архив: `outputs/windows_ready/TakSklad-2.0.6-win-ready.zip`;
  - ready archive SHA256: `1b40793e4936b9aca0c0bea59d78b89ee20b136fee81695481f58aee29479a24`;
  - ready archive содержит рядом с `TakSklad.exe`: `.env.taksklad-vds-2.0.generated.json`, `TakSklad_data.json`, `credentials.json`, `version.json`;
  - `./.venv/bin/python tools/release_preflight.py --verify-downloads` скачал оба assets и подтвердил SHA; единственный fail до commit был ожидаемый `version.json has local git diff`.

### Google archive mirror batch fix

- Причина: после релиза `2.0.6` scan-события уже схлопывались в batch, но обычный перенос завершённых заказов в Google `Архив` всё ещё шёл по одному заказу. Каждый заказ заново читал листы `data` и `Архив`, из-за чего зеркало быстро упиралось в Google read quota `429`.
- Backend:
  - добавлен batch-перенос нескольких обычных `google_sheets_archive_export` событий за один проход;
  - для batch-архива Google `data` и `Архив` читаются один раз, строки в архив пишутся одним `batch_update`, строки из `data` удаляются снизу вверх;
  - если архивное событие повторное и строки уже находятся в `Архиве`, событие закрывается как `skipped`, а не остаётся в `failed`;
  - если завершённого заказа уже нет в Google `data` и нет в `Архиве`, строка архива восстанавливается из VDS заказа/позиции/КИЗов;
  - если scan-событие по уже завершённой позиции не находит строку в активном `data`, оно закрывается как `skipped`, потому что финальное состояние пишет архивный экспорт;
  - старые зависшие `processing` события старше 10 минут автоматически возвращаются в `pending` и повторно обрабатываются;
  - специальные действия `archive_no_kiz`, `cancel`, `return` оставлены поштучными, чтобы не менять порядок редких административных операций;
  - scan batch и rate-limit поведение сохранены: при `429` событие возвращается в `pending`, worker ставит паузу до следующего цикла.
- Проверено:
  - `./.venv/bin/python -m py_compile backend/app/google_sheets_exporter.py backend/app/google_sheets_pending.py` - OK;
  - `./.venv/bin/python -m unittest tests.test_backend_google_sheets_exporter tests.test_backend_google_sheets_pending` - 19 tests OK;
  - `./.venv/bin/python -m unittest discover -s tests` - 319 tests OK.

### Live Google/VDS cleanup and delivery-date parser fix

- Причина: свежий Excel `Шаблон_отправки_заказов_на_склад_04_06_2026.xlsx` имел фактическую `ДАТА ДОСТАВКИ = 05.06.2026`, но importer взял `04.06.2026` из имени файла, потому что колонка даты была в верхней строке над основной шапкой.
- Live cleanup:
  - перед изменениями создан VDS backup `/opt/taksklad/backups/postgres/taksklad-postgres-20260604T054930Z.sql.gz`;
  - локально сохранён backup изменённых Google `data` строк: `outputs/diagnostics/2026-06-04-live/google_data_rows_before_0406_fix.json`;
  - локально сохранён backup удалённых лишних архивных строк: `outputs/diagnostics/2026-06-04-live-after-fix/google_archive_rows_before_extra_delete_0406_terminal.json`;
  - в VDS дата активных заказов `Перечисление` из файла `Шаблон_отправки_заказов_на_склад_04_06_2026.xlsx` исправлена с `2026-06-04` на `2026-06-05`, затронуто `16` заказов;
  - в Google `data` дата `40` строк перечисления исправлена на `05.06.2026`;
  - из Google `data` удалены `87` активных терминальных дублей за `04.06.2026`, которые уже были покрыты `Архивом`;
  - из Google `Архив` удалены `5` лишних терминальных дублей за `04.06.2026`.
- Итоговая сверка:
  - VDS: `04.06.2026 Терминал completed` - `114` позиций, `331` блок, `331` отсканирован;
  - Google `Архив`: `04.06.2026 Терминал Выполнено` - `114` строк, `331` блок, `331` КИЗ;
  - VDS: `05.06.2026 Перечисление active` - `16` заказов, `40` позиций, `97` блоков;
  - Google `data`: `05.06.2026 Перечисление Не выполнено` - `40` строк, `97` блоков;
  - pending Google queue после проверки: `0`.
- Код:
  - backend importer теперь ищет `Дата доставки` / `Дата отгрузки` / `Дата поставки` в строках над основной шапкой;
  - desktop importer получил такую же защиту;
  - Telegram import meta теперь показывает реальную единую дату строк, если она взята из Excel, а не дату из имени файла.
- VDS:
  - на сервер синхронизирован `backend/app/excel_importer.py`;
  - пересобраны и перезапущены `backend-api` и `telegram-worker`;
  - `https://api.taksklad.uz/health` - OK;
  - серверный parser smoke подтвердил: файл с именем `04_06` и верхней `ДАТА ДОСТАВКИ=2026-06-05` импортируется как `05.06.2026`.
- Проверено:
  - `./.venv/bin/python -m unittest tests.test_backend_telegram_import tests.test_excel_normalizer` - 38 tests OK;
  - `./.venv/bin/python -m unittest discover -s tests` - 322 tests OK.

### macOS ready build 2.0.6

- Причина: нужна Mac-сборка, которая запускается в один клик и работает с теми же JSON/VDS настройками, что складской ПК.
- Исправлено:
  - для frozen macOS `.app` рабочая папка теперь определяется как папка рядом с `TakSklad.app`, а не `TakSklad.app/Contents/MacOS`;
  - прямой запуск `.app` больше не пишет `docs/TakSklad.log` внутрь bundle и не ломает подпись;
  - `START_TAKSKLAD.command` оставлен как запасной one-click запуск;
  - Mac bundle пересобран через `./.venv/bin/python -m PyInstaller --clean --noconfirm TakSklad.spec`, чтобы не повторить ошибку `_struct`.
- Готовый артефакт:
  - папка: `outputs/mac_ready/TakSklad-2.0.6-mac-ready`;
  - архив: `outputs/mac_ready/TakSklad-2.0.6-mac-ready.zip`;
  - комплект содержит `TakSklad.app`, `.env.taksklad-vds-2.0.generated.json`, `credentials.json`, `TakSklad_data.json`, `version.json`, command-файлы.
- Проверено:
  - `START_TAKSKLAD.command --smoke-import` - OK;
  - `START_BACKEND.command --smoke-import` - OK;
  - `START_LOCAL.command --smoke-import` - OK;
  - `TakSklad.app/Contents/MacOS/TakSklad --smoke-import` - OK;
  - короткий GUI-launch через `START_TAKSKLAD.command` - OK;
  - короткий прямой GUI-launch бинарника `.app` - OK;
  - `codesign --verify --deep --strict` после запусков - OK.

### SkladBot sync acceleration and completed-order backfill

- Причина: склад может завершить заказ раньше, чем worker успел подтянуть номер WH-R из SkladBot. Закрытие без WH-R оставлено разрешенным, потому что это рабочая логика склада, но номер нужен позже для возвратов и сверок.
- Backend:
  - `SKLADBOT_DETAIL_LIMIT` увеличен с `3` до `10`, чтобы за один проход проверять больше свежих заявок SkladBot без резкого роста нагрузки;
  - advisory lock SkladBot worker переведен на `pg_try_advisory_xact_lock`, чтобы lock не зависал после commit/session reuse;
  - worker теперь проверяет не только активные, но и свежие завершенные заказы без полного комплекта `skladbot_request_number` + `skladbot_request_id`;
  - окно догонки завершенных заказов на VDS задано через `SKLADBOT_COMPLETED_BACKFILL_DAYS=2`;
  - после нахождения WH-R для свежего завершенного заказа worker ставит событие `google_sheets_skladbot_export` с `include_archive=true`, чтобы обновить Google `Архив`;
  - SkladBot metadata export больше не блокирует массовое закрытие заказов без КИЗов в кабинете.
- VDS:
  - перед деплоем создан backup `/opt/taksklad/backups/postgres/taksklad-postgres-20260604T081842Z.sql.gz`;
  - обновлены и перезапущены `backend-api`, `skladbot-worker`, `google-sheets-sync-worker`;
  - настройки VDS: `SKLADBOT_DETAIL_LIMIT=10`, `SKLADBOT_COMPLETED_BACKFILL_DAYS=2`, `SKLADBOT_SYNC_INTERVAL_SECONDS=60`;
  - проверено, что SkladBot worker работает без зависшего lock и без 429/API errors.
- Live verification:
  - по импорту `2e7702bf-eb5a-4b65-a28e-d0c4c93cb6f2` все `16/16` заказов получили WH-R и SkladBot ID в VDS;
  - Google queue по `google_sheets_skladbot_export` завершена, failed events нет;
  - последние SkladBot export события обновили Google `Архив`, а не только активный `data`.
- Проверено:
  - `./.venv/bin/python -m unittest tests.test_backend_skladbot_worker tests.test_backend_google_sheets_pending tests.test_backend_api_persistence tests.test_vds_acceptance_scripts` - 108 tests OK;
  - `./.venv/bin/python -m unittest discover -s tests` - 329 tests OK;
  - `https://api.taksklad.uz/health` - OK.

### SkladBot request auto-create dry-run

- Причина: нужно убрать ручной этап создания заявок SkladBot после Telegram Excel import, но первый этап должен быть безопасным и без реального `POST /v1/requests`.
- Backend:
  - добавлен сервис `skladbot_request_dry_run`, который после импорта строит preview будущей заявки SkladBot по каждому заказу;
  - одна заявка = один заказ TakSklad, товары внутри заявки собираются по всем позициям заказа, даже если текущий импорт добавил только часть строк;
  - payload использует `customer_id=6211`, `request_type_id=3389`, поля `address`, `comment`, `company_name`, `unloading_date`;
  - SKU mapping: Red `2189390`, Brown `2189391`, Gold SSL `2189394`;
  - неизвестный SKU не ломает импорт, а получает статус `blocked`;
  - заказ с уже заполненным `skladbot_request_number` или `skladbot_request_id` получает статус `already_linked`;
  - результат хранится в `pending_events` с `event_type=skladbot_request_dry_run`, `would_post=false`, и пишется в `audit_log`;
  - dry-run работает best-effort: если preview упал, основной импорт остается успешным, Google-очередь сохраняется, а ошибка пишется в import `raw_payload` и `audit_log`;
  - повторный запуск для того же `import_id` не плодит дубли, пересборка доступна отдельным API;
  - режим контролируется `SKLADBOT_CREATE_REQUESTS_MODE=dry_run|enabled|disabled`, по умолчанию `dry_run`;
  - `enabled` на этом этапе сохраняется как `configured_mode`, но фактический режим остается `dry_run`.
- API:
  - `GET /api/v1/admin/skladbot/dry-runs?import_id=...`;
  - `POST /api/v1/admin/skladbot/dry-runs/{id}/rebuild`.
- Web:
  - добавлена вкладка `SkladBot dry-run`;
  - показываются импорт, клиент, дата, тип оплаты, адрес, товары, блоки, статус, причина блокировки и JSON preview;
  - в истории импортов добавлена короткая сводка dry-run.
  - загрузка dry-run отделена от основной таблицы, поэтому сбой dry-run API не блокирует вход и рабочую таблицу.
- Важно:
  - реальное создание заявок SkladBot не включено;
  - на этом этапе SkladBot API не получает POST-запросы от TakSklad.
- Проверено:
  - `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest tests.test_backend_skladbot_request_dry_run` - 10 tests OK;
  - `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest tests.test_backend_api_persistence` - 50 tests OK;
  - `npm run build` в `frontend` - OK;
  - `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest discover -s tests` - 339 tests OK.

### Release 2.0.7 SkladBot dry-run rollout

- Причина: dry-run автосоздания заявок SkladBot проверен на VDS и должен войти в единый релизный контур backend/web/desktop.
- Dry-run проверен на реальной VDS базе без реального SkladBot POST:
  - последний импорт `710fb0c0-7008-4e73-8a8a-10d502d7df2e`: `22` заказа, `22 already_linked`, `51` товарная строка распознана mapping;
  - импорт `9de07944-00d6-4b3f-818e-e76ebb3cebb8`: `33` заказа, `5 ready`, `28 already_linked`, `0 blocked`, `5` payload готовы к preview;
  - API `GET /api/v1/admin/skladbot/dry-runs?import_id=...` вернул `200`, `33` строки, `5` payload.
- Релизные изменения:
  - `APP_VERSION` поднята до `2.0.7`;
  - release preflight, Windows test archive helper и VDS acceptance status переведены на `2.0.7`;
  - `version.json` подготовлен под `v2.0.7`, реальные SHA будут обновлены после GitHub Actions сборки артефактов.
- Важно:
  - боевой `POST /v1/requests` в SkladBot не включён;
  - production режим остается `SKLADBOT_CREATE_REQUESTS_MODE=dry_run`;
  - включение `enabled` будет отдельным этапом после ручного сравнения preview с заявкой менеджера.
