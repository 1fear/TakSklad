# Журнал Работ По Проекту

Документ фиксирует ход работ: что сделано, что не сделано, какие ошибки найдены, какие решения приняты и что требует проверки. Новые записи добавляются сверху.

## 2026-05-30

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
- Локальный Docker/Postgres smoke:
  - `GET /api/v1/orders/active` - `200`;
  - раннее `POST /api/v1/orders/{id}/complete` - `409`;
  - первый `POST /api/v1/scans` - `201`;
  - повторный дубль того же КИЗ - `409`;
  - второй `POST /api/v1/scans` - `201`;
  - закрытие заказа после всех сканов - `200`;
  - активный список после закрытия - `[]`.
- Тестовый Docker-стек остановлен через `docker compose down -v`.

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
