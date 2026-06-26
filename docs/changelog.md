# Журнал изменений

Здесь фиксируются все правки в коде TakSklad: что менялось, в каком файле, зачем, и какие тесты это покрывают. Записи идут от новых к старым.

## 2026-06-26

### Hotfix 2.0.24: forced update для Green/Brown коробов

**Файлы:** `src/taksklad/config.py`, `backend/app/settings.py`, `tests/test_scan_quantities.py`, `tests/test_backend_api_persistence.py`, `tools/release_preflight.py`, `tools/build_windows_test_archive.ps1`, `deploy/vds/acceptance_status.sh`.

**Что стало:**

- Версия desktop/backend поднята до `2.0.24`, чтобы рабочие ПК на `2.0.22` получили новый обязательный update.
- Release/preflight/VDS guards переключены с forced `2.0.23` на forced `2.0.24`.
- Добавлены regression tests на реальные сегодняшние коробочные КИЗы:
  - Green OP `010400639610445821...`, 2 короба по 50 блоков;
  - Brown SSL `010400639605407421...`, 2 короба по 50 блоков.
- Public `version.json` переведен на forced `2.0.24` с `block_workflow=true`.
- GitHub Release `v2.0.24`: `TakSklad.exe` SHA `7fa3b0b9c9526a3833e55b6d41a916edc433d0ecb775407713fad3ebfdd61973`, ZIP SHA `c0446e6293f477975347b1ac8fc426e9d41a6f5fc33420688fd6be87c2b6d94b`.

**Причина:**

- Mapping `0104006396104458 -> green:op` уже был в коде `2.0.23`, но публичный `version.json` был paused на `1.1.7`; из-за этого рабочий ПК остался на `2.0.22` и продолжил показывать `КИЗ распознан как: не распознан`.

## 2026-06-25

### Тип оплаты в истории клиента

**Файлы:** `backend/app/client_points_service.py`, `backend/app/schemas.py`, `frontend/src/api.ts`, `frontend/src/App.tsx`, `frontend/src/styles.css`, `tests/test_backend_api_persistence.py`, `docs/changelog.md`.

**Что стало:**

- В раскрытой истории заказов клиента показывается `Тип оплаты` по каждой дате отгрузки.
- Если за одну дату у клиента несколько типов оплаты, backend возвращает уникальные типы через запятую.

**Проверки:**

- `python3 -m py_compile backend/app/client_points_service.py backend/app/schemas.py backend/app/main.py` - OK.
- `cd frontend && npm run build` - OK.
- `.venv/bin/python -m unittest tests.test_backend_api_persistence.BackendApiPersistenceTests.test_admin_client_point_order_summary_groups_dates_and_products` - OK.

### Web/admin больше не режет рабочие цифры лимитами

**Файлы:** `backend/app/admin_service.py`, `backend/app/client_points_service.py`, `backend/app/event_queue_service.py`, `backend/app/incidents_service.py`, `backend/app/main.py`, `frontend/src/api.ts`, `frontend/src/App.tsx`, `tests/test_backend_api_persistence.py`, `docs/changelog.md`.

**Что стало:**

- Web-панель больше не подставляет скрытые лимиты `5000`, `1000`, `200` и `100` для основной таблицы, клиентских точек, инцидентов и очереди.
- Backend admin endpoints без `limit` возвращают полный набор строк; явный `limit` оставлен только как диагностический параметр.
- Счетчики `Сохранено`, `Показано`, инциденты и события очереди считаются по всем загруженным данным, а не по первой пачке.

**Проверки:**

- `python3 -m py_compile backend/app/admin_service.py backend/app/client_points_service.py backend/app/event_queue_service.py backend/app/incidents_service.py backend/app/main.py` - OK.
- `cd frontend && npm run build` - OK.
- `.venv/bin/python -m unittest tests.test_backend_api_persistence.BackendApiPersistenceTests.test_admin_table_totals_are_not_limited_by_row_limit tests.test_backend_api_persistence.BackendApiPersistenceTests.test_admin_table_supports_offset_pagination_metadata tests.test_backend_api_persistence.BackendApiPersistenceTests.test_admin_client_points_lists_order_points_and_updates_timeslot tests.test_backend_api_persistence.BackendApiPersistenceTests.test_admin_client_points_default_response_is_not_capped tests.test_backend_api_persistence.BackendApiPersistenceTests.test_admin_events_exposes_queue_diagnostics tests.test_backend_api_persistence.BackendApiPersistenceTests.test_admin_events_default_response_is_not_capped tests.test_backend_api_persistence.BackendApiPersistenceTests.test_admin_incidents_link_filter_redact_and_change_status_with_audit` - 7 tests OK.

### Telegram Excel import не подхватывает старый файл

**Файлы:** `backend/app/telegram_worker.py`, `backend/app/imports_service.py`, `tests/test_backend_telegram_import.py`, `tests/test_backend_api_persistence.py`, `docs/implementation-log.md`, `docs/changelog.md`.

**Что стало:**

- При отправке нового Excel в Telegram старые ожидания даты в этом же чате закрываются как `cancelled`, чтобы дата не ушла в старый файл.
- Если в чате есть несколько ожидающих Excel, дата применяется к последнему, а не к старейшему событию.
- Backend import теперь отсекает дубли внутри одного payload по `ID импорта` или `item_key` до создания `OrderItem` и до очереди Google Sheets export.
- Backfill уже существующих backend-строк в Google Sheets сохранен.

**Проверки:**

- `.venv/bin/python -m unittest tests.test_backend_telegram_import` - 65 tests OK.
- `.venv/bin/python -m unittest tests.test_backend_api_persistence` - 97 tests OK.
- `.venv/bin/python -m py_compile backend/app/telegram_worker.py backend/app/imports_service.py tests/test_backend_telegram_import.py tests/test_backend_api_persistence.py` - OK.

## 2026-06-24

### Emergency pause forced desktop auto-update

**Файлы:** `version.json`, `src/taksklad/app_updates.py`, `tools/release_preflight.py`, `deploy/vds/acceptance_status.sh`, `tools/build_windows_test_archive.ps1`, `tests/test_app_updates.py`, `tests/test_update_service.py`, `tests/test_release_preflight.py`, `tests/test_vds_acceptance_scripts.py`, `tests/test_windows_test_build_helper.py`.

**Что стало:**

- Public update manifest переведен в paused rollout: `latest_version=1.1.7`, `min_supported_version=1.1.7`, `mandatory=false`, download URL/SHA пустые.
- Старые рабочие ПК больше не должны попадать в hard-lock из-за `min_supported_version=2.0.23`.
- В desktop updater `mandatory=true` больше не блокирует склад сам по себе. Блокировка workflow включается только отдельным явным флагом manifest `block_workflow=true`.
- Release/preflight/VDS guards теперь принимают два явных состояния: paused `1.1.7` или forced `2.0.23`.

**Проверки:**

- `.venv/bin/python -m unittest tests.test_app_updates` - 13 tests OK.
- `PYTHONPATH=src:. .venv/bin/python -m unittest tests.test_update_service tests.test_release_preflight tests.test_vds_acceptance_scripts tests.test_windows_test_build_helper` - 25 tests OK.
- `PYTHONPATH=src:. .venv/bin/python -m compileall -q src/taksklad/app_updates.py src/taksklad/update_service.py tools/release_preflight.py` - OK.

### Hotfix 2.0.23: Green короб и retry автообновления

**Файлы:** `src/taksklad/scan_quantities.py`, `backend/app/scan_quantities.py`, `src/taksklad/app_updates.py`, `src/taksklad/update_service.py`, `tests/test_scan_quantities.py`, `tests/test_backend_api_persistence.py`, `tests/test_app_updates.py`, `tests/test_update_service.py`.

**Что стало:**

- Добавлен новый коробочный GTIN Green OP `0104006396104458`; такие короба распознаются как `green:op` и дают `+50` блоков.
- Старый Green GTIN `0104006396104448` сохранен для обратной совместимости.
- После уже принятой попытки обязательного автообновления перезапуск старого exe больше не упирается в часовой cooldown-блок, а может сразу предложить повторить установку.
- Если версия приложения уже равна версии манифеста, package-only переход на onedir больше не блокирует сканирование как mandatory update.

**Проверки:**

- `.venv/bin/python -m unittest tests.test_app_updates tests.test_update_service` - 14 tests OK.
- `.venv/bin/python -m unittest tests.test_scan_quantities tests.test_backend_api_persistence.BackendApiPersistenceTests.test_scan_create_accepts_live_green_aggregate_box_gtin tests.test_backend_api_persistence.BackendApiPersistenceTests.test_scan_create_accepts_aggregate_box_when_next_ai_is_not_serial tests.test_backend_api_persistence.BackendApiPersistenceTests.test_scan_create_rejects_aggregate_box_for_wrong_product` - 12 tests OK.
- `.venv/bin/python -m unittest tests.test_release_preflight tests.test_windows_test_build_helper tests.test_vds_acceptance_scripts` - 19 tests OK.

**Релиз/deploy:**

- GitHub release `v2.0.23` опубликован, Windows workflow `28090484689` завершился успешно.
- Public `version.json` переведен на forced `2.0.23`, package type `onefile_exe`; SHA `TakSklad.exe` = `72740494cf7342624e98a1cb4d19130882cd346fe9b363840db11f84f3b6e7d7`, SHA ZIP = `e2ab0dc3ad46ab203161210389508543451cb3f42cf9d3b658af3373df7e998a`.
- VDS targeted deploy: пересобран только `backend-api`; restore point `/opt/taksklad/restore_points/pre-2023-green-box-updater-20260624T100252Z`, Postgres backup `/opt/taksklad/backups/postgres/taksklad-postgres-20260624T100252Z.sql.gz`.
- `https://api.taksklad.uz/health` вернул `version=2.0.23`; backend container распознал live Green-код как `green:op`, `block_quantity=50`.
- `./deploy/vds/acceptance_status.sh` остался `failed` только из-за старого `/ready=degraded` по `telegram_excel_import` и незакрытых ручных GO/NO-GO чекбоксов; version_json, Google/backend sync и SkladBot coverage OK.

### Короба новых Chapman SKU распознаются по GTIN короба

**Файлы:** `src/taksklad/scan_quantities.py`, `backend/app/scan_quantities.py`, `tests/test_scan_quantities.py`, `tests/test_backend_api_persistence.py`, `docs/implementation-log.md`, `docs/changelog.md`.

**Что стало:**

- Агрегационный короб распознается по `01 + GTIN короба`, без требования, чтобы сразу после GTIN шел AI `21`.
- Короба Brown OP, RED OP, Gold SSL, Brown SSL, RED SSL и Green OP продолжают считаться как `+50` блоков.
- Desktop и backend держат одинаковый mapping коробных префиксов.
- Wrong-SKU и проверка остатка позиции сохранены.

**Проверки:**

- `.venv/bin/python -m unittest tests.test_scan_quantities` - 9 tests OK.
- `.venv/bin/python -m unittest tests.test_backend_api_persistence` - 94 tests OK.
- `.venv/bin/python -m compileall -q src/taksklad backend/app tests/test_scan_quantities.py tests/test_backend_api_persistence.py` - OK.

## 2026-06-23

### Отгрузка в браке в ежедневном SkladBot уведомлении

**Файлы:** `backend/app/skladbot_daily_report.py`, `tests/test_skladbot_daily_report.py`, `docs/report-source-rules.md`, `docs/implementation-log.md`, `docs/changelog.md`.

**Что стало:**

- Тип заявки `Отгрузка в браке` больше не суммируется в обычную строку `Отгрузка`.
- Telegram daily message получил отдельную строку `Отгрузка в браке: N заявок, M блоков`, чтобы брак был виден без открытия XLSX.
- XLSX-лист `Сводка` получил отдельную строку `Отгрузка в браке`; в движении остатков она остается отрицательным расходом.

**Проверки:**

- `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:. .venv/bin/python -m unittest tests.test_skladbot_daily_report` - 20 tests OK.
- `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:. .venv/bin/python -m unittest tests.test_skladbot_daily_report tests.test_backend_telegram_import` - 84 tests OK.
- `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:. .venv/bin/python -m compileall -q backend/app/skladbot_daily_report.py tests/test_skladbot_daily_report.py` - OK.
- `git diff --check -- backend/app/skladbot_daily_report.py tests/test_skladbot_daily_report.py docs/report-source-rules.md docs/changelog.md docs/implementation-log.md` - OK.
- VDS restore point: `/opt/taksklad/restore_points/pre-defect-shipment-daily-20260623T174716Z`.
- VDS Postgres backup: `/opt/taksklad/backups/postgres/taksklad-postgres-20260623T174716Z.sql.gz`.
- VDS deploy: синхронизирован только `backend/app/skladbot_daily_report.py`; пересобран `telegram-worker`, compose также пересоздал `backend-api` как зависимость.
- VDS `telegram-worker` compileall по `app/skladbot_daily_report.py` и `app/telegram_worker.py` - OK.
- Production smoke внутри `telegram-worker`: `Отгрузка: 1 заявок, 10 блоков` и `Отгрузка в браке: 1 заявок, 3 блоков` выводятся отдельными строками.
- `https://api.taksklad.uz/health` - OK, `version=2.0.21`.
- `https://api.taksklad.uz/ready` - migrations/database OK, общий статус `degraded` из-за старой failed `telegram_excel_import`, не из-за daily report.
- VDS `./deploy/vds/acceptance_status.sh` - технические проверки marker/google/skladbot/menu OK, общий `status=failed` из-за того же `ready=degraded` и незакрытого GO/NO-GO чеклиста.
- Daily report anti-duplicate check: `pending_events` для `skladbot_daily_report:2026-06-23:*` остался `completed=1`, повторная отправка не появилась.
- Свежие логи `telegram-worker` и `backend-api` после деплоя - без `ERROR/Traceback/Exception/CRITICAL/failed` и без повторной отправки `SkladBot отчет`.

### WEB/LOG client identity for logistics points

**Файлы:** `backend/app/client_points_service.py`, `frontend/src/App.tsx`, `frontend/src/styles.css`, `tests/test_backend_api_persistence.py`, `backend/README.md`, `docs/taksklad-system-stack-overview.md`, `docs/implementation-log.md`, `docs/changelog.md`.

**Что стало:**

- Точка логистики теперь фиксируется по `client_name`: название точки, юрлицо и клиент считаются одним полем.
- Адрес, координаты и ТП являются обновляемыми деталями точки и подтягиваются из новых импортов.
- Сохраненный таймслот не теряется при смене адреса у того же клиента.
- Web UI разделил поиск и создание: поиск фильтрует текущих клиентов, а создание открывается отдельной кнопкой `Создать точку`.

**Проверки:**

- `.venv/bin/python -m unittest tests.test_backend_api_persistence.BackendApiPersistenceTests.test_admin_client_points_lists_order_points_and_updates_timeslot tests.test_backend_api_persistence.BackendApiPersistenceTests.test_admin_client_points_use_client_identity_when_address_changes tests.test_backend_api_persistence.BackendApiPersistenceTests.test_import_updates_client_point_address_and_keeps_timeslot_by_client tests.test_backend_api_persistence.BackendApiPersistenceTests.test_logistics_report_uses_saved_client_point_timeslot tests.test_backend_api_persistence.BackendApiPersistenceTests.test_readiness_accepts_user_password_hash_schema_head_revision tests.test_backend_skeleton.BackendSkeletonTests.test_initial_schema_contains_mvp_tables_and_constraints tests.test_backend_skeleton.BackendSkeletonTests.test_sql_bootstrap_and_alembic_migrations_keep_forward_only_contract` - 7 tests OK.
- `npm --prefix frontend run build` - OK.
- Production deploy на `taksklad.uz` выполнен targeted bundle из VDS remote-base: изменены только `backend/app/client_points_service.py`, `frontend/src/App.tsx`, `frontend/src/styles.css`.
- VDS restore point: `/opt/taksklad/restore_points/pre-client-identity-timeslots-20260623T102546Z`.
- VDS Postgres backup: `/opt/taksklad/backups/postgres/taksklad-postgres-20260623T102544Z.sql.gz`.
- VDS build: `backend-api` + `frontend` rebuilt; миграций нет, Alembic current остается `20260623_0004`.
- Live checks: `https://api.taksklad.uz/health` - OK; `https://api.taksklad.uz/ready` - migrations OK, overall degraded из-за старой очереди `telegram_excel_import`; `https://taksklad.uz/` отдает frontend asset `index-U-lmCpOW.js` / `index-afgwTo0A.css`.
- Live rollback smoke: тестовый клиент получил один `client_points`, адрес обновился на новый, таймслот `08:31-09:32` попал в логистический XLSX; после rollback `persisted_points=0`, `persisted_orders=0`.

### WEB/RBAC logistics slots limited user

**Файлы:** `backend/app/web_auth.py`, `backend/app/main.py`, `backend/app/models.py`, `backend/app/schemas.py`, `backend/app/health_service.py`, `backend/migrations/versions/20260623_0004_user_password_hash.py`, `backend/sql/001_initial_schema.sql`, `frontend/nginx.conf.template`, `frontend/src/api.ts`, `frontend/src/App.tsx`, `tests/test_backend_api_persistence.py`, `tests/test_backend_skeleton.py`, `tests/test_vds_acceptance_scripts.py`, `backend/README.md`, `docs/implementation-log.md`, `docs/changelog.md`.

**Что стало:**

- Web-auth умеет проверять DB-backed пользователей из `users` с `password_hash`, не ломая старый env-admin login.
- Session cookie теперь несет `role` и `permissions`; роль `logistics_slots` получает read-only доступ к web UI и право `client_points:write`.
- Все state-changing API для заказов, импортов, сканов, возвратов, очереди, SkladBot dry-run, sync и reconciliation закрыты `admin:write`.
- `POST /api/v1/admin/client-points/timeslot` оставлен доступным для `logistics_slots`.
- Frontend proxy `/api/` больше не подставляет внутренний Bearer service token после `auth_request`; backend видит реальную web-session роль.
- В web `Клиенты` добавлен безопасный сброс кастомного таймслота к `10:00-18:00`.
- Alembic head обновлен до `20260623_0004`.

**Проверки:**

- `.venv/bin/python -m unittest tests.test_backend_api_persistence` - 91 tests OK.
- `.venv/bin/python -m unittest tests.test_vds_acceptance_scripts.VdsAcceptanceScriptsTests.test_frontend_uses_same_origin_api_proxy_contract` - OK.
- `npm --prefix frontend run build` - OK.
- Production deploy на `taksklad.uz` выполнен targeted bundle из VDS remote-base, без локальных незадеплоенных фич.
- VDS restore point: `/opt/taksklad/restore_points/pre-web-rbac-logistics-user-20260623T092150Z`.
- VDS Postgres backup: `/opt/taksklad/backups/postgres/taksklad-postgres-20260623T092149Z.sql.gz`.
- VDS build/migration: `backend-api` + `frontend` rebuilt, Alembic `20260623_0004` applied.
- Live checks: `https://api.taksklad.uz/health` - OK; `https://api.taksklad.uz/ready` - migrations OK, overall degraded из-за старой очереди `telegram_excel_import`.
- Live RBAC smoke через `https://taksklad.uz/api`: новый пользователь `998933456753` получил `role=logistics_slots`, `permissions=["client_points:write"]`; `GET /api/v1/admin/table` - 200; admin-write endpoints - 403; `POST /api/v1/admin/client-points/timeslot` проходит auth и на пустом payload возвращает validation `422`.

### WEB/LOG client points and logistics time slots

**Файлы:** `backend/app/models.py`, `backend/app/client_points_service.py`, `backend/app/imports_service.py`, `backend/app/logistics_service.py`, `backend/app/main.py`, `backend/app/schemas.py`, `backend/app/health_service.py`, `backend/migrations/versions/20260623_0003_client_points.py`, `backend/sql/001_initial_schema.sql`, `frontend/src/api.ts`, `frontend/src/App.tsx`, `frontend/src/styles.css`, `tests/test_backend_api_persistence.py`, `tests/test_backend_skeleton.py`, `backend/README.md`, `docs/report-source-rules.md`, `docs/taksklad-system-stack-overview.md`, `docs/implementation-log.md`, `docs/changelog.md`.

**Что стало:**

- Добавлен справочник `client_points` для юрлица/точки/адреса, координат, торгового представителя и окна доставки.
- Web-панель получила вкладку `Клиенты` с ручным добавлением точки, поиском по юрлицам, фильтром уникальных таймслотов и inline-редактированием `Доставка С/ПО`.
- Импорт сохраняет новые точки с дефолтом `10:00-18:00`; старые точки из заказов видны в web как derived-записи и сохраняются при первой правке.
- Логистический XLSX подставляет сохраненный таймслот по `client_name + address`, а для неизвестных точек оставляет прежний fallback `10:00-18:00`.
- Alembic head обновлен до `20260623_0003`; отсутствие новой миграции должно быть видно через `/ready`.

**Проверки:**

- `.venv/bin/python -m py_compile backend/app/client_points_service.py backend/app/models.py backend/app/schemas.py backend/app/main.py backend/app/imports_service.py backend/app/logistics_service.py backend/app/health_service.py tests/test_backend_api_persistence.py tests/test_backend_skeleton.py` - OK.
- `.venv/bin/python -m unittest tests.test_backend_api_persistence tests.test_backend_skeleton` - 99 tests OK.
- `npm run build` в `frontend/` - OK.
- `git diff --check` - OK.
- Production deploy на `taksklad.uz` выполнен targeted bundle без соседних локальных изменений; VDS restore point: `/opt/taksklad/restore_points/pre-client-points-timeslots-20260623T082754Z`.
- VDS Postgres backup: `/opt/taksklad/backups/postgres/taksklad-postgres-20260623T082754Z.sql.gz`.
- VDS build/migration: `backend-api` + `frontend` rebuilt, Alembic `20260623_0003` applied.
- Live checks: `https://api.taksklad.uz/health` - OK; `https://api.taksklad.uz/ready` - migrations OK, overall degraded из-за старой очереди `telegram_excel_import`; `https://taksklad.uz/` отдает новый asset.
- Live API smoke: `GET /api/v1/admin/client-points?limit=3` через service token внутри `backend-api` - 200, ответ содержит поля `delivery_from/delivery_to`.
- Live logistics smoke: в rollback-транзакции временный слот `08:31-09:32` попал в XLSX `Заявки`, после rollback `client_points` не изменился.

## 2026-06-22

### WEB-03 same-origin API proxy contract

**Файлы:** `tests/test_vds_acceptance_scripts.py`, `docs/taksklad-feature-user-stories.xlsx`, `docs/implementation-log.md`, `docs/changelog.md`.

**Что стало:**

- `GAP-026` переведен в `fixed_retested`: web frontend закреплен за same-origin `/api`, без отдельного браузерного API host.
- Nginx frontend container проксирует `/api/` в `backend-api`, использует `auth_request` и Bearer service token внутри Docker-сети.
- CSP оставляет `connect-src 'self'`, поэтому web UI не зависит от CORS для рабочих admin/report запросов.
- Vite dev proxy остается явным локальным режимом через `VITE_TAKSKLAD_DEV_API_URL`.
- Manual browser smoke для reports/activity diagnostics остается pending отдельно.

**Проверки:**

- `.venv/bin/python -m unittest tests.test_vds_acceptance_scripts.VdsAcceptanceScriptsTests.test_frontend_uses_same_origin_api_proxy_contract` - 1 test OK.

### API-09 configurable SkladBot SKU mapping

**Файлы:** `backend/app/skladbot_request_dry_run.py`, `tests/test_backend_skladbot_request_dry_run.py`, `deploy/vds/docker-compose.yml`, `deploy/vds/.env.example`, `tests/test_vds_acceptance_scripts.py`, `backend/README.md`, `docs/taksklad-system-stack-overview.md`, `docs/taksklad-feature-user-stories.xlsx`, `docs/implementation-log.md`, `docs/changelog.md`.

**Что стало:**

- `GAP-029` переведен в `fixed_retested`: SkladBot SKU mapping больше не является только hardcoded runtime-таблицей.
- Добавлен `SKLADBOT_SKU_MAPPING_JSON`: пустое значение сохраняет текущий Chapman default, JSON override может заменить или добавить SKU key.
- Невалидный JSON или неправильная запись mapping блокирует dry-run заказа и не ставит `skladbot_request_create` event.
- `SKLADBOT_CREATE_REQUESTS_MODE` не менялся: default остается `dry_run`, live POST требует явного `enabled`.
- Live SkladBot API/tokens acceptance остается pending отдельно.

**Проверки:**

- `.venv/bin/python -m unittest tests.test_backend_skladbot_request_dry_run tests.test_backend_skladbot_worker` - 76 tests OK.
- `.venv/bin/python -m unittest tests.test_vds_acceptance_scripts tests.test_backend_skeleton` - 14 tests OK.
- `.venv/bin/python -m py_compile backend/app/skladbot_request_dry_run.py tests/test_backend_skladbot_request_dry_run.py tests/test_vds_acceptance_scripts.py` - OK.

### API-08 Google export retry cooldown

**Файлы:** `backend/app/google_sheets_pending.py`, `tests/test_backend_google_sheets_pending.py`, `docs/taksklad-feature-user-stories.xlsx`, `docs/event-queue-lifecycle.md`, `docs/implementation-log.md`, `docs/changelog.md`.

**Что стало:**

- `GAP-028` переведен в `fixed_retested`: после Google `429` / `quota` pending export больше не повторяется сразу до `payload.next_attempt_at`.
- Старое событие на cooldown не блокирует более новое ready-событие очереди.
- Если ready-событий нет, результат retry показывает `remaining` по pending/deferred exports.
- Live Google credentials/quota acceptance остается pending отдельно.

**Проверки:**

- `.venv/bin/python -m unittest tests.test_backend_google_sheets_pending.GoogleSheetsPendingLockTests.test_future_retry_after_event_does_not_block_newer_ready_event tests.test_backend_google_sheets_pending.GoogleSheetsPendingLockTests.test_rate_limit_keeps_event_pending_and_stops_batch tests.test_backend_google_sheets_pending.GoogleSheetsPendingLockTests.test_postgres_pending_selection_uses_skip_locked_row_lock tests.test_backend_google_sheets_pending.GoogleSheetsPendingLockTests.test_bad_event_does_not_block_newer_valid_event` - 4 tests OK.
- `.venv/bin/python -m py_compile backend/app/google_sheets_pending.py tests/test_backend_google_sheets_pending.py` - OK.

### API-06 import Google export failure isolation

**Файлы:** `backend/app/imports_service.py`, `tests/test_backend_api_persistence.py`, `docs/taksklad-feature-user-stories.xlsx`, `docs/event-queue-lifecycle.md`, `docs/implementation-log.md`, `docs/changelog.md`.

**Что стало:**

- `GAP-023` переведен в `fixed_retested`: сбой постановки Google export после backend import больше не превращает успешный импорт в 500.
- Postgres orders/items остаются созданными, а API возвращает `201` с `google_sheets_status=error` и текстом `google_sheets_error`.
- Для review создаются incident `google_sheets_import_export` и audit `google_sheets_import_export_failed`.
- Обычная постановка Google import records в `pending_events` не изменилась.

**Проверки:**

- `.venv/bin/python -m unittest tests.test_backend_api_persistence.BackendApiPersistenceTests.test_import_reports_google_queue_failure_without_rolling_back_backend_data tests.test_backend_api_persistence.BackendApiPersistenceTests.test_import_keeps_backend_data_when_google_sheets_export_fails tests.test_backend_api_persistence.BackendApiPersistenceTests.test_duplicate_backend_import_still_can_backfill_google_sheets tests.test_backend_api_persistence.BackendApiPersistenceTests.test_import_preview_reports_duplicates_invalid_rows_and_does_not_write` - 4 tests OK.
- `.venv/bin/python -m py_compile backend/app/imports_service.py tests/test_backend_api_persistence.py` - OK.

### API-04 scan Google export local lock coverage

**Файлы:** `tests/test_backend_google_sheets_pending.py`, `docs/taksklad-feature-user-stories.xlsx`, `docs/event-queue-lifecycle.md`, `docs/implementation-log.md`, `docs/changelog.md`.

**Что стало:**

- `GAP-021` переведен в `fixed_retested`: локальный/non-Postgres обработчик Google export больше не считается непроверенным no-op.
- Добавлен контракт, что если process-local lock уже занят, `process_pending_google_sheets_exports()` возвращает `busy`, не читает БД и не меняет события.
- Существующий scan API контракт подтвержден: Google export event ставится в очередь best-effort и не блокирует успешный scan.
- Manual operator/UI/live acceptance для `API-04` остается pending отдельно.

**Проверки:**

- `.venv/bin/python -m unittest tests.test_backend_google_sheets_pending.GoogleSheetsPendingLockTests.test_non_postgres_export_lock_returns_busy_without_processing tests.test_backend_google_sheets_pending.GoogleSheetsPendingLockTests.test_rate_limit_keeps_event_pending_and_stops_batch tests.test_backend_api_persistence.BackendApiPersistenceTests.test_scan_create_queues_google_sheets_export_when_google_is_down tests.test_backend_api_persistence.BackendApiPersistenceTests.test_scan_create_exports_scan_state_to_google_sheets_best_effort` - 4 tests OK.
- `.venv/bin/python -m py_compile backend/app/google_sheets_pending.py tests/test_backend_google_sheets_pending.py tests/test_backend_api_persistence.py` - OK.

### API-01 empty service-token auth guard

**Файлы:** `backend/app/main.py`, `tests/test_backend_api_persistence.py`, `tests/test_backend_cors.py`, `docs/taksklad-feature-user-stories.xlsx`, `backend/README.md`, `docs/taksklad-system-stack-overview.md`, `docs/implementation-log.md`, `docs/changelog.md`.

**Что стало:**

- `GAP-018` переведен в `fixed_retested`: пустой `TAKSKLAD_API_TOKEN` больше не открывает защищенные `/api/v1/*`, если web-auth настроен.
- Bearer token считается валидным только когда service token реально задан и совпадает.
- Web session cookie по-прежнему допускает admin API после успешного login.
- Локальный no-auth режим сохранен только для случая, когда не настроены ни service token, ни web-auth.
- Browser login/session/CORS smoke остается manual pending.

**Проверки:**

- `.venv/bin/python -m unittest tests.test_backend_api_persistence.BackendApiPersistenceTests.test_web_auth_login_sets_cookie_and_check_accepts_session tests.test_backend_api_persistence.BackendApiPersistenceTests.test_web_auth_session_allows_admin_api_without_service_token tests.test_backend_api_persistence.BackendApiPersistenceTests.test_web_auth_configured_without_service_token_still_requires_session tests.test_backend_api_persistence.BackendApiPersistenceTests.test_api_allows_local_no_auth_only_when_no_auth_is_configured tests.test_backend_cors` - 5 tests OK.
- `.venv/bin/python -m py_compile backend/app/main.py tests/test_backend_api_persistence.py tests/test_backend_cors.py` - OK.

### WEB-02 admin event retry and payload redaction

**Файлы:** `backend/app/event_queue_service.py`, `tests/test_backend_api_persistence.py`, `docs/taksklad-feature-user-stories.xlsx`, `docs/event-queue-lifecycle.md`, `docs/implementation-log.md`, `docs/changelog.md`.

**Что стало:**

- `GAP-025` переведен в `fixed_retested`: backend уже отдаёт в web-admin только redacted `raw_payload` и `last_error` для событий очереди.
- Ручной retry события требует причину, пишет audit `pending_event_retry_requested` и не разрешает retry terminal/state events.
- `telegram_excel_import` теперь считается retryable только если в payload сохранен исходный `document.file_id`; без него backend возвращает `409`, не меняя статус события.
- Browser smoke по экрану incidents/events остается manual pending, потому что реальный web UI не запускался в этом цикле.

**Проверки:**

- `.venv/bin/python -m unittest tests.test_backend_api_persistence.BackendApiPersistenceTests.test_admin_event_detail_retry_redacts_payload_and_writes_audit tests.test_backend_api_persistence.BackendApiPersistenceTests.test_admin_event_retry_rejects_telegram_import_when_original_file_is_unavailable` - 2 tests OK.
- `.venv/bin/python -m py_compile backend/app/event_queue_service.py tests/test_backend_api_persistence.py` - OK.

### TS-TG-002 invalid Telegram notification queue handling

**Файлы:** `backend/app/telegram_worker.py`, `tests/test_backend_telegram_import.py`, `docs/taksklad-feature-user-stories.xlsx`, `docs/event-queue-lifecycle.md`, `docs/implementation-log.md`, `docs/changelog.md`.

**Что стало:**

- `GAP-034` переведен в `fixed_retested`: queued `telegram_notification` без текста или без адресата больше не помечается как временный `failed`.
- Такие события переводятся в `blocked`, получают понятный `last_error` и audit `telegram_notification_blocked`.
- Реальные ошибки отправки Telegram остаются `failed` и retryable.
- Live validation allowed/admin chat settings остается manual pending.

**Проверки:**

- `.venv/bin/python -m unittest tests.test_backend_telegram_import.BackendTelegramImportTests.test_telegram_worker_sends_pending_notification_event tests.test_backend_telegram_import.BackendTelegramImportTests.test_telegram_worker_blocks_invalid_notification_events_without_retry tests.test_backend_telegram_import.BackendTelegramImportTests.test_telegram_worker_resets_stale_processing_notification_before_processing tests.test_backend_telegram_import.BackendTelegramImportTests.test_telegram_worker_runs_scheduled_jobs_after_getupdates_conflict` - 4 tests OK.
- `.venv/bin/python -m py_compile backend/app/telegram_worker.py tests/test_backend_telegram_import.py` - OK.

### DESK-14 return totals display for legacy Google rows

**Файлы:** `src/taksklad/app_returns.py`, `tests/test_desktop_ui_contract.py`, `docs/taksklad-feature-user-stories.xlsx`, `docs/taksklad-full-functionality.md`, `docs/implementation-log.md`, `docs/changelog.md`.

**Что стало:**

- Окно `Возвраты` теперь считает блоки и сумму не только по backend-полям `quantity_blocks` / `line_total`, но и по legacy Google-полям `Кол-во блок` / `Сумма`.
- Для backend-заказов поведение не меняется.
- Для старых Google fallback-заказов оператор больше не видит ложные `0 блоков` и `0 сум`, если состав пришел в Google-формате.
- Это только отображение в UI возврата: правила `confirmed_items`, backend source of truth и освобождение КИЗа не менялись.

**Проверки:**

- `.venv/bin/python -m unittest tests.test_desktop_ui_contract.DesktopUiContractTests.test_return_mark_sends_confirmed_items_to_backend tests.test_desktop_ui_contract.DesktopUiContractTests.test_backend_returns_list_reads_backend_without_google_fallback tests.test_desktop_ui_contract.DesktopUiContractTests.test_backend_return_lookup_reads_backend_without_google_fallback tests.test_desktop_ui_contract.DesktopUiContractTests.test_backend_return_rejects_google_order_without_backend_id tests.test_desktop_ui_contract.DesktopUiContractTests.test_backend_return_uses_backend_id_for_google_order tests.test_desktop_ui_contract.DesktopUiContractTests.test_legacy_return_keeps_google_write_fallback_for_google_order tests.test_desktop_ui_contract.DesktopUiContractTests.test_return_confirmed_items_are_built_from_backend_items tests.test_desktop_ui_contract.DesktopUiContractTests.test_return_totals_support_backend_and_google_item_shapes tests.test_backend_api_persistence.BackendApiPersistenceTests.test_return_lookup_and_mark_returned_excludes_order_from_active_list tests.test_backend_api_persistence.BackendApiPersistenceTests.test_return_releases_kiz_for_new_outbound_scan_with_history tests.test_backend_api_persistence.BackendApiPersistenceTests.test_failed_return_does_not_release_kiz_for_new_order tests.test_backend_api_persistence.BackendApiPersistenceTests.test_mark_return_exports_archive_and_returns_to_google_sheets_best_effort tests.test_backend_api_persistence.BackendApiPersistenceTests.test_mark_return_rejects_mismatched_confirmed_items_without_side_effects` - 13 tests OK.
- `.venv/bin/python -m py_compile src/taksklad/app_returns.py tests/test_desktop_ui_contract.py backend/app/orders_service.py tests/test_backend_api_persistence.py` - OK.

### DESK-16 day-end Telegram result clarity

**Файлы:** `src/taksklad/app_day_end.py`, `tests/test_daily_report.py`, `docs/taksklad-feature-user-stories.xlsx`, `docs/taksklad-full-functionality.md`, `docs/user-business-process-guide.md`, `docs/implementation-log.md`, `docs/changelog.md`.

**Что стало:**

- Итог закрытия смены теперь показывает по каждому XLSX-файлу не только `отправлен` / `в очереди Telegram` / `не отправлен`, но и причину для `queued` и `failed`.
- Длинные Telegram-ошибки обрезаются до безопасной длины, чтобы не ломать окно результата.
- Успешная отправка остается короткой: `отправлен`, без лишнего технического текста.
- `GAP-016` оставлен `needs_validation`: реальная Telegram-доставка, права чата, сеть и retry очереди требуют ручной проверки.

**Проверки:**

- `.venv/bin/python -m unittest tests.test_daily_report tests.test_desktop_ui_contract` - 50 tests OK.
- `.venv/bin/python -m py_compile src/taksklad/app_day_end.py src/taksklad/reports.py src/taksklad/telegram_service.py tests/test_daily_report.py` - OK.

### DESK-15 updater launch recovery

**Файлы:** `src/taksklad/app_updates.py`, `tests/test_app_updates.py`, `docs/taksklad-feature-user-stories.xlsx`, `docs/taksklad-full-functionality.md`, `docs/implementation-log.md`, `docs/changelog.md`.

**Что стало:**

- Если временный PowerShell/cmd installer обновления не стартует, приложение больше не падает из Tkinter callback и не закрывается как будто обновление началось успешно.
- Оператор получает critical recovery с причиной, ссылкой на `TakSklad_update.log` и безопасным действием: закрыть старую версию, установить свежий Windows-архив и не сканировать через старый exe.
- При успешном запуске installer поведение прежнее: приложение закрывается, чтобы updater мог заменить файлы.
- `GAP-015` оставлен `needs_validation`: реальный Windows updater/copy flow, ярлык, restart и замена папки `_internal` требуют ручной проверки на Windows.

**Проверки:**

- `.venv/bin/python -m unittest tests.test_app_updates tests.test_update_service tests.test_windows_release_workflow tests.test_startup_check` - 14 tests OK.
- `.venv/bin/python -m py_compile src/taksklad/app_updates.py tests/test_app_updates.py src/taksklad/update_service.py tests/test_update_service.py` - OK.

### DESK-13 desktop Excel backend preview and coordinates

**Файлы:** `backend/app/imports_service.py`, `backend/app/main.py`, `backend/app/schemas.py`, `src/taksklad/app_imports.py`, `src/taksklad/backend_client.py`, `src/taksklad/excel_import.py`, `tests/test_app_imports.py`, `tests/test_backend_api_persistence.py`, `tests/test_backend_bridge.py`, `tests/test_excel_normalizer.py`, `docs/taksklad-feature-user-stories.xlsx`, `docs/taksklad-full-functionality.md`, `docs/user-business-process-guide.md`, `docs/implementation-log.md`, `docs/changelog.md`.

**Что стало:**

- Desktop import теперь явно принимает только `.xlsx` и `.xlsm` перед открытием файла через `openpyxl`.
- Если оператор через `All files` выбрал `.xls` или другой неподдерживаемый файл, preview получает понятную ошибку с разрешенными расширениями.
- Backend получил read-only endpoint `POST /api/v1/imports/preview`, который считает новые позиции, дубли, invalid rows и не создает `ImportJob`, `Order`, `OrderItem` или pending events.
- Desktop backend-mode preview теперь использует этот endpoint, показывает реальные дубли/invalid rows до подтверждения и отправляет на commit только новые строки.
- Desktop parser сохраняет поле `Координаты` в records, поэтому backend/logistics больше не теряют координаты, найденные в Excel.
- Старые docs по пустому адресу приведены к текущему контракту: пустой адрес без координат = `Самовывоз со склада`, координаты сохраняются отдельно.
- `GAP-013` оставлен `needs_validation`: полный Tkinter e2e импорта, реальный backend/Google commit и внешний геокодинг требуют ручной проверки на Windows.

**Проверки:**

- `.venv/bin/python -m unittest tests.test_excel_normalizer tests.test_app_imports tests.test_backend_bridge tests.test_backend_api_persistence.BackendApiPersistenceTests.test_import_preview_reports_duplicates_invalid_rows_and_does_not_write tests.test_backend_api_persistence.BackendApiPersistenceTests.test_import_stores_coordinates_blocks_and_prices tests.test_backend_api_persistence.BackendApiPersistenceTests.test_import_marks_missing_address_as_pickup` - 27 tests OK.
- `.venv/bin/python -m py_compile backend/app/imports_service.py backend/app/main.py backend/app/schemas.py src/taksklad/backend_client.py src/taksklad/app_imports.py src/taksklad/excel_import.py tests/test_app_imports.py tests/test_excel_normalizer.py tests/test_backend_bridge.py tests/test_backend_api_persistence.py` - OK.
- `.venv/bin/python -m unittest tests.test_backend_api_persistence tests.test_excel_normalizer tests.test_app_imports tests.test_backend_bridge tests.test_feature_user_stories_register tests.test_feature_acceptance_status` - 122 tests OK.
- `.venv/bin/python tools/feature_acceptance_status.py` - `status=ok`, `manual_complete=false`, `no_open_errors=false`.
- `.venv/bin/python -m unittest discover -s tests` - 558 tests OK.
- `.venv/bin/python -m py_compile main.py sitecustomize.py taksklad/__init__.py src/taksklad/*.py tests/*.py backend/app/*.py tools/feature_acceptance_status.py tools/release_preflight.py` - OK.
- `.venv/bin/python tools/release_preflight.py --skip-network` - `status=ok`.
- `git diff --check` - OK.

### DESK-12 current print settings flow

**Файлы:** `src/taksklad/printing.py`, `src/taksklad/app_printing.py`, `src/taksklad/app_finish.py`, `tests/test_printing.py`, `tests/test_main_refactor_contract.py`, `tests/test_desktop_ui_contract.py`, `docs/taksklad-feature-user-stories.xlsx`, `docs/implementation-log.md`, `docs/changelog.md`.

**Что стало:**

- Выбранные в окне печати принтер и размер этикетки теперь применяются к текущей печати сразу.
- Печать больше не зависит от того, удалось ли сохранить эти параметры в `print_settings`.
- Это работает и при завершении заказа, и при допечатке старых pending-сводок.
- `GAP-012` оставлен `needs_validation`: физическая печать, Windows-драйвер, custom paper size и читаемость этикетки всё еще требуют ручной проверки на складском ПК.

**Проверки:**

- `.venv/bin/python -m unittest tests.test_printing tests.test_pending_store tests.test_main_refactor_contract tests.test_desktop_ui_contract` - 62 tests OK.
- `.venv/bin/python -m unittest discover -s tests` - 552 tests OK.
- `.venv/bin/python -m py_compile src/taksklad/printing.py src/taksklad/app_printing.py src/taksklad/app_finish.py tests/test_printing.py tests/test_main_refactor_contract.py tests/test_desktop_ui_contract.py` - OK.
- `.venv/bin/python -m py_compile main.py sitecustomize.py taksklad/__init__.py src/taksklad/*.py tests/*.py backend/app/*.py tools/feature_acceptance_status.py tools/release_preflight.py` - OK.
- `.venv/bin/python -m unittest tests.test_feature_user_stories_register tests.test_feature_acceptance_status` - 14 tests OK.
- `.venv/bin/python tools/feature_acceptance_status.py` - `status=ok`, `manual_complete=false`, `no_open_errors=false`.
- `.venv/bin/python tools/release_preflight.py --skip-network` - `status=ok`.
- `git diff --check` - OK.

### DESK-11 pending print queue safety

**Файлы:** `src/taksklad/pending_store.py`, `src/taksklad/app_finish.py`, `src/taksklad/app_printing.py`, `tests/test_pending_store.py`, `tests/test_printing.py`, `tests/test_desktop_ui_contract.py`, `docs/taksklad-feature-user-stories.xlsx`, `docs/implementation-log.md`, `docs/changelog.md`.

**Что стало:**

- Завершение заказа теперь не стартует печать, если сводный лист не удалось надежно поставить в `pending_prints`.
- После успешной печати заказ не идет в backend complete/Google archive, если запись не удалось убрать из очереди печати.
- Ручная допечатка pending-сводок теперь тоже проверяет успешное удаление из очереди и показывает ошибку вместо ложного успеха.
- `GAP-011` оставлен `needs_validation`: физическая печать на Windows с реальным драйвером всё еще требует ручной acceptance.

**Проверки:**

- `.venv/bin/python -m unittest tests.test_pending_store tests.test_printing tests.test_main_refactor_contract tests.test_desktop_ui_contract` - 60 tests OK.
- `.venv/bin/python -m unittest discover -s tests` - 550 tests OK.
- `.venv/bin/python -m py_compile src/taksklad/pending_store.py src/taksklad/app_finish.py src/taksklad/app_printing.py tests/test_pending_store.py tests/test_printing.py tests/test_desktop_ui_contract.py` - OK.
- `.venv/bin/python -m py_compile main.py sitecustomize.py taksklad/__init__.py src/taksklad/*.py tests/*.py backend/app/*.py tools/feature_acceptance_status.py tools/release_preflight.py` - OK.
- `.venv/bin/python -m unittest tests.test_feature_user_stories_register tests.test_feature_acceptance_status` - 14 tests OK.
- `.venv/bin/python tools/feature_acceptance_status.py` - `status=ok`, `manual_complete=false`, `no_open_errors=false`.
- `.venv/bin/python tools/release_preflight.py --skip-network` - `status=ok`.
- `git diff --check` - OK.

### Desktop hard-error final-position action fix

**Файлы:** `src/taksklad/app_scanning.py`, `tests/test_desktop_ui_contract.py`, `docs/taksklad-feature-user-stories.xlsx`, `docs/implementation-log.md`, `docs/changelog.md`.

**Что стало:**

- После hard error при сохранении последней позиции приложение больше не включает `Следующая позиция`.
- Оператор остается на текущей позиции и может повторно нажать `ЗАВЕРШИТЬ ЗАКАЗ`.
- Для неполной позиции после ошибки обе action-кнопки остаются disabled.
- `GAP-010` оставлен `needs_validation`: нужна ручная проверка видимого Windows UI сценария.

**Проверки:**

- `.venv/bin/python -m unittest tests.test_desktop_ui_contract.DesktopUiContractTests.test_next_product_hard_error_keeps_final_position_actions_consistent` - OK.
- `.venv/bin/python -m unittest tests.test_desktop_ui_contract tests.test_desktop_pending_store tests.test_backend_bridge` - 59 tests OK.
- `.venv/bin/python -m unittest discover -s tests` - 546 tests OK.
- `.venv/bin/python -m py_compile main.py sitecustomize.py taksklad/__init__.py src/taksklad/*.py tests/*.py backend/app/*.py tools/feature_acceptance_status.py tools/release_preflight.py` - OK.
- `.venv/bin/python -m unittest tests.test_feature_user_stories_register tests.test_feature_acceptance_status` - 14 tests OK.
- `.venv/bin/python tools/feature_acceptance_status.py` - `status=ok`, `manual_complete=false`, `no_open_errors=false`.
- `.venv/bin/python tools/release_preflight.py --skip-network` - `status=ok`.
- `git diff --check` - OK.

### Desktop undo pending-save state fix

**Файлы:** `src/taksklad/app_scanning.py`, `tests/test_desktop_ui_contract.py`, `docs/taksklad-feature-user-stories.xlsx`, `docs/implementation-log.md`, `docs/changelog.md`.

**Что стало:**

- При откате saved-КИЗа через локальную `pending_saves` запись приложение теперь сразу синхронизирует `saved_codes_count` с оставшимися кодами.
- UI больше не считает откатанный pending-save КИЗ сохраненным, поэтому следующий undo/save/finish state остается консистентным без Google Sheets или VDS.
- `GAP-009` оставлен `needs_validation`: откат уже синхронизированного saved-кода без pending-записи всё еще требует live backend/Google и ручной Windows acceptance.

**Проверки:**

- `.venv/bin/python -m unittest tests.test_desktop_ui_contract.DesktopUiContractTests.test_undo_saved_pending_save_keeps_state_consistent_without_google_or_backend` - OK.
- `.venv/bin/python -m unittest tests.test_desktop_ui_contract tests.test_desktop_pending_store tests.test_backend_bridge` - 58 tests OK.
- `.venv/bin/python -m unittest discover -s tests` - 545 tests OK.
- `.venv/bin/python -m py_compile main.py sitecustomize.py taksklad/__init__.py src/taksklad/*.py tests/*.py backend/app/*.py tools/feature_acceptance_status.py tools/release_preflight.py` - OK.
- `.venv/bin/python -m unittest tests.test_feature_user_stories_register tests.test_feature_acceptance_status` - 14 tests OK.
- `.venv/bin/python tools/feature_acceptance_status.py` - `status=ok`, `manual_complete=false`, `no_open_errors=false`.
- `.venv/bin/python tools/release_preflight.py --skip-network` - `status=ok`.
- `git diff --check` - OK.

### Desktop duplicate/backend conflict guard coverage

**Файлы:** `tests/test_desktop_ui_contract.py`, `tests/test_refresh_fallback.py`, `docs/taksklad-feature-user-stories.xlsx`, `docs/implementation-log.md`, `docs/changelog.md`.

**Что стало:**

- `DESK-08` получил явные regression checks, что duplicate guards в desktop стоят до локального backup и до backend queue.
- Refresh contract теперь проверяет, что pending backend scan codes входят в `all_existing_codes` и блокируют повторный локальный прием КИЗа до синхронизации.
- `GAP-008` оставлен `needs_validation`: live cross-PC Windows/backend sync всё ещё требует ручной приемки.

**Проверки:**

- `.venv/bin/python -m unittest tests.test_desktop_ui_contract.DesktopUiContractTests.test_scan_rejects_duplicates_before_local_backup_and_backend_queue tests.test_desktop_ui_contract.DesktopUiContractTests.test_backend_blocked_scan_removes_code_and_keeps_position_open tests.test_refresh_fallback.RefreshFallbackTests.test_refresh_exposes_pending_backend_codes_as_known_duplicates tests.test_backend_bridge.BackendBridgeTests.test_backend_queue_drops_non_retryable_duplicate_scan_conflict tests.test_backend_api_persistence.BackendApiPersistenceTests.test_scan_create_is_idempotent_for_same_item_and_rejects_cross_order_duplicate` - 5 tests OK.
- `.venv/bin/python -m unittest tests.test_refresh_fallback tests.test_desktop_ui_contract` - 50 tests OK.
- `.venv/bin/python -m unittest discover -s tests` - 541 tests OK.
- `.venv/bin/python -m py_compile main.py sitecustomize.py taksklad/__init__.py src/taksklad/*.py tests/*.py backend/app/*.py tools/feature_acceptance_status.py tools/release_preflight.py` - OK.
- `.venv/bin/python tools/feature_acceptance_status.py` - `status=ok`, `manual_complete=false`, `no_open_errors=false`.
- `.venv/bin/python tools/release_preflight.py --skip-network` - `status=ok`.
- `git diff --check` - OK.

### Readiness redaction and refresh live evidence

**Файлы:** `backend/app/health_service.py`, `tests/test_backend_api_persistence.py`, `docs/taksklad-feature-user-stories.xlsx`, `docs/implementation-log.md`, `docs/changelog.md`, `backend/README.md`, `docs/event-queue-lifecycle.md`.

**Что стало:**

- Public `/ready` больше не раскрывает dynamic suffix в queue `event_type`: state-store типы вида `telegram_chat_state:<id>` агрегируются как `telegram_chat_state:*`.
- Compact readiness errors больше не отдают raw payload, idempotency key и linked fields; остаются только безопасные поля статуса, возраста и sanitized `last_error`.
- Admin `/api/v1/admin/events` не менялся, потому он остается за auth/session и нужен для операторской диагностики.
- `tests/test_refresh_fallback.py` теперь прямо проверяет, что `refresh_from_sheet(initial=False)` при выбранной позиции не вызывает reset и показывает статус `текущая позиция сохранена`.
- По `DESK-03/GAP-003` добавлено live evidence для безопасной части backend refresh: публичные `/health` и `/ready` отвечают `status=ok`, backend version `2.0.21`; live SkladBot/Windows UI acceptance остаются pending.

**Проверки:**

- `.venv/bin/python -m unittest tests.test_refresh_fallback` - 9 tests OK.
- `.venv/bin/python -m unittest tests.test_backend_api_persistence.BackendApiPersistenceTests.test_health_is_lightweight_and_readiness_reports_sanitized_db_queue_status` - OK.
- `.venv/bin/python tools/release_preflight.py` - `status=ok`.
- `.venv/bin/python -m unittest discover -s tests` - 539 tests OK.
- `.venv/bin/python -m py_compile main.py sitecustomize.py taksklad/__init__.py src/taksklad/*.py tests/*.py backend/app/*.py tools/feature_acceptance_status.py tools/release_preflight.py` - OK.
- `.venv/bin/python tools/feature_acceptance_status.py` - `status=ok`, `manual_complete=false`, `no_open_errors=false`.
- `.venv/bin/python tools/release_preflight.py --skip-network` - `status=ok`.
- `git diff --check` - OK.

### Feature user stories register, reconciliation fixes and order-list UX fix

**Файлы:** `docs/taksklad-feature-user-stories.xlsx`, `docs/README.md`, `docs/local-development-setup.md`, `docs/manual-acceptance-runbook.md`, `docs/taksklad-system-stack-overview.md`, `docs/report-source-rules.md`, `backend/app/reconciliation_service.py`, `backend/app/logistics_service.py`, `frontend/src/App.tsx`, `frontend/src/api.ts`, `src/taksklad/order_list_models.py`, `tools/feature_acceptance_status.py`, `tools/prepare_acceptance_kit.py`, `tests/test_feature_acceptance_status.py`, `tests/test_feature_user_stories_register.py`, `tests/test_acceptance_excel_generator.py`, `tests/test_reconciliation_service.py`, `tests/test_order_list_models.py`, `tests/test_backend_api_persistence.py`.

**Что стало:**

- Добавлен канонический spreadsheet `docs/taksklad-feature-user-stories.xlsx` с листами `Summary`, `User Stories`, `Test Loop`, `Errors`, `Sources`, `Manual Acceptance`.
- Реестр фиксирует 47 user stories, expected behaviour, evidence files, auto/manual status, live/hardware dependency и ошибки.
- Добавлен регрессионный тест `tests/test_feature_user_stories_register.py`, который защищает spreadsheet от schema drift, дублей Feature ID, stale pytest-команд и битых evidence paths.
- Добавлен `tools/feature_acceptance_status.py`: JSON-status для реестра функций, строгая проверка обязательных колонок, точного набора manual-строк, неизвестных статусов и gate-флаги `--require-manual-complete` / `--require-no-open-errors`.
- `feature_acceptance_status.py` явно не является production release `GO/NO-GO`; релизный канон остается `tools/release_go_no_go.py` + `outputs/taksklad_acceptance/ACCEPTANCE_RESULTS.md`.
- В spreadsheet исправлена классификация `DESK-07` и `DESK-17`: обе истории имеют auto coverage, но требуют manual Windows UI acceptance.
- Закрыты локально проверяемые gaps:
  - `GAP-004`: добавлен тест большого списка заказов на 2000 карточек и поиск без занижения общего количества;
  - `GAP-005`: добавлена проверка, что все настроенные product images реально лежат в `assets/product_images` и не пустые.
- После selector retest закрыты уже существующим auto coverage:
  - `GAP-006`, `GAP-007`: SKU prefixes, wrong-SKU и aggregate box `50` закреплены в `tests/test_scan_quantities.py`;
  - `GAP-019`: `/health` и `/ready` readiness contracts закреплены в backend persistence tests;
  - `GAP-030`: returned/order item reconciliation regressions закреплены в `tests/test_reconciliation_service.py`;
  - `GAP-040`: SkladBot daily report created-date rule закреплен fake-client tests;
  - `GAP-042`: day report business timezone закреплен API test;
  - `GAP-043`: KIZ XLSX partial date export и same filename/source_key закреплены API tests.
- Исправлен ложный daily reconciliation mismatch для частично выполненного multi-SKU заказа: Google-статус позиции теперь сравнивается с item-level статусом, а returned-order остается совместимым с Google `Выполнено`.
- Reconciliation без `report_date` теперь берет текущую дату в бизнес-таймзоне склада, а не UTC.
- Поиск в desktop order list больше не занижает summary карточки multi-SKU заказа: карточка показывает полный план заказа, даже если поиск совпал только с одной SKU.
- Логистический XLSX больше не теряет delivery-заказы без координат: основной лист `Заявки` остается только для маршрутизируемых строк, а no/invalid coordinates выводятся в отдельный лист `Требуют координаты`.
- Если на дату есть только delivery-заказы без координат, `/api/v1/logistics/report` теперь возвращает XLSX с листом проблем вместо `404`; pickup-only и stock-shortage blocked по-прежнему не попадают в логистику.
- Закрыт локально проверяемый return gap: backend `/api/v1/returns/{order_id}` является source of truth для возврата, Google Sheets остается best-effort mirror/export, а desktop в backend mode не пишет Google-only возврат без backend order id.
- Закрыт Google export queue lock gap: PostgreSQL intentionally не использует session-level advisory lock, чтобы не ловить stuck busy на pooled connection; claim pending events закреплен контрактом `FOR UPDATE SKIP LOCKED`.
- Закрыт migration contract gap: `001_initial_schema.sql`, Alembic baseline/head и forward-only downgrade posture закреплены tests against core tables/indexes and revision chain.
- Закрыт web/admin gap `GAP-024`: action bar теперь показывает `Удалить из активных` для одного активного заказа без КИЗов; UI отправляет `reason`, `idempotency_key` и `expected_updated_at`, а backend сохраняет проверки active/no-scans/pending Google/audit/export.
- Закрыт web/admin gap `GAP-020`: `/api/v1/admin/table` теперь поддерживает `limit` + `offset` и возвращает `limit`, `offset`, `row_count`, `total_rows`, `has_more`; web UI показывает загружено/всего и дает догрузить следующую страницу вместо тихой обрезки на 5000 строк.
- Закрыт desktop/runtime gap `GAP-017`: critical/UI exceptions теперь отправляют только короткий Telegram alert с текстом ошибки и ссылкой на локальный лог; operational documents/error log больше не прикладываются автоматически на аварийном пути.
- Закрыт desktop/startup gap `GAP-001`: добавлен `--smoke-gui`, который строит Tkinter UI, flushes layout и закрывает окно без `mainloop`; Windows release workflow теперь запускает `--smoke-import` и `--smoke-gui` для onefile и onedir из clean temp dirs.
- Закрыт desktop/UI gap `GAP-002`: `--smoke-gui` теперь проверяет semantic snapshot главного экрана, включая список заказов, поиск, текущую позицию, фото/GTIN, поле скана, action buttons, статистику, backend status и status/toast widgets.
- Документация локальной среды уточнена: backend-тестам нужен Python 3.10+, рекомендуется Python 3.12; локальная `.venv` пересобрана на Python 3.12.13, старая Python 3.9-среда сохранена как `archive/local-venv-backups/.venv.py39-backup-20260622T1408`.
- Acceptance-kit generator теперь берет текущий `APP_VERSION` из `src/taksklad/config.py`, чтобы README/template/result checklist не оставались на старом `2.0.15`.

**Проверки:**

- `/tmp/taksklad-test-py312/bin/python -m unittest tests.test_order_list_models tests.test_reconciliation_service` - 10 tests OK.
- `/tmp/taksklad-test-py312/bin/python -m unittest tests.test_backend_api_persistence.BackendApiPersistenceTests.test_logistics_report_uses_shipment_date_coordinates_and_prices tests.test_backend_api_persistence.BackendApiPersistenceTests.test_logistics_report_keeps_unrouteable_orders_on_separate_sheet tests.test_backend_api_persistence.BackendApiPersistenceTests.test_logistics_report_returns_unrouteable_sheet_when_date_has_no_routeable_orders tests.test_backend_api_persistence.BackendApiPersistenceTests.test_logistics_report_404_when_date_has_only_pickup_orders tests.test_backend_api_persistence.BackendApiPersistenceTests.test_logistics_report_normalizes_three_part_coordinates tests.test_backend_skladbot_request_dry_run.BackendSkladBotRequestDryRunTests.test_logistics_report_excludes_stock_shortage_blocked_order` - 6 tests OK.
- `/tmp/taksklad-test-py312/bin/python -m unittest tests.test_backend_api_persistence.BackendApiPersistenceTests.test_return_lookup_and_mark_returned_excludes_order_from_active_list tests.test_backend_api_persistence.BackendApiPersistenceTests.test_return_releases_kiz_for_new_outbound_scan_with_history tests.test_backend_api_persistence.BackendApiPersistenceTests.test_failed_return_does_not_release_kiz_for_new_order tests.test_backend_api_persistence.BackendApiPersistenceTests.test_mark_return_exports_archive_and_returns_to_google_sheets_best_effort tests.test_backend_api_persistence.BackendApiPersistenceTests.test_mark_return_rejects_mismatched_confirmed_items_without_side_effects tests.test_desktop_ui_contract.DesktopUiContractTests.test_return_mark_sends_confirmed_items_to_backend tests.test_desktop_ui_contract.DesktopUiContractTests.test_backend_returns_list_reads_backend_without_google_fallback tests.test_desktop_ui_contract.DesktopUiContractTests.test_backend_return_lookup_reads_backend_without_google_fallback tests.test_desktop_ui_contract.DesktopUiContractTests.test_backend_return_rejects_google_order_without_backend_id tests.test_desktop_ui_contract.DesktopUiContractTests.test_backend_return_uses_backend_id_for_google_order tests.test_desktop_ui_contract.DesktopUiContractTests.test_legacy_return_keeps_google_write_fallback_for_google_order tests.test_desktop_ui_contract.DesktopUiContractTests.test_return_confirmed_items_are_built_from_backend_items` - 12 tests OK.
- `/tmp/taksklad-test-py312/bin/python -m unittest tests.test_backend_google_sheets_pending.GoogleSheetsPendingLockTests.test_postgres_lock_does_not_use_session_level_advisory_lock tests.test_backend_google_sheets_pending.GoogleSheetsPendingLockTests.test_postgres_pending_selection_uses_skip_locked_row_lock tests.test_backend_google_sheets_pending.GoogleSheetsPendingLockTests.test_rate_limit_keeps_event_pending_and_stops_batch` - 3 tests OK.
- `/tmp/taksklad-test-py312/bin/python -m unittest tests.test_backend_skeleton.BackendSkeletonTests.test_initial_schema_contains_mvp_tables_and_constraints tests.test_backend_skeleton.BackendSkeletonTests.test_alembic_baseline_covers_current_schema_without_secrets tests.test_backend_skeleton.BackendSkeletonTests.test_sql_bootstrap_and_alembic_migrations_keep_forward_only_contract tests.test_backend_skeleton.BackendSkeletonTests.test_deploy_runbook_uses_alembic_for_normal_production_upgrades` - 4 tests OK.
- `/tmp/taksklad-test-py312/bin/python -m unittest tests.test_backend_api_persistence.BackendApiPersistenceTests.test_admin_state_changing_actions_require_reason tests.test_backend_api_persistence.BackendApiPersistenceTests.test_delete_active_order_removes_unscanned_order_and_queues_google_delete tests.test_backend_api_persistence.BackendApiPersistenceTests.test_delete_active_order_idempotency_prevents_duplicate_audit_and_export tests.test_backend_api_persistence.BackendApiPersistenceTests.test_delete_active_order_rejects_order_with_scans tests.test_backend_telegram_import.BackendTelegramImportTests.test_telegram_worker_manual_delete_active_order_calls_safe_backend_endpoint tests.test_backend_telegram_import.BackendTelegramImportTests.test_telegram_worker_manual_delete_refuses_started_order_before_backend_call` - 6 tests OK.
- `.venv/bin/python -m unittest tests.test_backend_api_persistence.BackendApiPersistenceTests.test_admin_table_totals_are_not_limited_by_row_limit tests.test_backend_api_persistence.BackendApiPersistenceTests.test_admin_table_supports_offset_pagination_metadata` - 2 tests OK.
- `.venv/bin/python -m unittest tests.test_desktop_ui_contract.DesktopUiContractTests.test_warning_and_info_use_non_blocking_status_notice tests.test_desktop_ui_contract.DesktopUiContractTests.test_critical_errors_send_alert_without_operational_documents tests.test_desktop_ui_contract.DesktopUiContractTests.test_desktop_errors_use_non_blocking_status_notice tests.test_desktop_diagnostics` - 4 tests OK.
- `.venv/bin/python -m unittest tests.test_desktop_smoke tests.test_windows_release_workflow tests.test_code_organization tests.test_startup_check` - 12 tests OK.
- `/tmp/taksklad-test-py312/bin/python -m unittest tests.test_order_list_models tests.test_product_images` - 8 tests OK.
- `/tmp/taksklad-test-py312/bin/python -m unittest tests.test_scan_quantities tests.test_reconciliation_service.ReconciliationServiceTests.test_returned_order_with_completed_google_status_is_not_status_mismatch tests.test_reconciliation_service.ReconciliationServiceTests.test_completed_item_in_active_multi_sku_order_matches_completed_google_row tests.test_skladbot_daily_report.SkladBotDailyReportTests.test_daily_report_uses_created_date_not_completion_cutoff tests.test_skladbot_daily_report.SkladBotDailyReportTests.test_daily_report_skips_completed_request_when_created_date_is_stale tests.test_backend_api_persistence.BackendApiPersistenceTests.test_health_is_lightweight_and_readiness_reports_sanitized_db_queue_status tests.test_backend_api_persistence.BackendApiPersistenceTests.test_readiness_accepts_incident_schema_head_revision tests.test_backend_api_persistence.BackendApiPersistenceTests.test_readiness_degrades_when_migration_state_is_missing_or_wrong tests.test_backend_api_persistence.BackendApiPersistenceTests.test_day_report_counts_scan_by_business_timezone tests.test_backend_api_persistence.BackendApiPersistenceTests.test_kiz_reports_show_source_file_progress_and_allow_partial_date_export tests.test_backend_api_persistence.BackendApiPersistenceTests.test_kiz_source_file_report_separates_same_filename_by_import` - 17 tests OK.
- `/tmp/taksklad-test-py312/bin/python -m unittest tests.test_acceptance_excel_generator tests.test_feature_acceptance_status tests.test_feature_user_stories_register` - 19 tests OK.
- 28/28 уникальных команд из `Test Loop` spreadsheet прошли на Python 3.12.
- `.venv/bin/python -m unittest discover -s tests` - 538 tests OK.
- `.venv/bin/python -m py_compile main.py sitecustomize.py taksklad/__init__.py src/taksklad/*.py tests/*.py backend/app/*.py tools/feature_acceptance_status.py` - OK.
- `.venv/bin/python tools/feature_acceptance_status.py` - `status=ok`, `manual_complete=false`, `no_open_errors=false`.
- `.venv/bin/python tools/feature_acceptance_status.py --require-manual-complete` - exit `3`, потому 45 manual rows pending.
- `.venv/bin/python tools/feature_acceptance_status.py --require-no-open-errors` - exit `4`, потому 27 open Errors rows remain.
- `.venv/bin/python tools/release_preflight.py --skip-network` - `status=ok`.
- `npm run build` в `frontend` - OK.
- `docker compose --env-file deploy/vds/.env.example -f deploy/vds/docker-compose.yml config` - OK.
- `bash -n deploy/vds/*.sh` - OK.
- `npm audit --audit-level=high` в `frontend` - 0 vulnerabilities.

**Что осталось ручным:**

- Windows UI, физическая печать, auto-updater, live Telegram, Google Sheets, SkladBot API и VDS acceptance отмечены в листе `Manual Acceptance`; без соответствующего окружения эти сценарии не считаются полностью закрытыми.

## 2026-06-21

### Ежедневный SkladBot отчет теперь фильтруется по дате создания заявки

**Файлы:** `backend/app/skladbot_daily_report.py`, `tests/test_skladbot_daily_report.py`, `docs/report-source-rules.md`, `docs/implementation-log.md`, `docs/changelog.md`.

**Что стало:**

- В daily report попадают только заявки `Выполнена` + `В архиве`, у которых `created_at`/`createdAt` равен дате отчета в бизнес-таймзоне.
- `completedAt`, `archivedAt`, `updatedAt` и `Дата выгрузки` больше не переносят заявку в отчет другого дня.
- В XLSX причина включения стала `создана`; старый fallback `впервые найдена выполненной` удален из правила отбора.
- Registry `pending_events` сохранен как защита от повторной плановой отправки.

**Проверки:**

- `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:. /tmp/taksklad-fulltest-codex-venv/bin/python -m unittest tests.test_skladbot_daily_report` - 19 tests OK.
- `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:. /tmp/taksklad-fulltest-codex-venv/bin/python -m unittest tests.test_skladbot_daily_report tests.test_backend_telegram_import` - 82 tests OK.
- `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:. /tmp/taksklad-fulltest-codex-venv/bin/python -m compileall -q backend/app/skladbot_daily_report.py tests/test_skladbot_daily_report.py` - OK.
- `git diff --check` - OK.
- VDS restore point: `/opt/taksklad/restore_points/pre-daily-report-created-date-20260621T175350Z`.
- VDS Postgres backup: `/opt/taksklad/backups/postgres/taksklad-postgres-20260621T175350Z.sql.gz`.
- VDS selective deploy: пересобран и перезапущен `telegram-worker`; `https://api.taksklad.uz/health` и `/ready` вернули `version=2.0.21`, `status=ok`.
- VDS `./deploy/vds/acceptance_status.sh` - общий `status=ok`; свежие логи `telegram-worker` и `backend-api` без `ERROR/Traceback/Exception/CRITICAL/failed`.

### Карточный список заказов по макету и forced rollout 2.0.21

**Файлы:** `src/taksklad/order_list_models.py`, `src/taksklad/order_list_widgets.py`, `src/taksklad/app_layout.py`, `src/taksklad/app_order_display.py`, `src/taksklad/config.py`, `backend/app/settings.py`, `tools/release_preflight.py`, `tools/build_windows_test_archive.ps1`, `deploy/vds/acceptance_status.sh`, `tests/test_order_list_models.py`, `tests/test_order_list_ui_contract.py`, `tests/test_desktop_ui_contract.py`, `tests/test_code_organization.py`, `tests/test_release_preflight.py`, `tests/test_vds_acceptance_scripts.py`, `tests/test_windows_test_build_helper.py`, `docs/user-business-process-guide.md`.

**Что стало:**

- Левая панель `Заказы для КИЗов` переведена со старого `Listbox` на карточный прокручиваемый список в стиле утвержденного макета.
- Каждая карточка показывает юрлицо, номер заявки SkladBot, дату отгрузки, количество SKU и план блоков.
- Поиск сохранил фильтрацию по клиенту, адресу, оплате, заявке, торговому представителю и товару.
- Выбор карточки хранит реальный `group_key` заказа, поэтому сканирование, завершение, печать и переход к первой незавершенной позиции не меняют бизнес-логику.
- Заголовки дат остаются визуальными разделителями и не могут быть выбраны как заказ.
- Добавлены отдельные model/widget модули, чтобы логика списка не возвращалась в `main.py`.
- `APP_VERSION` desktop/backend и release guard подняты до `2.0.21` для нового обязательного обновления складских ПК.

**Проверки:**

- `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:. /tmp/taksklad-fulltest-codex-venv/bin/python -m unittest tests.test_order_list_models tests.test_order_list_ui_contract tests.test_desktop_ui_contract tests.test_main_refactor_contract tests.test_code_organization` - 53 tests OK.
- `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:. /tmp/taksklad-fulltest-codex-venv/bin/python -m compileall -q src/taksklad/order_list_models.py src/taksklad/order_list_widgets.py src/taksklad/app_layout.py src/taksklad/app_order_display.py tests/test_order_list_models.py tests/test_order_list_ui_contract.py tests/test_desktop_ui_contract.py tests/test_code_organization.py` - OK.
- `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:. /tmp/taksklad-fulltest-codex-venv/bin/python -m unittest discover -s tests` - 509 tests OK.
- `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:. /tmp/taksklad-fulltest-codex-venv/bin/python -m compileall -q main.py sitecustomize.py taksklad src/taksklad backend/app tests tools` - OK.
- `npm --prefix frontend run build` - OK.
- `docker compose --env-file deploy/vds/.env.example -f deploy/vds/docker-compose.yml config` - OK.
- `docker compose --env-file deploy/traefik/.env.example -f deploy/traefik/docker-compose.yml config` - OK.
- `git diff --check` - OK.
- GitHub Actions `Build Windows Release` для `v2.0.21`, run `27909016791` - success.
- Release assets: `TakSklad.exe` SHA256 `a10e31e73b282ac4b3056fb3d2c60cad5e957412d82df4a60634a5d2939c1c77`; `TakSklad-windows-x64.zip` SHA256 `3385bb24dae1b4a0b7a923700ac730b87c7a06db8f83a9bcfd873823148131b5`.
- `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:. /tmp/taksklad-fulltest-codex-venv/bin/python tools/release_preflight.py --verify-downloads --timeout 120` - OK, GitHub assets скачаны и SHA совпали, backend health `2.0.21`.
- VDS backup перед deploy: `/opt/taksklad/backups/postgres/taksklad-postgres-20260621T153654Z.sql.gz`.
- VDS deploy: пересобраны `backend-api`, `telegram-worker`, `google-sheets-sync-worker`, `skladbot-worker`; `https://api.taksklad.uz/health` и `/ready` вернули `version=2.0.21`, `status=ok`.
- VDS `./deploy/vds/acceptance_status.sh` - общий `status=ok`, manifest `2.0.21`, services running; свежие логи backend/workers без `error|exception|traceback|critical|failed`.

### Модульный desktop и принудительный rollout 2.0.20

**Файлы:** `src/taksklad/main.py`, `src/taksklad/app_data_loading.py`, `src/taksklad/app_finish.py`, `src/taksklad/app_layout.py`, `src/taksklad/app_order_display.py`, `src/taksklad/app_returns.py`, `src/taksklad/app_runtime.py`, `src/taksklad/app_scanning.py`, `src/taksklad/backend_flow.py`, `src/taksklad/desktop_refresh_service.py`, `src/taksklad/desktop_scan_rules.py`, `src/taksklad/config.py`, `backend/app/settings.py`, `tools/*`, `deploy/vds/acceptance_status.sh`, `tests/*`.

**Что стало:**

- `main.py` снова приведен к роли entrypoint/wiring: startup, `ScanningApp.__init__`, mixin composition.
- Workflow сканирования, отображения заказа, finish, refresh, runtime, returns и backend blockers вынесены в отдельные owner-модули.
- Старые публичные экспорты `taksklad.main` сохранены там, где тесты и внешние вызовы еще используют прежнюю точку входа.
- Добавлен guard на размер `main.py`, чтобы workflow-логика не возвращалась обратно в entrypoint.
- `APP_VERSION` desktop/backend и release guard подняты до `2.0.20` для нового forced rollout.

**Проверки:**

- `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:. /tmp/taksklad-fulltest-codex-venv/bin/python -m unittest discover -s tests` - 502 tests OK.
- `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:. /tmp/taksklad-fulltest-codex-venv/bin/python -m compileall -q main.py sitecustomize.py taksklad src/taksklad backend/app tests tools` - OK.
- `npm --prefix frontend run build` - OK.
- `npm --prefix frontend audit --audit-level=high` - 0 vulnerabilities.
- `docker compose --env-file deploy/vds/.env.example -f deploy/vds/docker-compose.yml config` - OK.
- `docker compose --env-file deploy/traefik/.env.example -f deploy/traefik/docker-compose.yml config` - OK.
- `for f in deploy/vds/*.sh; do bash -n "$f"; done` - OK.
- `git diff --check` - OK.

### UI polish сканирования и принудительный rollout 2.0.19

**Файлы:** `src/taksklad/ui_widgets.py`, `src/taksklad/main.py`, `src/taksklad/app_day_end.py`, `src/taksklad/config.py`, `backend/app/settings.py`, `tools/release_preflight.py`, `tools/build_windows_test_archive.ps1`, `deploy/vds/acceptance_status.sh`, `tests/*`.

**Что стало:**

- Кнопки получили более мягкий радиус по умолчанию и сглаженные углы.
- Hover теперь слегка осветляет кнопку, а не затемняет ее.
- В карточке текущей позиции уменьшены слишком крупные шрифты юрлица/товара.
- Прогресс текущей позиции вынесен в отдельный компактный блок.
- Статистика перешла на KPI-плитки: значения и подписи больше не съезжают относительно друг друга.
- Служебные строки текущей позиции стали приглушенными, чтобы не спорить с главным SKU.
- Toast ошибок оставлен прямоугольным с `radius=8` и автоскрытием через 5 секунд.
- `APP_VERSION` desktop/backend и release guard подняты до `2.0.19` для нового forced rollout.

**Проверки:**

- `PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m unittest tests.test_desktop_ui_contract tests.test_product_images tests.test_scan_quantities tests.test_release_preflight tests.test_vds_acceptance_scripts tests.test_windows_test_build_helper` - 64 tests OK.
- `PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m compileall -q src/taksklad/main.py src/taksklad/ui_widgets.py src/taksklad/app_day_end.py src/taksklad/config.py backend/app/settings.py tools/release_preflight.py` - OK.
- `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /tmp/taksklad-venv312/bin/python -m unittest discover -s tests` - 493 tests OK.

## 2026-06-20

### Фото товара в экране сканирования

**Файлы:** `src/taksklad/main.py`, `src/taksklad/product_images.py`, `src/taksklad/config.py`, `assets/product_images/*`, `.github/workflows/build-windows-release.yml`, `tools/build_windows_test_archive.ps1`, `tests/*`.

**Что стало:**

- В карточке текущей позиции desktop теперь есть фото товара и GTIN рядом с реквизитами заказа.
- Поддержаны 6 рабочих Chapman SKU: Brown OP, Brown SSL, Gold SSL, Green OP, RED OP, RED SSL.
- Если фото не найдено, экран показывает заглушку и не блокирует сканирование.
- Палитра и отступы склада подогнаны под теплый макет: мягкий фон, золотой акцент, зеленый прогресс, красный toast.
- Windows release/test build теперь упаковывает `assets/product_images`, чтобы фото были доступны в собранном `TakSklad.exe`.
- `APP_VERSION` desktop/backend и release guard подняты до `2.0.18` для нового forced rollout.

**Проверки:**

- `PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m unittest tests.test_product_images tests.test_desktop_ui_contract tests.test_windows_release_workflow tests.test_windows_test_build_helper tests.test_scan_quantities` - 48 tests OK.
- `PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m compileall -q src/taksklad/product_images.py src/taksklad/main.py tests/test_product_images.py tests/test_desktop_ui_contract.py` - OK.
- `PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m unittest discover -s tests` - 491 tests OK.

### Daily reconciliation: returned больше не считается Google drift

**Файлы:** `backend/app/google_sheets_exporter.py`, `tests/test_reconciliation_service.py`.

**Что стало:**

- `returned` теперь мапится в Google-статус `Выполнено` при сверке DB/Google mirror.
- Это убирает ложный `google_mirror_mismatch`, когда заказ уже возвращен в DB, а Google корректно хранит основной статус `Выполнено` и отдельные return-колонки.

**Проверки:**

- `PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m unittest tests.test_reconciliation_service` - 4 tests OK.
- `PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m compileall -q backend/app/google_sheets_exporter.py tests/test_reconciliation_service.py` - OK.

### Принудительное обновление 2.0.17 с диагностикой wrong-SKU

**Файлы:** `src/taksklad/main.py`, `src/taksklad/config.py`, `backend/app/settings.py`, `tools/*`, `deploy/vds/acceptance_status.sh`, `tests/*`.

**Что стало:**

- При ошибке `КИЗ не соответствует товару текущей позиции` desktop показывает не только общий текст, а диагностические данные: товар позиции, ожидаемый SKU, распознанный SKU КИЗа, префикс КИЗа и версию приложения.
- `APP_VERSION` поднят до `2.0.17`, чтобы складские ПК получили новое обязательное обновление и старые копии приложения не продолжали работать молча.
- Релизные guard-скрипты и тесты переведены на ожидаемый forced rollout `2.0.17`.

**Проверки:**

- `PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m unittest tests.test_desktop_ui_contract tests.test_scan_quantities tests.test_release_preflight tests.test_vds_acceptance_scripts tests.test_windows_test_build_helper tests.test_release_go_no_go` - 67 tests OK.
- `PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m unittest discover tests` - 481 tests OK.
- `PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m compileall -q src/taksklad backend/app tests tools main.py` - OK.
- GitHub Actions `Build Windows Release` для `v2.0.17` - success.

### Расширение Chapman SKU для SkladBot и КИЗов

**Файлы:** `backend/app/skladbot_request_dry_run.py`, `backend/app/skladbot_worker.py`, `backend/app/scan_quantities.py`, `backend/app/telegram_worker.py`, `src/taksklad/scan_quantities.py`, `src/taksklad/skladbot.py`, `src/taksklad/config.py`, `tests/*`.

**Что стало:**

- Добавлены SKU `Chapman Brown SSL 100\`20`, `Chapman Green OP 20`, `Chapman RED SSL 100 20` для создания заявок SkladBot.
- КИЗ-проверка теперь различает не только цвет, но и формат SKU: `brown:op`, `brown:ssl`, `red:op`, `red:ssl`, `gold:ssl`, `green:op`.
- Ручной Telegram-заказ получил отдельные кнопки по новым SKU.
- После live-create SkladBot номер заявки явно фиксируется в JSONB DB, а входящая Google mirror sync больше не откатывает существующий WH-R в `Проверяется`, если зеркало еще не успело обновиться.
- Desktop `APP_VERSION` поднят до `2.0.16` для принудительного обновления складских ПК с локальной проверкой новых SKU.

**Проверки:**

- `PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m unittest tests.test_skladbot_sync tests.test_backend_skladbot_worker tests.test_backend_skladbot_request_dry_run tests.test_scan_quantities tests.test_backend_api_persistence tests.test_backend_telegram_import` - 246 tests OK.
- `PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m unittest discover tests` - 479 tests OK.
- `PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m compileall -q backend/app src/taksklad tests tools main.py` - OK.
- `npm --prefix frontend run build` - OK.
- `docker compose --env-file deploy/vds/.env.example -f deploy/vds/docker-compose.yml config` - OK.

## 2026-06-18

### Reconciliation alerts вынесены из рабочей группы

**Файлы:** `backend/app/telegram_worker.py`, `deploy/vds/docker-compose.yml`, `deploy/vds/.env.example`, `tests/test_skladbot_daily_report.py`.

**Что стало:**

- Добавлена настройка `TAKSKLAD_DAILY_RECONCILIATION_CHAT_IDS` для отдельных получателей daily reconciliation alerts.
- Ежедневный SkladBot отчет продолжает уходить в `SKLADBOT_DAILY_REPORT_CHAT_IDS`.
- Если отдельный список alert-чата не задан, сохраняется старое поведение: сверка отправляет уведомления туда же, куда ушел ежедневный отчет.

**Проверки:**

- `./.venv/bin/python -m unittest tests.test_skladbot_daily_report tests.test_reconciliation_service` - 16 tests OK.
- `./.venv/bin/python -m unittest tests.test_backend_api_persistence.BackendApiPersistenceTests` - 81 tests OK.
- `./.venv/bin/python -m unittest discover tests` - 476 tests OK.
- `docker compose --env-file deploy/vds/.env.example -f deploy/vds/docker-compose.yml config` - OK.

## 2026-06-17

### Operations control hardening продолжение

**Файлы:** `backend/app/reconciliation_service.py`, `backend/app/telegram_worker.py`, `backend/app/admin_service.py`, `backend/app/schemas.py`, `frontend/package*.json`, `tests/*`, `docs/*`.

**Что стало:**

- Добавлена ежедневная DB-first сверка Postgres/Google mirror/SkladBot с отдельными счетчиками Google-only, DB-only active, status mismatch, WH-R mismatch и SkladBot gaps.
- Critical reconciliation alerts дедупятся по incident/date/source/chat и содержат следующее действие.
- Google-down по сверке считается mirror issue, а не падением DB workflow.
- Админка показывает заявку возврата SkladBot рядом с исходной WH-R для returned-заказов.
- Frontend build dependency обновлен до Vite 8.0.16 и esbuild 0.28.1, `npm audit --audit-level=high` показывает 0 vulnerabilities.

**Проверки:**

- `./.venv/bin/python -m unittest discover -s tests` - 475 tests OK.
- `./.venv/bin/python -m compileall -q backend/app src/taksklad tests tools` - OK.
- `npm --prefix frontend run build` - OK.
- `npm --prefix frontend audit --audit-level=high` - 0 vulnerabilities.
- `docker compose --env-file deploy/vds/.env.example -f deploy/vds/docker-compose.yml config` - OK.
- `./.venv/bin/python tools/release_preflight.py --skip-network` - OK.
- `git diff --check` - OK.

## 2026-06-16

### Production hardening supergoal

**Файлы:** `backend/*`, `src/taksklad/*`, `frontend/src/*`, `deploy/vds/*`, `tools/release_preflight.py`, `tests/*`, `docs/*`, `.supergoal/*`.

**Что стало:**

- Desktop forced-update guard блокирует обычную работу, если обязательное обновление не применено, и показывает понятный recovery path.
- Backend получил Alembic baseline, readiness endpoints, event queue diagnostics и sanitized diagnostics log.
- Операции с КИЗами усилены per-KIZ advisory lock и idempotency, чтобы снизить риск дублей при конкурентных scan/undo/return/reset.
- Admin actions в API, web и Telegram требуют reason/source/actor/idempotency и защищены от stale повторов.
- Web panel показывает readiness, import/event errors, audit details и причины заблокированных действий.
- Telegram ops стали строже: admin-gated manual controls, безопасное удаление только не начатых активных заказов, actionable ошибки отчетов.
- Report rules зафиксированы: day/logistics/KIZ reports читают TakSklad DB, ежедневный SkladBot report читает SkladBot API, Google остается mirror/export.
- Исправлены отчетные edge cases: `acceptedAmount=0`, плохая дата `/skladbot_daily`, маскирование секретов в Telegram/backend ошибках, варианты самовывоза в логистике.

**Rollback:**

- Код откатывать к предыдущему good commit и пересобирать затронутые контейнеры.
- Перед любым production rollback делать Postgres backup. Если уже применялись Alembic migrations, DB downgrade выполнять только отдельным осознанным шагом по runbook.
- Desktop rollout откатывать через release manifest/update URL, не меняя данные в БД.

**Проверки:**

- `./.venv/bin/python -m unittest discover -s tests` - 458 tests OK.
- `./.venv/bin/python -m compileall -q backend/app src/taksklad tests tools` - OK.
- `npm --prefix frontend run build` - OK.
- `docker compose --env-file deploy/vds/.env.example -f deploy/vds/docker-compose.yml config` - OK.
- `./.venv/bin/python tools/release_preflight.py --skip-network` - OK.
- `git diff --check` - OK.

## 2026-06-09

### План и факт в списке заявок SkladBot daily report

**Файлы:** `backend/app/skladbot_daily_report.py`, `tests/test_skladbot_daily_report.py`, `docs/*`.

**Что стало:**

- На листе `Заявки` вместо одной колонки `Блоков` теперь есть `Блоков план`, `Блоков факт`, `Отклонение`.
- На листе `Товары заявок` добавлены `Блоков план`, `Принято факт`, `Блоков факт`, `Отклонение`.
- Кейс приемки `план 1 / acceptedAmount 1750` теперь виден прямо в списке заявок и товарах заявки, а не только в сводке.
- Автоматическая отправка не менялась: ежедневный отчет остается по расписанию `22:00`.

**Проверки:**

- `./.venv/bin/python -m unittest tests.test_skladbot_daily_report` - 7 tests OK.
- `./.venv/bin/python -m unittest discover tests` - 402 tests OK.
- `./.venv/bin/python -m compileall -q backend/app src/taksklad tools main.py tests` - OK.
- `docker compose --env-file deploy/vds/.env.example -f deploy/vds/docker-compose.yml config` - OK.
- `git diff --check` - OK.

### Фактическая приемка в ежедневном SkladBot отчете

**Файлы:** `backend/app/skladbot_daily_report.py`, `backend/app/skladbot_worker.py`, `tests/test_skladbot_daily_report.py`, `docs/*`.

**Что стало:**

- Для строки `Приемка` ежедневный отчет больше не берет плановое `products.amount`.
- Если SkladBot отдал `acceptedAmount`, отчет использует фактически принятое количество как есть: `1250 -> 1250`, `1750 -> 1750`.
- SKU-остатки на конец дня берутся из `/products`, где SkladBot отдает текущий `amount` по каждому товару.
- `/report/stock` остается контрольным общим итогом, но не используется как источник SKU-детализации, потому что он возвращает только общий остаток.

**Проверки:**

- `./.venv/bin/python -m unittest tests.test_skladbot_daily_report` - 7 tests OK.
- `./.venv/bin/python -m unittest discover tests` - 402 tests OK.
- `./.venv/bin/python -m compileall -q backend/app src/taksklad tools main.py tests` - OK.
- `docker compose --env-file deploy/vds/.env.example -f deploy/vds/docker-compose.yml config` - OK.
- `git diff --check` - OK.
- VDS restore point: `/opt/taksklad/restore_points/pre-daily-report-accepted-amount-20260609T173947Z`.
- VDS Postgres backup: `/opt/taksklad/backups/postgres/taksklad-postgres-20260609T173947Z.sql.gz`.
- VDS пересобраны и перезапущены `backend-api`, `telegram-worker`, `skladbot-worker`.
- VDS live-smoke по `WH-R-194859`: Red `acceptedAmount=1250 -> 1250` блоков, Brown `acceptedAmount=1750 -> 1750` блоков.
- Ручная переотправка отчета за `09.06.2026` выполнена в настроенный Telegram-чат.
- VDS `./deploy/vds/acceptance_status.sh` - общий `status=ok`.
- Свежие логи `backend-api`, `telegram-worker`, `skladbot-worker` - без `ERROR/Traceback/Exception`.

### SKU-колонки в ежедневном SkladBot отчете

**Файлы:** `backend/app/skladbot_daily_report.py`, `tests/test_skladbot_daily_report.py`, `docs/*`.

**Что стало:**

- В листе `Сводка` ежедневного отчета больше нет заглушек `SKU1/SKU2/SKU3`.
- Колонки строятся по реальным товарам из SkladBot: остатки и заявки склеиваются по названию, артикулу или штрихкоду.
- По каждой SKU видно остаток на начало дня, приемку, отгрузку, возврат и остаток на конец дня.
- Лист `Остатки` снова показывает построчные остатки SkladBot по товарам, чтобы можно было сверить сводку.

**Проверки:**

- `./.venv/bin/python -m unittest tests.test_skladbot_daily_report` - 6 tests OK.
- `./.venv/bin/python -m unittest discover tests` - 401 tests OK.
- `./.venv/bin/python -m compileall -q backend/app src/taksklad tools main.py tests` - OK.
- `docker compose --env-file deploy/vds/.env.example -f deploy/vds/docker-compose.yml config` - OK.
- `git diff --check` - OK.
- VDS restore point: `/opt/taksklad/restore_points/pre-daily-report-sku-summary-20260609T171702Z`.
- VDS Postgres backup: `/opt/taksklad/backups/postgres/taksklad-postgres-20260609T171702Z.sql.gz`.
- VDS пересобраны и перезапущены `backend-api`, `telegram-worker`.
- VDS synthetic XLSX-smoke внутри `telegram-worker`: заголовки `Chapman Brown OP 20`, `Chapman Gold SSL`, `Chapman RED OP 20`, колонка `Заявок`.
- VDS `./deploy/vds/acceptance_status.sh` - общий `status=ok`.
- `https://api.taksklad.uz/health` - 200 OK, backend `2.0.12`.

### Оперативный ack для полной позиции при застрявшей scan-очереди

**Файлы:** `backend/app/orders_service.py`, `src/taksklad/backend_events.py`, `tests/*`.

**Что стало:**

- Если позиция уже полностью отпикана в backend, повторная отправка лишнего scan-события по этой позиции возвращает успешный ответ и не добавляет новый КИЗ.
- Конфликт КИЗа с другой позицией не скрывается и остается `409`.
- Desktop-очередь также считает `Order item is already fully scanned` принятым событием, чтобы не блокировать переход склада дальше.

**Проверки:**

- Точечные backend/desktop tests - 5 tests OK.
- `./.venv/bin/python -m compileall -q backend/app src/taksklad tests/test_backend_api_persistence.py tests/test_backend_bridge.py` - OK.
- VDS smoke по полной позиции WH-R-194868: API вернул `201`, счетчик КИЗов остался `150 -> 150`.

### Самовывоз и фильтр логистического отчета

**Файлы:** `backend/app/excel_importer.py`, `backend/app/imports_service.py`, `backend/app/logistics_service.py`, `backend/app/google_sheets_exporter.py`, `src/taksklad/excel_import.py`, `tests/*`, `docs/*`.

**Что стало:**

- Если в Excel/API адрес пустой, технический или явно указан как `Самовывоз`, в заказ записывается `Самовывоз со склада`.
- Для Telegram Excel import и desktop import строки с координатами остаются доставочными: если адреса нет, адрес подтягивается по координатам, а при ошибке reverse geocode остается `Координаты: ...`.
- Google Sheets остается зеркалом: fallback-адрес в mirror/export теперь тоже `Самовывоз со склада`, а не `Адрес не указан`.
- Логистический отчет и список дат логистики берут только доставочные заказы с валидными координатами.
- Заказы `Самовывоз со склада`, без координат или с невалидными координатами не попадают в логистический XLSX и не ломают выгрузку всей даты.

**Проверки:**

- `./.venv/bin/python -m unittest tests.test_backend_telegram_import tests.test_backend_api_persistence tests.test_excel_normalizer` - 108 tests OK.
- `./.venv/bin/python -m unittest tests.test_backend_google_sheets_exporter tests.test_backend_google_sheets_pending tests.test_google_sheets_sync_worker` - 33 tests OK.
- `./.venv/bin/python -m unittest discover tests` - 383 tests OK.
- `./.venv/bin/python -m compileall -q backend/app src/taksklad tools main.py tests` - OK.
- `npm run build` в `frontend` - OK.
- `docker compose --env-file deploy/vds/.env.example -f deploy/vds/docker-compose.yml config` - OK.
- `git diff --check` - OK.
- VDS restore point: `/opt/taksklad/restore_points/pre-pickup-logistics-20260609T090735Z`.
- VDS Postgres backup: `/opt/taksklad/backups/postgres/taksklad-postgres-20260609T090735Z.sql.gz`.
- VDS пересобраны и перезапущены `backend-api`, `telegram-worker`, `google-sheets-sync-worker`.
- `https://api.taksklad.uz/health` - 200 OK, backend `2.0.9`.
- VDS `./deploy/vds/acceptance_status.sh` - общий `status=ok`.
- VDS in-container smoke логистики: из 4 временных строк в XLSX попала только доставочная строка с валидными координатами; самовывоз, пустые координаты и `999,999` исключены.
- VDS fresh logs `backend-api`, `telegram-worker`, `google-sheets-sync-worker` - без `ERROR/Traceback/Exception`.

### Новый шаблон ежедневного SkladBot отчета

**Файлы:** `backend/app/skladbot_daily_report.py`, `deploy/vds/.env.example`, `tests/test_skladbot_daily_report.py`, `docs/*`.

**Что стало:**

- Лист `Сводка` теперь повторяет рабочий шаблон Антона: дата отчета, время формирования, `customer_id` и таблица `Отчет о движении остатков за день`.
- В таблице сводки показываются `Приемка`, `Отгрузка`, `Возврат`, количество заявок и остаток на конец дня.
- Заглушки `SKU1/SKU2/SKU3` заменены на реальные названия товаров из SkladBot. По каждой SKU видно остаток на начало дня, приемку, отгрузку, возврат и остаток на конец дня.
- `Отгрузка` выводится отрицательным числом, чтобы движение остатков читалось как расход.
- Лист `Остатки` показывает построчные остатки SkladBot по товарам, а не одну агрегированную строку по клиенту.
- Google Sheets в ежедневном отчете не используется. Источник данных остается SkladBot API.
- Daily report теперь выдерживает временный SkladBot `429 Too Many Requests`: detail-запрос повторяется после cooldown, а общий delay применяется и между списками заявок.
- Добавлены env-настройки `SKLADBOT_DAILY_REPORT_429_RETRIES` и `SKLADBOT_DAILY_REPORT_429_RETRY_SECONDS`.

**Проверки:**

- `./.venv/bin/python -m unittest tests.test_skladbot_daily_report` - 6 tests OK.
- `./.venv/bin/python -m unittest tests.test_skladbot_daily_report tests.test_backend_telegram_import` - 49 tests OK.
- `./.venv/bin/python -m unittest discover tests` - 379 tests OK.
- `./.venv/bin/python -m compileall -q backend/app src/taksklad tools main.py tests` - OK.
- `docker compose --env-file deploy/vds/.env.example -f deploy/vds/docker-compose.yml config` - OK.
- `git diff --check` - OK.
- VDS live read-only dry-run отчета за `09.06.2026`: `requests_total=27`, `Отгрузка=26/1069`, `Приемка=1/2`, `Возврат=0/0`, `stock_total=931`, `errors_count=0`.
- VDS `./deploy/vds/acceptance_status.sh` - общий `status=ok`.

### Исправление 500 при сохранении КИЗов в backend

**Файлы:** `backend/app/orders_service.py`, `tests/test_backend_api_persistence.py`, `docs/*`.

**Что стало:**

- При создании скана backend явно flush-ит новую строку `scan_codes` до записи движения в `kiz_movements`.
- Это убирает PostgreSQL FK-ошибку `kiz_movements_scan_code_id_fkey`, из-за которой Windows-приложение видело `Backend не принял все КИЗы позиции`.
- Добавлен регрессионный тест, который проверяет порядок записи: `scan_codes` уже должен существовать в БД перед созданием `kiz_movements`.

**Проверки:**

- `./.venv/bin/python -m unittest tests.test_backend_api_persistence.BackendApiPersistenceTests.test_scan_flushes_scan_code_before_kiz_movement tests.test_backend_api_persistence.BackendApiPersistenceTests.test_return_releases_kiz_for_new_outbound_scan_with_history tests.test_backend_api_persistence.BackendApiPersistenceTests.test_failed_return_does_not_release_kiz_for_new_order` - OK.

## 2026-06-08

### Ежедневный SkladBot отчет в Telegram

**Файлы:** `backend/app/skladbot_daily_report.py`, `backend/app/telegram_worker.py`, `deploy/vds/.env.example`, `tests/test_skladbot_daily_report.py`, `docs/*`.

**Что стало:**

- Добавлен генератор ежедневного отчета по SkladBot напрямую из SkladBot API.
- Источник данных: SkladBot, не Google Sheets.
- Отчет включает:
  - заявки за день по категориям `Отгрузка`, `Возврат`, `Приемка`, `Прочее`;
  - юрлицо/точку, дату выгрузки, адрес, комментарий и товары по каждой заявке;
  - складские движения за день через `/warehouse/transactions`;
  - актуальный остаток клиента через `/report/stock`;
  - отдельный лист с ошибками сбора, если какой-то endpoint временно недоступен.
- Telegram worker получил ручную admin-команду `/skladbot_daily ДД.ММ.ГГГГ` для проверки отчета.
- Добавлено расписание отправки: `SKLADBOT_DAILY_REPORT_ENABLED=true`, время по умолчанию `22:00`.
- Защита от дублей хранится в `pending_events` по ключу `date+chat_id`, поэтому один чат не получит один и тот же daily report несколько раз за день.
- Между чтением деталей заявок есть настраиваемая пауза `SKLADBOT_DAILY_REPORT_REQUEST_DELAY_SECONDS=3.0`, чтобы не провоцировать rate limit SkladBot.
- Без `SKLADBOT_DAILY_REPORT_CHAT_IDS` автоматическая отправка не включается.

**Проверки:**

- `./.venv/bin/python -m unittest tests.test_skladbot_daily_report tests.test_backend_telegram_import` - 46 tests OK.
- `./.venv/bin/python -m unittest discover tests` - 375 tests OK.
- `./.venv/bin/python -m compileall -q backend/app src/taksklad tools main.py tests` - OK.
- `npm run build` в `frontend` - OK.
- `docker compose --env-file deploy/vds/.env.example -f deploy/vds/docker-compose.yml config` - OK.
- VDS live read-only dry-run отчета за `08.06.2026`: `requests_total=71`, `Отгрузка=67`, `Возврат=3`, `Приемка=1`, `stock_total=1578`, `errors_count=0`.
- VDS `./deploy/vds/acceptance_status.sh` - общий `status=ok`.
- После обнаружения Telegram `409 Conflict` worker больше не падает всем циклом: scheduled-задачи продолжают выполняться, а конфликт логируется как warning.
- Финальный VDS acceptance после ожидания цикла SkladBot worker: общий `status=ok`, `skladbot_coverage.status=ok`, `telegram_menu.status=ok`.

### Ручной выбор даты для Telegram Excel import

**Файлы:** `backend/app/telegram_worker.py`, `backend/app/excel_importer.py`, `tests/test_backend_telegram_import.py`, `docs/*`.

**Что стало:**

- После загрузки Excel-файла через Telegram бот не создаёт заказы сразу.
- Файл получает статус `waiting_shipment_date` и ждёт ответ оператора.
- Оператор отправляет дату одним сообщением в формате `ДД.ММ.ГГГГ`.
- Только после этого import переводится в `pending` и отправляется в backend.
- Введённая дата считается осознанным выбором пользователя и переопределяет дату внутри Excel.
- Старая сохранённая дата чата больше не используется для автоматической загрузки Excel.

**Проверки:**

- `./.venv/bin/python -m unittest tests.test_backend_telegram_import` - 43 tests OK.
- `./.venv/bin/python -m unittest discover tests` - 372 tests OK.
- `./.venv/bin/python -m compileall -q backend/app src/taksklad tools main.py tests` - OK.
- `git diff --check` - OK.

### Защита Telegram Excel import от старой сохранённой даты

**Файлы:** `backend/app/excel_importer.py`, `tests/test_backend_telegram_import.py`, `docs/*`.

**Что стало:**

- Дата из Excel теперь главнее сохранённой Telegram-даты.
- Telegram-дата используется только как fallback, если Excel не содержит надёжную дату.
- Если Telegram передаёт одну дату, а Excel содержит другую, импорт переводится в `waiting_date_choice` до создания заказов и SkladBot-заявок.
- Бот показывает inline-кнопки `Использовать дату Excel: ...` и `Отменить импорт`.
- Нажатие `Использовать дату Excel` очищает старую Telegram-дату в событии и запускает импорт заново по дате файла.
- Нажатие `Отменить импорт` переводит событие в `cancelled`; заказы и WH-R не создаются.

**Проверки:**

- `./.venv/bin/python -m unittest tests.test_backend_telegram_import` - 40 tests OK.
- `./.venv/bin/python -m unittest discover tests` - 369 tests OK.
- `./.venv/bin/python -m compileall -q backend/app src/taksklad tools main.py tests` - OK.
- `git diff --check` - OK.

## 2026-05-30

### Перенесены Telegram-кнопки в нижнее меню и добавлена очередь Excel-файлов

**Файлы:** `backend/app/telegram_worker.py`, `tests/test_backend_telegram_import.py`, `docs/*`.

**Что стало:**

- Telegram worker отправляет reply keyboard, то есть кнопки отображаются в нижней панели Telegram вместо inline-кнопок под `/start`.
- В нижнем меню есть кнопки: `Дневной отчёт`, `Статус backend`, `История импортов`, `Помощь`.
- Дополнительно настраивается системная кнопка меню команд Telegram через `setMyCommands` и `setChatMenuButton`.
- Команды `/report`, `/health`, `/imports`, `/help` сохранены как fallback.
- Excel-файлы `.xlsx/.xlsm`, отправленные или пересланные в Telegram-чат, ставятся в очередь `pending_events`.
- Если отправить 5 Excel-файлов подряд, worker поставит все 5 в очередь и обработает их последовательно.
- Очередь хранится в Postgres, поэтому файл не теряется при перезапуске worker после постановки в очередь.

**Проверки:**

- `.venv/bin/python -m unittest tests.test_backend_telegram_import` - 7 тестов пройдены.
- `.venv/bin/python -m unittest discover -s tests` - 66 тестов пройдены.
- `.venv/bin/python -m py_compile backend/app/*.py tests/*.py` - успешно.
- VDS `backend-api` и `telegram-worker` пересобраны и запущены.
- VDS `/health` на временном `sslip.io`-домене вернул `200`.
- Внутри VDS `telegram-worker` выполнен compile-check обновлённых файлов.
- VDS `getMyCommands` вернул команды `report`, `health`, `imports`, `help`.
- VDS `getChatMenuButton` вернул `type=commands`.

### Реализован Telegram Excel import через backend

**Файлы:** `backend/app/excel_importer.py`, `backend/app/telegram_worker.py`, `backend/requirements.txt`, `deploy/vds/docker-compose.yml`, `deploy/vds/.env.example`, `tests/test_backend_telegram_import.py`, `docs/*`.

**Что стало:**

- Telegram worker принимает Excel-документы `.xlsx/.xlsm` из разрешённых чатов.
- Файл скачивается во временный файл, разбирается через `openpyxl`, затем отправляется в `POST /api/v1/imports`.
- Parser поддерживает лист `Заявки`, алиасы колонок и fallback-даты.
- Если в Excel нет `Кол-во блок`, блоки считаются через `TAKSKLAD_DEFAULT_PIECES_PER_BLOCK`.
- Размер файла ограничивается через `TELEGRAM_WORKER_MAX_FILE_BYTES`.
- Ошибки скачивания Telegram не раскрывают полный URL с bot token.
- Ответы Telegram worker отправляются обычным текстом без `parse_mode=HTML`, чтобы имя Excel-файла или ошибка с символами `<`/`&` не ломали отправку.
- Excel workbook закрывается явно после чтения, чтобы Windows не держал файл залоченным.
- Добавлен чеклист Windows-приёмки backend bridge.

**Проверки:**

- `.venv/bin/python -m unittest tests.test_backend_telegram_import` - 2 теста пройдены.
- `.venv/bin/python -m unittest discover -s tests` - 61 тест пройден.
- `.venv/bin/python -m py_compile main.py sitecustomize.py taksklad/__init__.py src/taksklad/*.py tests/*.py backend/app/*.py` - успешно.
- VDS `backend-api` и `telegram-worker` пересобраны и запущены.
- VDS parser smoke внутри `telegram-worker` прошёл на тестовом `.xlsx`.

### Добавлен импорт заказов в Postgres, история импортов и backup-скрипты

**Файлы:** `backend/app/imports_service.py`, `backend/app/main.py`, `backend/app/schemas.py`, `tests/test_backend_api_persistence.py`, `deploy/vds/backup_postgres.sh`, `deploy/vds/restore_postgres.sh`, `docs/vds-release-readiness.md`, `docs/*`.

**Что стало:**

- `POST /api/v1/imports` создает `orders` и `order_items` из текущего desktop/Excel/Google-формата.
- `GET /api/v1/imports` возвращает историю импортов.
- Импорт группирует несколько товаров в один заказ и пропускает дубли позиций.
- Ошибочные строки возвращаются в `errors`, не ломая весь импорт.
- Добавлены ручные backup/restore-скрипты Postgres для VDS.
- Добавлен документ готовности VDS-линии к релизной приемке.

**Проверки:**

- `.venv/bin/python -m unittest tests/test_backend_api_persistence.py` - 5 тестов пройдены.
- `.venv/bin/python -m unittest discover -s tests` - 53 теста пройдены.
- `.venv/bin/python -m py_compile main.py sitecustomize.py taksklad/__init__.py src/taksklad/*.py tests/*.py backend/app/*.py` - успешно.
- `bash -n deploy/vds/backup_postgres.sh` - успешно.
- `bash -n deploy/vds/restore_postgres.sh` - успешно.
- Локальный Docker/Postgres smoke с импортом, дублем, сканами и завершением заказа - успешно.

### Реализованы backend endpoint'ы активных заказов, сканов и завершения заказа

**Файлы:** `backend/app/main.py`, `backend/app/orders_service.py`, `backend/app/models.py`, `backend/app/schemas.py`, `backend/requirements.txt`, `tests/test_backend_api_persistence.py`, `docs/*`.

**Что стало:**

- `GET /api/v1/orders/active` теперь возвращает реальные невыполненные заказы из БД с позициями.
- `POST /api/v1/scans` теперь пишет КИЗ в `scan_codes`, обновляет `scanned_blocks`, закрывает позицию при достижении плана и защищает от дублей.
- `POST /api/v1/orders/{order_id}/complete` теперь проверяет недосканированные обязательные позиции, закрывает заказ и пишет аудит.
- SQLAlchemy-модели можно поднимать в SQLite для быстрых тестов, при этом Postgres остаётся основной БД.
- Добавлена зависимость `httpx` для `FastAPI TestClient`.

**Проверки:**

- `.venv/bin/python -m unittest tests/test_backend_api_persistence.py` - 3 теста пройдены.
- `.venv/bin/python -m unittest discover -s tests` - 51 тест пройден.
- `.venv/bin/python -m py_compile main.py sitecustomize.py taksklad/__init__.py src/taksklad/*.py tests/*.py backend/app/*.py` - успешно.
- Локальный Docker/Postgres smoke прошёл полный сценарий: активный заказ, ранний отказ закрытия, два скана, дубль КИЗ, успешное закрытие заказа.
- Staging VDS обновлен и проверен через внешний HTTPS API тем же сценарием; временные smoke-данные удалены.

### Добавлен воспроизводимый Traefik-шаблон и зафиксирован VDS smoke-deploy

**Файлы:** `deploy/traefik/*`, `docs/implementation-log.md`.

**Что стало:**

- Добавлен `deploy/traefik/docker-compose.yml` для серверного Traefik с HTTPS, Docker provider и Let's Encrypt.
- Добавлен `deploy/traefik/.env.example` без секретов.
- Зафиксирован фактический VDS smoke-deploy: Docker/Compose, UFW, Traefik, `postgres`, `backend-api`, временный `sslip.io`-домен.
- Отдельно записано решение по Traefik: образ `v3.3` не работал с новым Docker API, сервер переведен на `traefik:v3.6`.

**Проверки:**

- На VDS `postgres` поднят и healthy.
- На VDS `backend-api` поднят.
- Внешний `GET /health` через HTTPS вернул `200`.
- Без Bearer-токена защищенный endpoint вернул `401`.
- С Bearer-токеном защищенный endpoint дошел до приложения и вернул ожидаемый MVP-ответ `501`.

### Зафиксирована локальная среда разработки ноутбука

**Файлы:** `docs/local-development-setup.md`, `docs/implementation-log.md`.

**Что стало:**

- Описана локальная настройка ноутбука для TakSklad: `.venv`, Python-зависимости, Docker CLI, Compose, Buildx, Colima, GitHub CLI.
- Зафиксированы команды для проверки тестов, backend compose config, локального запуска `postgres + backend-api` и остановки тестового стека.
- Уточнено, что рабочий `deploy/vds/.env` создаётся из `.env.example`, хранится локально и не попадает в Git.

**Проверки:**

- `.venv/bin/python -m unittest discover -s tests` - 47 тестов пройдены.
- `.venv/bin/python -m py_compile main.py sitecustomize.py taksklad/__init__.py src/taksklad/*.py tests/*.py backend/app/*.py` - успешно.
- Docker smoke `hello-world` - успешно.
- Локальный VDS compose smoke: `postgres + backend-api` собраны и подняты, `/health` отвечает, стартовые таблицы Postgres созданы.

### Добавлен VDS/backend MVP-каркас без Windows-релиза

**Файлы:** `.gitignore`, `backend/*`, `deploy/vds/*`, `tests/test_backend_skeleton.py`, `docs/*`.

**Что стало:**

- Добавлен FastAPI backend shell для будущего серверного TakSklad.
- Реализован `GET /health`.
- Зафиксированы контрактные endpoint'ы для активных заказов, сканов, завершения заказа, импортов и дневного отчёта. Реальной бизнес-логики в них пока нет, они возвращают `501 Not Implemented`.
- Добавлены настройки backend через env и опциональная проверка сервисного Bearer-токена.
- Добавлены SQLAlchemy-модели и стартовая PostgreSQL-схема для заказов, позиций, КИЗов, импортов, очередей, пользователей и аудита.
- Добавлен Dockerfile и VDS Docker Compose под `postgres`, `backend-api`, `adminer` и Traefik routing.
- Добавлен `.env.example` без реальных секретов.
- `.gitignore` теперь игнорирует реальные `.env`-файлы.
- Добавлены тесты backend-скелета, которые проверяют структуру, настройки, SQL-схему и compose без Docker.

**Что специально не менялось:**

- `version.json` не обновлялся и остаётся на `1.1.7`.
- Windows-архив, GitHub Release, tag и push-уведомления не создавались.
- Desktop пока не подключён к backend.

**Проверки:**

- `.venv/bin/python -m unittest tests/test_backend_skeleton.py` - 5 тестов пройдены.
- `.venv/bin/python -m unittest discover -s tests` - 47 тестов пройдены.
- `.venv/bin/python -m py_compile main.py sitecustomize.py taksklad/__init__.py src/taksklad/*.py tests/*.py backend/app/*.py` - успешно.
- `git diff --check -- . ':!archive/**'` - успешно.
- FastAPI smoke: `/health` вернул `200`, контрактный endpoint активных заказов вернул ожидаемый `501`.
- Docker Compose runtime не проверен локально: Docker CLI отсутствует в текущем окружении.

## 2026-05-29

### Начато разбиение `main.py`

**Файлы:** `src/taksklad/main.py`, `src/taksklad/http_client.py`, `src/taksklad/update_service.py`, `src/taksklad/printing.py`, `src/taksklad/pending_store.py`, `src/taksklad/reports.py`, `src/taksklad/ui_widgets.py`, `src/taksklad/telegram_service.py`, `src/taksklad/app_telegram.py`, `src/taksklad/app_updates.py`, `src/taksklad/app_imports.py`, `src/taksklad/app_catalog.py`, `src/taksklad/app_control_panel.py`, `src/taksklad/app_skladbot.py`, `src/taksklad/app_printing.py`, `src/taksklad/app_day_end.py`, `src/taksklad/duplicate_codes.py`, `tests/test_daily_report.py`, `docs/*`.

**Что стало:**

- HTTPS-запросы, автообновление, печать, локальные очереди/backup, отчеты и кнопка UI вынесены из `main.py` в отдельные модули.
- Telegram-сервис, Telegram UI/polling, UI-логика автообновления, ручной Excel-импорт, справочник товаров, контрольная панель, SkladBot-оркестрация, настройки/очередь печати, завершение дня и форматирование дублей КИЗ вынесены в отдельные модули.
- `main.py` уменьшен с 4190 до 1172 строк и остается главным образом Tkinter-оркестратором сканирования и основного UI.
- Тест дневного отчета обновлен под новый модуль `taksklad.reports`.

**Проверки:**

- `.venv/bin/python -m py_compile main.py sitecustomize.py taksklad/__init__.py src/taksklad/*.py tests/*.py` - успешно.
- `.venv/bin/python -m unittest discover -s tests` - 35 тестов пройдены.

### Локально структурирован репозиторий и закреплено название TakSklad

**Файлы:** `src/taksklad/*`, `main.py`, `taksklad/__init__.py`, `sitecustomize.py`, `.github/workflows/build-windows-release.yml`, `.gitignore`, `docs/*`, `README.md`, `tests/*`.

**Что стало:**

- Кодовые модули перенесены из корня в пакет `src/taksklad/`.
- Корневой `main.py` оставлен как тонкий запускатель для разработки и PyInstaller.
- Добавлен bridge-пакет `taksklad/`, чтобы тесты и локальные команды импортировали код из `src/`.
- Старые локальные артефакты перенесены в `archive/repo-cleanup-20260529/`: логи, backup JSON, старые credentials-снимки, `reports/`, `exports/`, `scan_backups/`, legacy runtime JSON и cache.
- В корне оставлены активные `credentials.json` и `TakSklad_data.json`, чтобы не сломать локальный запуск.
- В рабочих файлах удалены упоминания старого названия; официальное имя проекта и приложения — `TakSklad`.

**Проверки:**

- `.venv/bin/python -m unittest discover -s tests` - 35 тестов пройдены.
- `.venv/bin/python -m py_compile main.py sitecustomize.py taksklad/__init__.py src/taksklad/*.py tests/*.py` - успешно.

### `version.json` закреплен на рабочей стабильной версии 1.1.7

**Файл:** `version.json`.

**Что было:**

В манифесте стояло `latest_version = 1.1.17` и `min_supported_version = 1.1.17`. Для рабочих компьютеров на `1.1.7` это означало, что приложение видит себя ниже минимально поддерживаемой версии и может предлагать/требовать обновление.

**Что стало:**

- `latest_version = 1.1.7`
- `min_supported_version = 1.1.7`
- `mandatory = false`
- `download_url` / SHA очищены, чтобы стабильный манифест не ссылался на артефакты другой версии.

**Зачем:** рабочая линия склада остается на стабильной `1.1.7`, пока новая стабилизация не проверена вручную. Новый архив на этом этапе не собирается и не выкатывается.

### Обновление списка заказов больше не блокирует сканирование

**Файлы:** `main.py`, `docs/implementation-log.md`.

**Что было:**

Кнопка `Обновить` использовала общий флаг `operation_in_progress`. Пока Google Sheets загружал список заказов, приложение считало себя полностью занятым, поэтому сканер получал ошибку `Дождитесь завершения текущей операции`, даже если фактически шла только фоновая загрузка списка.

**Что стало:**

- Добавлено отдельное состояние `refresh_in_progress` для фонового обновления заказов.
- Ручное обновление при выбранной позиции больше не сбрасывает текущий заказ и не блокирует ввод КИЗов.
- Сохранение текущей позиции проверяется в момент завершения обновления: если пользователь выбрал заказ уже во время фоновой загрузки, выбор не сбрасывается.
- Повторное обновление во время уже идущего обновления показывает отдельное сообщение.
- Фоновый SkladBot не стартует параллельно с ручным обновлением, активным сканированием или блокирующей операцией.
- `fetch_sheet_data` и `fetch_sheet_data_with_sync` переиспользуют уже загруженные строки Google Sheets для списка существующих КИЗов, вместо второго чтения всего листа.
- Добавлен cooldown для фоновых Google Sheets обращений после `429`/timeout. В первую очередь это защищает Telegram lock/state от частых повторов, которые добивают квоту.
- Добавлен SkladBot `dry_run=True` для безопасной проверки сопоставления без записи в Google Sheets.
- Добавлен SkladBot API timeout `SKLADBOT_API_TIMEOUT_SECONDS = 8`, чтобы фоновый синк не зависал надолго на медленных деталях заявки.
- Обновлен устаревший тест SkladBot: сохраненный `requests_limit=100` больше не ожидается как рабочий лимит, потому что код держит минимум 500 заявок для 14-дневного окна.
- Заведен `docs/implementation-log.md` для фиксации сделанного, нерешенного, ошибок, решений и ручных проверок.

**Зачем:** можно обновлять список заказов и продолжать сканирование на выбранной позиции, не ловя ложное состояние "приложение занято".

**Проверка:** требуется ручная проверка в UI: выбрать заказ, начать сканирование, нажать `Обновить`, убедиться, что КИЗы продолжают приниматься, а текущая позиция не сбрасывается.

**Фактические проверки:**

- UI-smoke без реальных сетевых вызовов пройден: КИЗ принят во время фонового обновления, текущая позиция сохранена.
- SkladBot dry-run прошел без записи. В текущем `data` нет активных невыполненных заказов, поэтому сопоставлять нечего.
- Read-only SkladBot API с лимитом 10 заявок прошел, примеры заявок содержат дату выгрузки, получателя и товары.

### Добавлена единая база знаний и roadmap складской экосистемы

**Файлы:** `docs/project-knowledge-base.md`, `docs/warehouse-ecosystem-roadmap.md`.

**Что добавлено:**

- `project-knowledge-base.md` - единый документ по текущему состоянию TakSklad: архитектура, модули, Google Sheets, локальные файлы, SkladBot, Telegram, очереди, автообновление, логи, известные проблемы, запреты и ближайшая повестка.
- `warehouse-ecosystem-roadmap.md` - развёрнутый план перехода от desktop-приложения к складской экосистеме: VPS, Docker Compose, PostgreSQL, backend API, Telegram worker, SkladBot worker, report/backup worker, web panel, миграционные этапы и риски.

**Зачем:** собрать проектные знания в `docs/`, чтобы дальнейшая разработка не держалась в переписке. Эти документы фиксируют текущую систему и направление расширения в WMS-экосистему.

**Важно по безопасности:** документы не содержат Telegram token, Google private key, реальные chat_id, пароли VPS или другие секреты.

## 2026-05-28

### `version.json` переключён на `onefile_exe`, чтобы остановить активный цикл автообновления на ноуте

**Файл:** `version.json`.

**Что было:**

После фикса автообновления в коде (26.05) на ноуте пользователя всё ещё крутился старый билд без правок, поэтому продолжал срабатывать цикл: каждый старт → старый exe видит `package_type: onedir_zip` → решает «нужен переход на onedir» → запускает PowerShell-updater → `self.destroy()` → PowerShell делает `Start-Process -FilePath $NewExe` → новый старт → goto 1. Перевыкатывать билд можно только через сборку, а пока — пользователь не может закрыть приложение.

**Что стало:**

В манифесте `version.json`:

```diff
-  "mandatory": true,
-  "package_type": "onedir_zip",
+  "mandatory": false,
+  "package_type": "onefile_exe",
```

После push'a в `main`-ветку старый exe на ноуте при следующем запуске прочитает обновлённый манифест:

- `update_available` = `compare_versions("1.1.17", "1.1.17") < 0` → False
- `below_min_version` = `compare_versions("1.1.17", "1.1.17") < 0` → False
- `package_transition_required` = `manifest_targets_onedir(...)` → False (потому что `'onefile_exe'` нет в `('onedir','onedir_zip','zip')`)

Все три условия в `handle_update_info` дают False → ранний return → `start_auto_update` не вызывается → цикл прерывается.

**Зачем:** ноут починится без пересборки exe. Манифест — самый лёгкий способ дотянуться до уже задеплоенной версии без перекомпиляции.

**Когда менять обратно:** после следующего билда (1.1.18+) с моими правками `handle_update_info`, можно вернуть `package_type: onedir_zip` и `mandatory: true`. Новый exe уже умеет показывать промпт и не входить в цикл, поэтому старая модель «жёсткого обновления» снова безопасна.

**Шаги для пользователя:**

1. `git add version.json && git commit -m "..." && git push` из этой машины.
2. На ноуте через Диспетчер задач убить все процессы `TakSklad.exe`, `cmd.exe` и `powershell.exe`, связанные с обновлением (правый клик → «Завершить дерево процессов»).
3. Удалить из `%TEMP%` остатки скриптов: `TakSklad_updater_*.bat`, `TakSklad_updater_*.ps1`, `TakSklad_rename_*.bat`.
4. Запустить `TakSklad.exe` — он не должен зайти в цикл обновления.

### `maybe_rename_windows_executable`: убрана вторая лестница цикла перезапуска

**Файлы:** `main.py` (функция `maybe_rename_windows_executable`).

**Что было:**

В правке 26.05 я закрыл цикл автообновления в `create_windows_exe_updater` и `create_windows_onedir_updater` — при ошибке установки они больше не запускают старый exe. Но **пропустил третье место с тем же антипаттерном** — `.bat`-скрипт внутри `maybe_rename_windows_executable`.

Эта функция срабатывает, когда пользователь скачал приложение под нестандартным именем (например `TakSklad-windows-x64.exe` вместо ожидаемого `TakSklad.exe`). Скрипт копирует OLD→NEW, запускает NEW, удаляет OLD. При ошибке копирования (NEW занят антивирусом, нет прав на запись и т.п.) бат делал `start "" "%OLD%"` — запускал OLD под старым именем. OLD при старте снова входил в `maybe_rename_windows_executable` (потому что basename(OLD) != APP_EXECUTABLE_NAME), снова создавал .bat, копия снова падала, снова `start OLD` → бесконечный цикл «приложение само открывается после закрытия».

В отличие от auto-update loop, этот срабатывал даже когда пользователь ничего не делал с обновлениями — только при «нестандартном» имени исполняемого файла.

**Что стало:**

Из `.bat`-скрипта убран `start "" "%OLD%"` в ветке ошибки `copy /Y`. Теперь скрипт пишет в `TakSklad_update.log` (в `docs/`):
```
[date time] Не удалось создать "%NEW%", перезапуск старого exe отключён во избежание цикла
```
и выходит с кодом 1. Приложение не открывается заново — пользователь видит, что оно не стартовало, смотрит лог, решает что делать (часто — переименовать exe вручную в `TakSklad.exe`).

**Зачем:** закрыть ту же яму, что и в updater'ах. Теперь во всех трёх местах, где `.bat`/`.ps1` могли запустить старый exe из ветки ошибки, такого больше нет:
1. `create_windows_exe_updater` — закрыто 26.05
2. `create_windows_onedir_updater` — закрыто 26.05
3. `maybe_rename_windows_executable` — закрыто сейчас

**Тесты:** покрыто compile-check'ом `main.py`. Поведение завязано на Windows `.bat`-runtime, full regression возможен только на Windows-машине.

### SkladBot: окно синхронизации расширено до 14 дней, фильтр в детали идёт по `unloading_date`

**Файлы:** `config.py`, `skladbot.py`.

**Что было:**

Лог `2026-05-28 14:02:57 [INFO] SkladBot: список=500, к проверке за сегодня/вчера=0` чётко показал: из 500 заявок SkladBot **ни одна** не проходит первичный фильтр окна. Причина в комбинации:

1. `SKLADBOT_SYNC_LOOKBACK_DAYS = 1` — окно «вчера и сегодня».
2. `list_item_in_sync_window` фильтрует по `created_at` — когда заявка была создана в SkladBot, не когда она отгружается.
3. У оператора склада типовой цикл: заявка создаётся в SkladBot за 2–4 дня до отгрузки. Сегодня 28.05, отгрузки 26.05 — заявки созданы 24–26.05. С окном в 1 день они уже за бортом.

После первичного фильтра то же самое делает `request_in_sync_window` уже на полной детали заявки — снова по `created_at` вместо `unloading_date`. То есть даже если первичный фильтр пропустил, повторная проверка отсеет.

Видимое следствие — «номера заявок не подтягиваются»: матчингу нечего проверять, все группы помечаются `Не найдено`.

**Что стало:**

- `SKLADBOT_SYNC_LOOKBACK_DAYS = 14` (было 1). Покрывает обычный логистический цикл, при котором заявка создаётся за 1–7 дней до отгрузки.
- `request_in_sync_window` теперь смотрит сначала на `unloading_date` (дата отгрузки из детали), и только если её нет — на `created_at`. Это концептуально правильно: нам важна дата отгрузки, не дата создания.
- Первичный фильтр `list_item_in_sync_window` остаётся на `created_at`, потому что в листинге заявок SkladBot нет `unloading_date` (он только в детали). Но окно стало достаточно широким, чтобы потенциально интересные заявки прошли — а точечный фильтр по детали отсеет лишнее.
- Расширен диагностический лог `fetch_candidate_requests`: теперь пишет фактический разброс `created_at` всех 500 заявок и размер окна — `SkladBot: список=500 (created_at 14.05.2026..28.05.2026), окно=14 дн., к проверке=N, активных=A, завершённых/архивных=C`. Сразу видно, не отсекает ли окно полезные строки.

**Зачем:** убрать ситуацию «0 заявок к проверке», когда в SkladBot реально есть нужные заявки, просто на 3+ дня старше окна.

**Регрессионный риск:** при увеличении окна с 1 до 14 дней первичный фильтр пропускает примерно в 14 раз больше заявок (грубая оценка). Каждая из них требует одного `GET /requests/show/{id}` для получения детали. При типовом сценарии (200 пропущенных вместо 15) и `request_delay_seconds = 0.05` это +10 секунд к синку. Допустимо для периодической задачи с интервалом 10 минут. Если станет узким местом — можно вернуть `SKLADBOT_SYNC_LOOKBACK_DAYS` ближе к 7.

**Тесты:** покрыто прогоном существующих регрессионных тестов в `tests/test_skladbot_sync.py` — все 9 моих проходят, включая `test_fetches_details_only_for_today_and_yesterday_requests` (он передаёт `today` и `lookback_days` явно в `fetch_candidate_requests`, поэтому смена дефолта на него не влияет).

### SkladBot матчинг клиента стал устойчив к кавычкам и пунктуации + диагностический лог результата

**Файлы:** `skladbot.py`, `skladbot_sync.py`, `tests/test_skladbot_sync.py`.

**Что было:**

После правки 26.05 (строгое сравнение клиента через `normalize_lookup_text`) появилась обратная проблема: матчинг стал слишком жёстким. В Excel клиент часто записан с кавычками — `"MARKET AL-KABIR" MChJ` — а SkladBot отдаёт recipient без кавычек или с типографскими `«»`. `normalize_lookup_text` снимал регистр и лишние пробелы, но кавычки, дефисы и точки сохранял. Любое такое расхождение валило строгое равенство, и заявка помечалась `Не найдено`.

В лог при этом писалось только «список=500, к проверке за сегодня/вчера=58» — сколько именно групп нашлось / не нашлось не было видно. Диагностика вслепую.

**Что стало:**

- В `skladbot.py` добавлена функция `normalize_company_name(value)`. Делает то же что `normalize_lookup_text`, плюс удаляет ВСЕ небуквенно-цифровые символы (кавычки `"'«»“”„`, дефисы, точки, запятые, скобки) и схлопывает пробелы. Слова и цифры сохраняются и должны совпадать один-в-один.
- `request_matches_order_group` теперь сравнивает клиента через `normalize_company_name` вместо `normalize_lookup_text`. Семантика прежняя — нужен тот же контрагент. Терпимость только к пунктуации.
- В `sync_skladbot_request_numbers` после прохода всех групп пишется итоговый INFO в лог:
  ```
  SkladBot sync: групп=X, заявок-кандидатов=Y, matched=A, not_found=B, multiple=C, ячеек обновлено=D
  ```
  Если есть `not_found` — отдельной строкой выводятся до 5 примеров с датой, клиентом и числом товаров. Это позволяет сразу видеть в логе, кто не матчится, и проверить написание в SkladBot.

**Примеры что теперь матчится:**

| Excel (Клиент) | SkladBot (recipient) | Раньше | Сейчас |
|---|---|---|---|
| `"MARKET AL-KABIR" MChJ` | `MARKET AL-KABIR MChJ` | Не найдено | Найдено |
| `"MARKET AL-KABIR" MChJ` | `«MARKET AL-KABIR» MChJ` | Не найдено | Найдено |
| `ООО "Аэропорт"` | `ООО Аэропорт` | Не найдено | Найдено |
| `"MARKET AL-KABIR" MChJ` | `"MARKET AL-KEBIR" MChJ` | Не найдено | Не найдено (правильно — разные слова) |

**Зачем:** закрыть регрессию от прошлой правки, но не возвращаться к нечёткому токен-матчингу, из-за которого номера заявок сползали к соседним клиентам.

**Тесты (`tests/test_skladbot_sync.py`):**

- `test_matches_request_when_client_quotes_differ` — три кейса нормализации (`normalize_company_name` отдаёт одинаковый результат для разных кавычек, разные слова дают разные результаты) + полный e2e: заявка SkladBot без кавычек матчится с группой Excel с кавычками.
- Регрессионные `test_does_not_match_request_from_different_client`, `test_does_not_match_request_with_different_unloading_date`, `test_does_not_match_request_when_unloading_date_is_missing` продолжают проходить — защита от сползания номеров и нестрогих дат не нарушена.

Все 9 моих тестов в файле зелёные. Падает только предсуществующий `test_load_settings_respects_saved_skladbot_limits` — не связан с этой правкой (там `SKLADBOT_REQUESTS_LIMIT` в config.py подняли с 100 до 500, тест не обновили).

## 2026-05-26

### Логи приложения переехали в `docs/`

**Файлы:** `config.py`, `main.py`, `.gitignore`.

**Что было:**

`TakSklad.log` и `TakSklad_update.log` писались в корень папки приложения рядом с `main.py`, `credentials.json`, `*.json`-очередями и историческим `TakSklad.log`. Корневая папка постепенно превращалась в свалку: код, секреты, бэкапы Google Sheets, очереди, логи — всё в одном месте. Найти нужное по списку файлов становилось всё дольше.

**Что стало:**

- В `config.py` добавлены `LOG_DIR = os.path.join(APP_DIR, "docs")` и `UPDATE_LOG_FILE = os.path.join(LOG_DIR, "TakSklad_update.log")`. `LOG_FILE` перенесён в `docs/TakSklad.log`.
- В `main.py` перед `logging.basicConfig` теперь `os.makedirs(LOG_DIR, exist_ok=True)` — на первом запуске после клона/установки папка создаётся автоматически, без `FileNotFoundError`.
- Три места, где `TakSklad_update.log` собирался вручную через `os.path.join(APP_DIR, f"{APP_NAME}_update.log")` (`create_windows_exe_updater`, `create_windows_onedir_updater`, `maybe_rename_windows_executable`), теперь используют единый `UPDATE_LOG_FILE`.
- В `.gitignore` добавлена строка `docs/*.log` — `.md`-файлы в `docs/` остаются в git, а логи игнорируются.

**Зачем:** держать всё связанное с историей проекта и его диагностикой в одном месте (`docs/`). Поиск по корню становится короче, а changelog, документация и логи лежат рядом, что удобно при отладке.

**Что НЕ меняется:**

- Старые `TakSklad.log` и `TakSklad.log` в корне репозитория остаются как есть (исторические артефакты, уже в `.gitignore`). При следующем запуске приложение начнёт писать в `docs/TakSklad.log`. Корневой `TakSklad.log` можно удалить вручную, когда захочешь.
- `*.log` exclusion в robocopy внутри PowerShell-апдейтера сработает по filename-паттерну независимо от подкаталога, поэтому при обновлении `docs/TakSklad.log` и `docs/TakSklad_update.log` не затрутся.
- Расположение `credentials.json`, `TakSklad_data.json`, `pending_*.json` и прочих рабочих файлов не меняется — это отдельный вопрос (см. ранее раздел про беспорядок в корне).

**Тесты:** покрытие compile-check'ом `config.py` и `main.py`; полный прогон unit-тестов — 25/26 проходят (единственный fail `test_load_settings_respects_saved_skladbot_limits` пре-существующий, не из этой правки).

### Автообновление больше не зацикливается и спрашивает разрешение

**Файлы:** `main.py` (функции `handle_update_info`, `create_windows_exe_updater`, `create_windows_onedir_updater`), `config.py` (константа `UPDATE_RETRY_COOLDOWN_SECONDS`).

**Что было:**

На onefile-сборке `package_transition_required` возвращал True на каждом запуске (манифест `"package_type": "onedir_zip"`, а у клиента ещё onefile). Автообновление **запускалось без подтверждения пользователя**, скачивало ZIP, дёргало PowerShell-installer и закрывало приложение через `self.destroy()`. PowerShell в финале всегда делал `Start-Process -FilePath $NewExe`, запуская приложение заново. Если установка падала (robocopy не смог перезаписать файлы — антивирус, права доступа, занятый файл), в `catch`-блоке PowerShell-скрипт всё равно делал `Start-Process -FilePath $current_exe` — то есть запускал **старую** версию. Эта старая версия снова видела «нужно обновиться», снова запускала тот же updater, который снова падал, который снова запускал старый exe.

Пользователь видел это как «приложение постоянно открывается после закрытия» — приложение действительно закрывалось само (это `self.destroy()` после updater), но через пару секунд PowerShell-скрипт запускал его снова. Каждый запуск выглядел как «оно вернулось».

Аналогичная проблема была и в `create_windows_exe_updater` (онефайл-апдейтер): после 60 неудачных попыток `copy /Y` он делал `start "" "%APP%"` — запускал старый exe.

**Что стало:**

- В `handle_update_info` добавлен **диалог подтверждения** перед стартом обновления. Кнопка «Нет» — обновление откладывается. В сообщении показывается версия, причина (новая версия / минимально поддерживаемая / переход onefile→onedir) и текст из `update_info.message`.
- Добавлен **cooldown 1 час** на повторную попытку обновления той же версии (константа `UPDATE_RETRY_COOLDOWN_SECONDS` в `config.py`, состояние хранится в секции `update_skip_state` файла `TakSklad_data.json`). Если пользователь отказался или установка падала, следующая проверка той же версии не сработает раньше, чем через час. Это страховка на случай, если кто-то в `version.json` укажет более новую версию чем фактически выкатил.
- В `create_windows_onedir_updater` из `catch`-блока убран `Start-Process` старого exe. Теперь при падении updater пишет в `TakSklad_update.log` и выходит с кодом 1 — приложение **не запускается заново**. Пользователь видит, что оно не открылось, идёт в лог, понимает причину.
- В `create_windows_exe_updater` после 60 неудачных попыток `copy /Y` убран `start "" "%APP%"`. Поведение симметрично onedir-апдейтеру: пишем в лог, выходим, ничего не запускаем.

**Зачем:** прервать бесконечный цикл «закрытие → updater → старый exe → закрытие». Тихое автообновление без подтверждения — само по себе антипаттерн, а в комбинации с always-restart-on-failure оно превращалось в плохо отлаживаемый цикл.

**Как поведёт себя приложение после правки:**

- Если установка успешна (типовой случай) — поведение не изменилось: новый exe запускается через `Start-Process`.
- Если установка падает — приложение не открывается заново; в `TakSklad_update.log` появляется строка с причиной.
- Если пользователь нажал «Нет» в диалоге — приложение продолжает работать на текущей версии; следующий промпт появится не раньше чем через час и только при следующем запуске.

**Тесты:** покрытие через unit-тесты ограничено (логика завязана на `messagebox.askyesno`, PowerShell-скрипты и Windows-специфичные subprocess). Сейчас проверено compile-check'ом `main.py` и `config.py`. Полная регрессия — на реальной Windows-машине после следующей сборки.

**Откат, если что-то пошло не так:** временно вернуть автообновление без промпта можно, удалив блок `messagebox.askyesno(...)` в `handle_update_info` и условие `if not user_confirmed: return`. Cooldown можно отключить, поставив `UPDATE_RETRY_COOLDOWN_SECONDS = 0`.

### Telegram-бот: общий `last_update_id` в Google Sheets, чтобы два компа не обрабатывали один и тот же файл

**Файлы:** `sheets.py`, `main.py`, `tests/test_telegram_lock.py`.

**Что было:**

Single-listener lock через лист `_TakSklad_System` уменьшил, но не убрал двойную обработку. `process_telegram_updates` читал `telegram_state.last_update_id` **из локального** `TakSklad_data.json` через `load_telegram_state()`. Каждый компьютер вёл свой счётчик прочитанных Telegram-апдейтов. Плюс в `ensure_telegram_poll_lock` локальный кэш `telegram_lock_owned_until` мог 60 секунд считать, что lock у него, даже если сосед уже его перехватил.

В результате при гонке lock'а оба компьютера получали тот же `update_id`, оба обрабатывали один и тот же Excel-файл, оба отвечали в чат: один — «Файл не импортирован: занят другой операцией», второй — «Excel импортирован... Позиций загружено: 23». Данные уходили в чужую таблицу того компьютера, у которого был неактуальный `SPREADSHEET_ID` или старый `credentials.json`.

**Что стало:**

- В лист `_TakSklad_System`, строка 3 (после header и lock-строки), добавлена общая строка `telegram_state` со схемой `key/owner_id/owner_label/updated_at/updated_ts`. В `owner_id` пишется `last_update_id` строкой, в `owner_label` — компьютер, который последним подтвердил апдейт.
- Новые функции в `sheets.py`: `read_shared_telegram_state()` и `write_shared_telegram_state(last_update_id, owner_label, now_ts=None)`. Запись отказывается перезаписывать большее значение меньшим — это защищает от того, что параллельный писатель откатит чужой прогресс.
- `process_telegram_updates` теперь сначала читает общий `last_update_id` из Google Sheets, берёт максимум между общим и локальным, и передаёт его в `getUpdates(offset=last+1)`. Если общий state временно недоступен (Google Sheets лежит), откатывается на локальный кэш и продолжает работу.
- Внутри цикла обработки апдейтов добавлена явная проверка `update_id <= last_update_id → skip`. Это страхует от случая, когда Telegram всё-таки вернул уже обработанный апдейт.
- После обработки локальный state пишется как раньше, а общий — только если он строго больше текущего значения в Google.

**Зачем:** даже при кратком сбое lock'а второй компьютер больше не сможет повторно прогнать тот же `update_id` и записать дубль в Google Sheets.

**Что НЕ закрывается этим фиксом:** если оба компьютера запущены с разными `SPREADSHEET_ID` или разными `credentials.json` (например, на одном — старый ключ от katering, на другом — новый от taksklad), они пишут в РАЗНЫЕ таблицы и общий state у них тоже разный. В этом случае нужно сначала привести оба компьютера к одной конфигурации (см. ниже «Действия эксплуатанта»).

**Тесты (`tests/test_telegram_lock.py`):**

- `test_read_returns_zero_when_state_row_missing` — если строки state нет, читается 0.
- `test_write_creates_state_row_with_last_update_id` — первая запись создаёт строку.
- `test_write_refuses_to_go_backwards` — попытка записать меньшее значение игнорируется.
- `test_write_updates_when_new_value_is_greater` — большее значение перезаписывает старое.

**Действия эксплуатанта (не код, делается руками):**

1. На втором компьютере открыть `credentials.json` и проверить, что это актуальный service account TakSklad, а не старый ключ от другого проекта. Реальные `client_email`, `project_id` и `private_key_id` не фиксируются в документации и Git.
2. На втором компьютере открыть `config.py` строки 4-5 и проверить, что указан актуальный `SPREADSHEET_ID` рабочей таблицы TakSklad и `SHEET_NAME = "data"`. Реальный идентификатор таблицы сверяется по локальной рабочей конфигурации.
3. В Google Sheets открыть доступ к рабочей таблице для актуального service account TakSklad с ролью **Editor**.
4. После синхронизации перезапустить TakSklad на обоих компьютерах, чтобы оба прочитали общий `last_update_id` из листа `_TakSklad_System` с самого начала.

### Telegram-бот слушает только один компьютер через временный lock

**Файлы:** `config.py`, `main.py`, `sheets.py`, `tests/test_telegram_lock.py`.

**Что было:**

Если два компьютера запускали TakSklad с одним Telegram bot token, оба вызывали `getUpdates`, и Telegram возвращал `HTTP Error 409: Conflict`.

**Что стало:**

- добавлен временный lock в Google Sheets на листе `_TakSklad_System`;
- компьютер, который получил lock `telegram_poll`, опрашивает Telegram;
- второй компьютер пропускает Telegram polling и раз в 15 секунд пробует получить lock снова;
- lock обновляется раз в 20 секунд и считается устаревшим через 60 секунд;
- проверка lock выполняется в Telegram worker, не в UI-потоке, поэтому сканирование не ждёт Google Sheets;
- при закрытии приложения текущий владелец пытается освободить lock.

**Как быстро отключить:** в `telegram_settings` можно добавить `"single_listener_lock": false`; также есть общий флаг `TELEGRAM_SINGLE_LISTENER_LOCK_ENABLED` в `config.py`.

**Зачем:** убрать конфликт Telegram на двух компьютерах без большой архитектурной переделки. Это временный изолированный механизм, который потом можно быстро заменить или удалить.

**Тесты (`tests/test_telegram_lock.py`):**

- `test_acquire_creates_lock_sheet_and_writes_owner`;
- `test_active_other_owner_blocks_lock`;
- `test_stale_other_owner_can_be_replaced`;
- `test_release_clears_only_own_lock`.

### Release-архивы теперь включают локальный `version.json`

**Файл:** `.github/workflows/build-windows-release.yml`, шаг `Build onedir app`.

**Что было:**

В папочную Windows-сборку копировался `README.txt`, но `version.json` оставался только в репозитории и на GitHub raw URL.

**Что стало:**

Workflow создаёт локальный `version.json` рядом с `TakSklad.exe` в папке `TakSklad` перед упаковкой ZIP. Внутри фиксируются `app_version`, release tag, URL публичного update manifest и ссылка на release.

**Зачем:** готовые архивы становятся самодостаточнее: по папке приложения видно, какая версия установлена и откуда приложение проверяет обновления. Сам механизм автообновления по-прежнему читает публичный `version.json` с GitHub, поэтому будущие уведомления об обновлении приходят через интернет-адрес из `config.py`, а не через локальную копию файла.

**Почему не копируется публичный manifest один-в-один:** публичный `version.json` содержит SHA ZIP-архива. Если положить этот файл внутрь ZIP, SHA архива изменится, и поле `sha256_onedir` внутри станет устаревшим. Поэтому в архиве хранится локальный manifest без self-hash, а контрольные суммы остаются в публичном `version.json`.

**Тесты:** изменение упаковки; проверено обновлением текущего локального архива `TakSklad-ready-v1.1.16-with-data.zip` и наличием `TakSklad/version.json` внутри.

### Флаг “занято” больше не остаётся висеть после сбоя интерфейса

**Файл:** `main.py`, функции `set_busy`, `clear_busy`, `show_busy_error`, фоновые операции обновления/импорта/печати.

**Что было:**

Финализаторы некоторых операций сначала включали кнопки, а уже потом сбрасывали `operation_in_progress`. Если Tkinter уже пересоздал или закрыл кнопку, мог возникнуть UI-сбой до `clear_busy()`, и приложение продолжало отвечать `Дождитесь завершения текущей операции`, хотя рабочий поток уже завершился.

**Что стало:**

- начало и завершение операции пишутся в лог с длительностью;
- сообщение “занято” показывает, какая операция держит блокировку;
- критичные финализаторы сначала сбрасывают `operation_in_progress`, затем безопасно обновляют кнопки через `safe_config`;
- импорт из Telegram тоже заполняет данные текущей операции и очищает их через общий `clear_busy`.

**Зачем:** во время сканирования КИЗов оператор должен видеть реальную причину блокировки, а интерфейс не должен оставаться навсегда занятым после побочной ошибки UI.

**Тесты:** покрыто компиляцией `main.py`; поведение завязано на Tkinter callbacks.

### Сохранение общего JSON повторяется, если Windows кратко держит файл

**Файл:** `storage.py`, функция `save_app_data`.

**Что было:**

Сохранение всегда писало в один и тот же `TakSklad_data.json.tmp`, затем делало `os.replace`. Если второй процесс, антивирус или Windows на короткое время держали `.tmp` или основной JSON, появлялись ошибки `WinError 32` / `WinError 5`.

**Что стало:**

- временный файл теперь уникальный для каждой записи;
- при `PermissionError` замена повторяется до 8 раз с короткой паузой;
- недозаменённый временный файл удаляется в `finally`.

**Зачем:** два запущенных экземпляра или краткая блокировка файла больше не должны сразу ломать локальные очереди, настройки и общий файл данных.

**Тесты (`tests/test_storage_credentials.py`):**

- `test_save_app_data_retries_when_replace_is_temporarily_locked` — первый `os.replace` падает с `PermissionError`, второй успешно сохраняет данные.

### Ошибки Google Sheets стали понятнее для оператора

**Файл:** `sheets.py`, функция `format_google_sheets_error`.

**Что было:**

Ошибки Google могли уходить в интерфейс техническим текстом вроде `('invalid_grant: Invalid JWT Signature.', ...)`, а `PermissionError` после `403` иногда отображался пустой строкой.

**Что стало:**

- `403 / The caller does not have permission` показывается как проблема доступа service account к таблице;
- `invalid_grant / Invalid JWT Signature` показывается как старый или повреждённый Google-ключ;
- запись КИЗов в Google Sheets возвращает тот же понятный текст ошибки.

**Зачем:** оператору сразу видно, что надо заменить ключ или открыть таблицу сервисному аккаунту, а не ждать завершения несуществующей операции.

**Тесты (`tests/test_google_error_messages.py`):**

- `test_permission_error_gets_actionable_message`;
- `test_invalid_jwt_gets_actionable_message`.

### Обновление списка заказов больше не ждёт SkladBot

**Файл:** `main.py`, функции `fetch_sheet_data_with_sync`, `refresh_from_sheet`, `sync_skladbot_async`.

**Что было:**

Кнопка `ОБНОВИТЬ` и стартовая загрузка списка читали Google Sheets, затем сразу синхронно запускали SkladBot и только после этого отдавали управление интерфейсу. На втором компьютере это приводило к состоянию `Обновляю список заказов...`: список ещё пустой, кнопки заблокированы, хотя другой компьютер продолжает сканировать.

**Что стало:**

- быстрое обновление читает Google Sheets и очередь сохранений без ожидания SkladBot;
- список для КИЗов становится доступен сразу после чтения Google Sheets;
- SkladBot запускается отдельной фоновой задачей и не держит `operation_in_progress`;
- если фоновая SkladBot-синхронизация записала номера и оператор не находится внутри заказа, список перечитывается мягко.

**Зачем:** второй компьютер должен иметь возможность обновить список и начать работу, даже если SkladBot долго отвечает или временно недоступен.

**Тесты (`tests/test_refresh_fallback.py`):**

- `test_can_refresh_without_blocking_on_skladbot_sync` — быстрый refresh не вызывает SkladBot-синхронизацию и возвращает заказы из Google.

### Матчинг заявок SkladBot: дата выгрузки теперь обязательный критерий

**Файл:** `skladbot.py`, функция `request_matches_order_group`.

**Что было:**

```python
if parse_date_to_standard(group.get("date")) != parse_date_to_standard(request.get("unloading_date")):
    return False
```

Если обе даты приходили пустыми, `parse_date_to_standard` возвращал `""` для обеих сторон, `"" != ""` — это False, и проверка пропускала запись дальше. То есть дата по факту не была обязательной.

**Что стало:**

```python
group_date = parse_date_to_standard(group.get("date"))
request_date = parse_date_to_standard(request.get("unloading_date"))
if not group_date or not request_date or group_date != request_date:
    return False
```

Обе даты обязаны быть непустыми и строго равными после нормализации (`dd.mm.yyyy`). Если хотя бы одна пустая или не парсится — матчинг не делается.

**Зачем:** «Дата отгрузки» в листе `data` должна один-в-один совпадать с «Дата выгрузки» (`unloading_date`) в SkladBot. Без этого под одну строку могут схлопнуться заявки разных дней одного клиента.

**Тесты (`tests/test_skladbot_sync.py`):**

- `test_does_not_match_request_with_different_unloading_date` — заявка того же клиента за другой день не привязывается.
- `test_does_not_match_request_when_unloading_date_is_missing` — пустая дата в заявке SkladBot не даёт привязки.

### Матчинг заявок SkladBot: клиент сравнивается строго, без fuzzy токенов

**Файл:** `skladbot.py`, функция `request_matches_order_group`.

**Что было:**

```python
if not text_tokens_match(group.get("client"), request.get("recipient"), NOISE_COMPANY_TOKENS):
    return False
```

Нечёткое сравнение токенов с порогом 75% и вырезанием шумовых слов (`mchj`, `ooo`, `ип`, `мчж` и т.п.). Из-за этого:

- Похожие, но разные клиенты могли совпасть по токенам.
- При повторной синхронизации после второго импорта группа из одной строки могла «прицепиться» к заявке соседнего клиента, у которого совпали адрес и количество.

В рабочей выгрузке `TakSklad рабочая база.xlsx` (фильтр «Перечисление», 26.05.2026) это давало 14 номеров заявок, привязанных к 2 разным клиентам, и расхождение 40 шт по клиенту `"MARKET AL-KABIR" MChJ` между TakSklad и `Список_заказов_на_доставку_Чапамана_на_26_05_2026.xlsx`.

**Что стало:**

```python
group_client = normalize_lookup_text(group.get("client"))
request_recipient = normalize_lookup_text(request.get("recipient"))
if not group_client or not request_recipient or group_client != request_recipient:
    return False
```

Строгое равенство после `normalize_lookup_text` (приведение к нижнему регистру, `ё→е`, удаление `*` и `:`, схлопывание пробелов). Обе стороны обязаны быть непустыми.

**Зачем:** «Название компании/Имя человека» в SkladBot должно один-в-один совпадать с «Клиент» в листе `data`. Любое расхождение — это другая компания.

**Поведение в граничных случаях:**

- Если у одного клиента в SkladBot 2+ подходящих заявки (после нескольких импортов) — статус становится `Несколько совпадений` вместо случайной привязки.
- Если recipient в SkladBot пустой или отличается формулировкой, которую `normalize_lookup_text` не схлопывает (например лишняя пунктуация) — статус `Не найдено`. Видно в столбце «Статус SkladBot», правится вручную.

**Тесты (`tests/test_skladbot_sync.py`):**

- `test_does_not_match_request_from_different_client` — заявка от чужого клиента с теми же адресом, оплатой, датой и количеством не привязывается.

### Заведён журнал изменений

**Файл:** `docs/changelog.md` (этот файл).

Заведено правило: при любой правке в коде сюда добавляется запись с файлом, диффом сути, причиной и тестами.

## Что осталось за рамками этих правок

- `address_matches` всё ещё нечёткое (порог 55% токенов). При необходимости — сделать строгим аналогично клиенту и дате.
- `match_group_to_requests` требует, чтобы набор товаров заявки полностью совпадал с набором товаров группы. После второго импорта в группу попадает только новая строка — она не совпадёт с полной заявкой SkladBot и пометится `Не найдено`. Отдельный вопрос: разрешать ли частичное сопоставление или пересинхронизировать всю группу при дозагрузке.
- Сверку правок на живом SkladBot API я выполнить из своей среды не могу (внешний доступ к `api.skladbot.ru` заблокирован прокси). После прогона правки локально пришли свежий `TakSklad рабочая база.xlsx` — проверю, что номера встали корректно.

### Безопасный Git-снимок desktop-стабилизации

**Дата:** 2026-05-29.

**Что сделано:**

- Подготовлен Git-снимок текущей стабилизации без публикации нового автообновления.
- `version.json` закреплен на `1.1.7`, `mandatory: false`, ссылки на загрузку и SHA очищены.
- Документация очищена от конкретных значений Google service account, `private_key_id` и `SPREADSHEET_ID`.
- Зафиксировано правило: обычный push кода не должен менять публичный manifest обновления для рабочих компьютеров.

**Что не сделано:**

- Новый release-архив не собирался.
- Новый tag/release не публиковался.
- Push-уведомление об обновлении не готовилось.

**Проверки:**

- Python compile - успешно.
- Unit tests - 35 тестов пройдены.
- `version.json` - валидный JSON и закреплен на `1.1.7`.
- Старое имя проекта в рабочем дереве не найдено.
- Ручной Windows-smoke остается обязательным перед release-архивом.

### GitHub-репозиторий переименован в TakSklad

**Дата:** 2026-05-30.

**Что сделано:**

- Репозиторий GitHub переименован со старого исторического имени на `1fear/TakSklad`.
- Локальный `origin` переключен на `https://github.com/1fear/TakSklad.git`.
- Проверено, что новый репозиторий доступен, `main` на месте.
- Старый URL GitHub редиректит на новый репозиторий.

**Что не менялось:**

- `version.json` не повышался: рабочая линия остается `1.1.7`.
- Новый release/tag не создавался.
- Workflow-сборка Windows не запускалась.
- Push-уведомления об обновлении не готовились.

**Проверки:**

- Python compile - успешно.
- Unit tests - 35 тестов пройдены.
- `version.json` - валидный JSON и закреплен на `1.1.7`.
- `git diff --check` - успешно.

### Desktop-стабилизация без релиза

**Дата:** 2026-05-30.

**Файлы:** `src/taksklad/main.py`, `src/taksklad/sheets.py`, `src/taksklad/skladbot.py`, `src/taksklad/skladbot_sync.py`, `src/taksklad/app_skladbot.py`.

**Что сделано:**

- Google Sheets ошибки теперь переводятся в понятные сообщения: доступ к таблице, повреждённый ключ, quota/429, сеть/DNS/timeout/SSL.
- Ошибка обновления списка заказов больше не идёт через критическое окно приложения и Telegram-лог: оператор видит мягкое сообщение, а последний список остаётся доступным.
- Если обновление списка уже идёт, повторное нажатие показывает длительность операции и явно говорит, что можно работать с уже загруженным списком.
- Для долгого обновления добавлен UI-статус каждые 15 секунд, чтобы было видно, что приложение не зависло.
- SkladBot ошибки нормализованы: неверный API-токен, 429, timeout/network и некорректный JSON.
- SkladBot-синхронизация не падает наружу при ошибке чтения/записи Google Sheets; она возвращает результат с `errors` и не блокирует список заказов.

**Тесты:**

- Добавлены проверки Google-friendly messages.
- Добавлены проверки SkladBot-friendly messages.
- Добавлены проверки, что SkladBot read/write failure не выбрасывает исключение.
- Добавлена проверка fallback-сообщения обновления списка.
- Полный набор: 42 теста пройдены.

**Что не менялось:**

- `version.json` не повышался и остается на `1.1.7`.
- Релиз, тег, Windows-архив и push-уведомление не создавались.

### VDS staging: импорт заказов, backup и Traefik routing

**Дата:** 2026-05-30.

**Файлы:** `backend/app/main.py`, `backend/app/imports_service.py`, `backend/app/schemas.py`, `deploy/vds/docker-compose.yml`, `deploy/vds/backup_postgres.sh`, `deploy/vds/restore_postgres.sh`, `tests/test_backend_api_persistence.py`.

**Что сделано:**

- Добавлен `POST /api/v1/imports` для загрузки заказов в Postgres.
- Добавлен `GET /api/v1/imports` для истории импортов.
- Импорт поддерживает текущие русскоязычные поля desktop/Excel/Google-формата.
- Заказы группируются по дате, клиенту, адресу, оплате, представителю и заявке SkladBot.
- Повторный импорт той же позиции пропускается как дубль.
- Добавлены ручные backup/restore-скрипты Postgres.
- Для backend/adminer добавлен label `traefik.docker.network`, чтобы Traefik всегда проксировал через внешнюю Docker-сеть.

**Тесты и smoke:**

- Полный локальный набор: 53 теста пройдены.
- py_compile прошел.
- compose config для VDS и Traefik прошел.
- shell syntax backup/restore прошел.
- Локальный Docker/Postgres smoke прошел.
- VDS staging smoke прошел: health, auth `401`, import, duplicate import, scans, duplicate scan, complete checks, import history, backup, cleanup.

**Что не менялось:**

- `version.json` не повышался.
- Windows-архив не собирался.
- GitHub Release/tag не создавался.
- Push-уведомление рабочим компьютерам не отправлялось.

## 2026-05-31 - Telegram Import, Logistics Coordinates, SkladBot Blocks, KIZ By Source File

- Добавлена локальная точка восстановления перед MVP-доработками.
- Telegram-бот переведён на нижнее меню: дата отгрузки, отчёт логистики, Выгрузка КИЗов.
- Excel import теперь принимает дату отгрузки от менеджера, координаты, суммы и цены.
- Количество для SkladBot приводится к блокам; штуки/пачки остаются для отчётов.
- Если цена/сумма не пришла в Excel, сумма считается по `240000` сум за блок.
- Логистический отчёт выгружается отдельным Excel по выбранной дате и содержит координаты как основное поле для логистики.
- SkladBot matching проверяет только `3PL отгрузка`, дату, клиента, оплату, нормализованный товар и блоки.
- Адрес SkladBot больше не блокирует совпадение.
- Добавлены backend-эндпоинты КИЗ по исходным файлам: список завершённых файлов и Excel-выгрузка по файлу.
- Исправлен Telegram polling timeout для `getUpdates`.
- SkladBot worker теперь пропускает API-вызов без активных backend-заказов, обрабатывает `429` и сверяет заявки по `unloading_date`.
- На существующей заявке SkladBot `WH-R-190960` проверен реальный match без создания новой заявки в WMS.
- Тесты: `python -m unittest discover -s tests` - 74 OK.

### Уточнение После Финального Брифа Chapman

- Desktop SkladBot больше не отсекает совпадение из-за отличающегося адреса.
- Desktop SkladBot принимает оба названия типа заявки: `Отгрузка 3PL` и `3PL отгрузка`.
- Яндекс Геокодер в desktop убирает страну из адреса: `Узбекистан, Ташкент...` превращается в `Ташкент...`.
- Логистический backend-отчёт теперь требует координаты; если координат нет, отдаёт ошибку `409` вместо пустого файла.
- Координаты с третьим компонентом, например `41.214609,69.223027,15`, нормализуются до `41.214609,69.223027`.
- КИЗ-отчёт по исходному файлу получил лист `Сводка` с общей суммой заказа, планом и фактическим количеством блоков.
- Реальные Excel-шаблоны из Telegram проверены parser'ом: 5 файлов, координаты найдены во всех строках, предупреждений нет.
- Тесты: `python -m unittest discover -s tests` - 79 OK.

### Backend API MVP закрыт дневным отчётом и автоматическим backup

**Дата:** 2026-05-30.

**Файлы:** `backend/app/reports_service.py`, `backend/app/main.py`, `backend/app/schemas.py`, `tests/test_backend_api_persistence.py`, `deploy/vds/install_backup_timer.sh`, `deploy/vds/systemd/*`, `backend/README.md`.

**Что сделано:**

- `GET /api/v1/reports/day` больше не заглушка `501`.
- Дневной отчёт строится из Postgres по заказам, позициям и сканам.
- В отчёт попадают заказы выбранной даты и заказы, по которым были сканы в выбранную дату.
- Возвращаются totals: заказы, активные/закрытые заказы, позиции, план блоков, отсканировано, сканы за день, остаток, количество КИЗ.
- Добавлена группировка по типу оплаты: `terminal`, `transfer`, `unknown`.
- В строках заказов сохраняется номер заявки SkladBot, если он был импортирован.
- Добавлен systemd timer `taksklad-postgres-backup.timer` для ежедневного backup Postgres на VDS.

**Тесты и smoke:**

- Полный локальный набор: 55 тестов пройдены.
- py_compile прошел.
- compose config для VDS и Traefik прошел.
- shell syntax backup/restore/install scripts прошел.
- VDS staging пересобран и поднят.
- systemd backup timer включен, ручной запуск service создал backup-файл.
- VDS smoke для `/reports/day` прошел на временном заказе; smoke-данные удалены.

**Что не менялось:**

- `version.json` не повышался.
- Windows-архив не собирался.
- GitHub Release/tag не создавался.
- Push-уведомление рабочим компьютерам не отправлялось.
