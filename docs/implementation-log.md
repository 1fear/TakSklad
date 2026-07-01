# Журнал Работ По Проекту

Документ фиксирует ход работ: что сделано, что не сделано, какие ошибки найдены, какие решения приняты и что требует проверки. Новые записи добавляются сверху.

## 2026-07-01

### Pending event queue indexes and deploy workflow bootstrap

- Причина: активный tracker-хвост `Ускорить TakSklad queue checks` требовал индексировать hot-path выборки очереди, а первый CI/CD deploy должен работать с текущим VDS app dir, который может быть не git checkout.
- Изменено:
  - добавлена Alembic migration `20260701_0007_pending_event_indexes.py` с duplicate-safe `CREATE INDEX IF NOT EXISTS`;
  - `pending_events` получил индексы `status, created_at, id`, `status, updated_at, id`, `event_type, status, created_at, id`, `event_type, status, updated_at, id`, `updated_at, created_at, id`;
  - SQLAlchemy metadata, bootstrap SQL и readiness head подняты до `20260701_0007`;
  - `deploy/vds/deploy_from_git.sh` умеет деплоить в non-git app dir через временный clone и `rsync --delete` с exclude для `.env*`, `outputs`, `backups`, runtime logs, restore points, virtualenv и build/cache каталогов;
  - public `/health` и `/ready` в deploy-скрипте проверяются с retry, чтобы transient 404/502 сразу после recreate не помечал успешный deploy как failed;
  - `TAKSKLAD_DEPLOY_ACCEPTANCE=optional` теперь не блокирует deploy при no-go от `acceptance_status.sh`; `required` остается строгим и падает при missing/no-go acceptance.
- Локальные проверки:
  - `bash -n deploy/vds/deploy_from_git.sh` - OK;
  - `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. ./.venv/bin/python -m py_compile backend/app/models.py backend/app/health_service.py backend/migrations/versions/20260701_0007_pending_event_indexes.py tests/test_backend_skeleton.py tests/test_backend_api_persistence.py tests/test_ci_cd_workflows.py` - OK;
  - `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. ./.venv/bin/python -m unittest tests.test_backend_skeleton tests.test_ci_cd_workflows tests.test_backend_api_persistence.BackendApiPersistenceTests.test_failed_import_creates_linked_incident_and_resolve_removes_readiness_blocker tests.test_backend_api_persistence.BackendApiPersistenceTests.test_readiness_accepts_pending_event_indexes_schema_head_revision tests.test_backend_api_persistence.BackendApiPersistenceTests.test_readiness_degrades_when_migration_state_is_missing_or_wrong` - 16 tests OK;
  - `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. ./.venv/bin/python -m unittest tests.test_backend_skeleton tests.test_ci_cd_workflows tests.test_backend_google_sheets_pending tests.test_backend_events` - 27 tests OK;
  - `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. ./.venv/bin/python -m unittest tests.test_backend_api_persistence` - 120 tests OK;
  - `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. ./.venv/bin/python -m alembic -c backend/alembic.ini heads` - `20260701_0007 (head)`;
  - `TAKSKLAD_ENV_FILE=.env.example docker compose --env-file deploy/vds/.env.example -f deploy/vds/docker-compose.yml config --quiet` - OK;
  - `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. ./.venv/bin/python tools/release_preflight.py --skip-network` - OK;
  - `npm --prefix frontend run build` - OK;
  - `bash -n deploy/vds/*.sh && git diff --check` - OK.

### Smartup late export split after SkladBot link

- Причина: по `"KAMALOVA KAMOLA ABDUXALIL QIZI"YTT` Smartup дал один адрес/юрлицо двумя разными deal id в разных частях export:
  - `Терминал 01.07.2026 Часть 1.xlsx`: `Chapman RED OP 20` + `Chapman Brown OP 20`, deal `258112497`;
  - `Терминал 01.07.2026 Часть 2.xlsx`: `Chapman Green OP 20`, deal `258183923`.
- Старое поведение: после части 1 TakSklad уже создавал SkladBot `WH-R-202581` на 2 блока; часть 2 позже добавляла Green в тот же backend order, но SkladBot-заявка уже была создана и не расширялась.
- Изменено:
  - отменена задержка SkladBot create до финального Smartup-слота;
  - `backend/app/imports_service.py` теперь для `smartup_auto` проверяет базовый backend order до добавления новой позиции;
  - если базовый order уже имеет `skladbot_request_number` или `skladbot_request_id`, новая строка не добавляется в старый order;
  - если для базового order уже есть pending event `skladbot_request_create`, новая строка также не добавляется в старый order, даже если WH-R еще не успел записаться в `raw_payload`;
  - для такой поздней выгрузки создается новый backend order со стабильным split-key по `order_key + source_batch_key`;
  - новый backend order получает собственный SkladBot dry-run/create path, значит будет создана отдельная WH-заявка под то же юрлицо/адрес/дату;
  - `preview_import` использует тот же split-key, поэтому preview и реальный import не расходятся.
- Текущий Kamalova `WH-R-202581` кодом не ремонтировался: Антон вручную создает отдельную SkladBot-заявку на +1 Green.
- Локальные проверки:
  - `PYTHONPATH=. ./.venv/bin/python -m unittest tests.test_backend_api_persistence.BackendApiPersistenceTests.test_smartup_late_export_splits_when_existing_order_already_has_skladbot_request` - OK;
  - `PYTHONPATH=. ./.venv/bin/python -m unittest tests.test_backend_api_persistence.BackendApiPersistenceTests.test_smartup_late_export_splits_when_existing_order_has_pending_skladbot_create` - OK;
  - `PYTHONPATH=. ./.venv/bin/python -m unittest tests.test_backend_api_persistence.BackendApiPersistenceTests.test_smartup_late_export_splits_when_existing_order_already_has_skladbot_request tests.test_backend_api_persistence.BackendApiPersistenceTests.test_smartup_late_export_splits_when_existing_order_has_pending_skladbot_create tests.test_backend_api_persistence.BackendApiPersistenceTests.test_import_keeps_same_business_item_when_source_import_id_differs tests.test_backend_api_persistence.BackendApiPersistenceTests.test_import_skips_duplicate_rows_inside_same_payload` - OK;
  - `PYTHONPATH=. ./.venv/bin/python -m unittest tests.test_smartup_auto_import tests.test_backend_skladbot_request_dry_run` - 62 tests OK;
  - `PYTHONPATH=. ./.venv/bin/python -m py_compile backend/app/imports_service.py backend/app/smartup_auto_import.py backend/app/skladbot_request_dry_run.py tests/test_backend_api_persistence.py tests/test_smartup_auto_import.py tests/test_backend_skladbot_request_dry_run.py` - OK.
- Production deploy:
  - app path: `/opt/stacks/taksklad/app`;
  - restore point: `/opt/stacks/taksklad/restore_points/pre-smartup-linked-split-20260701T124040Z`;
  - Postgres backup: `/opt/taksklad/backups/postgres/taksklad-postgres-20260701T124040Z.sql.gz`;
  - selective hotfix applied to runtime `backend/app/imports_service.py`, `backend/app/smartup_auto_import.py`, `backend/app/skladbot_request_dry_run.py`;
  - rebuilt/recreated `backend-api`, `smartup-auto-import-worker`, `skladbot-worker`;
  - runtime smoke: linked `smartup_auto` order splits, `excel` does not split, split key is stable, old `not_before` defer code absent from runtime files;
  - public `/health` - OK, version `2.0.25`;
  - public `/ready` remains `degraded` from old unrelated `telegram_excel_import` failures and one `google_sheets_export` pending;
  - `SMARTUP_AUTOMATION_RUNTIME_REQUIRED=1 ./deploy/vds/verify_smartup_automation.sh` - `status=ok`;
  - current Kamalova order unchanged by code deploy: `WH-R-202581`, Brown `1/1`, Red `1/1`, Green `0/1`;
  - fresh logs for rebuilt services had no `ERROR`, `CRITICAL`, `Traceback`, `Exception`, `panic`, `NameError`.
- Pending-create follow-up deploy:
  - restore point: `/opt/stacks/taksklad/restore_points/pre-smartup-pending-split-20260701T125238Z`;
  - Postgres backup: `/opt/taksklad/backups/postgres/taksklad-postgres-20260701T125238Z.sql.gz`;
  - selective hotfix applied to runtime `backend/app/imports_service.py`;
  - container `py_compile` for `app/imports_service.py` - OK;
  - rebuilt/recreated `backend-api`, `smartup-auto-import-worker`;
  - runtime smoke with rollback: pending `skladbot_request_create` forces split, `excel` does not split, split key starts with `late-skladbot-split:`;
  - public `/health` - OK, version `2.0.25`;
  - public `/ready` remains `degraded` from old unrelated queue events and Google mirror pending;
  - `./deploy/vds/verify_smartup_automation.sh` - `status=ok`;
  - fresh logs for `backend-api` and `smartup-auto-import-worker` had no `error`, `traceback`, `exception`, `critical`, `failed`.

### SkladBot duplicate SKU aggregation before create

- Причина: Smartup/Excel может дать один и тот же SKU несколькими строками внутри одной будущей SkladBot-заявки. На примере `"YASMINA GROUP 555" MCHJ` файл `/Users/anton/Documents/Telegram/Терминал 30.06.2026 Часть 2.xlsx` содержит `Chapman Green OP 20` двумя строками по 1 блоку с разными Smartup deal/import id. SkladBot карточка показывала 5 блоков, а экран обработки схлопывал дубль товара некорректно.
- Изменено:
  - `backend/app/skladbot_request_dry_run.py` теперь агрегирует ready-продукты перед dry-run/create payload по SkladBot product identity: `product_data_id`, `barcode`, `is_main_barcode`;
  - исходные строки не удаляются из TakSklad/Google evidence, в dry-run product сохраняется `source_products`;
  - добавлен регрессионный тест: две строки `Chapman Green OP 20` по 1 блоку уходят в SkladBot payload одной позицией `amount=2`.
- Локальные проверки:
  - `PYTHONPATH=. ./.venv/bin/python -m unittest tests.test_backend_skladbot_request_dry_run` - 30 tests OK;
  - `./.venv/bin/python -m compileall -q backend/app/skladbot_request_dry_run.py tests/test_backend_skladbot_request_dry_run.py` - OK;
  - `git diff --check -- backend/app/skladbot_request_dry_run.py tests/test_backend_skladbot_request_dry_run.py` - OK.
- Production deploy:
  - app path: `/opt/stacks/taksklad/app`;
  - Postgres backup: `/opt/taksklad/backups/postgres/taksklad-postgres-20260701T120248Z.sql.gz`;
  - restore point: `/opt/stacks/taksklad/restore_points/pre-skladbot-sku-aggregation-20260701T120259Z`;
  - selective hotfix applied only to server `backend/app/skladbot_request_dry_run.py`, because local file currently also contains unrelated dirty `representative_contacts` changes that are not deployed on VDS;
  - rebuilt/recreated `backend-api` and `smartup-auto-import-worker`;
  - container `py_compile` for `app/skladbot_request_dry_run.py` in both services - OK;
  - runtime aggregation smoke inside `backend-api`: `[(2430805, 2, 2), (2189392, 1, 1)]`;
  - public `/health` - OK, version `2.0.25`;
  - public `/ready` remains `degraded` from old `telegram_excel_import` failures and one `google_sheets_export` pending, not from this hotfix;
  - fresh logs for `backend-api` and `smartup-auto-import-worker` had no `ERROR`, `CRITICAL`, `Traceback`, `Exception`, `panic`, `NameError`;
  - `SMARTUP_AUTOMATION_RUNTIME_REQUIRED=1 ./deploy/vds/verify_smartup_automation.sh` - `status=ok`, pending SkladBot creates `0`;
  - `./deploy/vds/acceptance_status.sh` could not run because `/opt/stacks/taksklad/app/outputs/taksklad_acceptance/acceptance_manifest.json` is absent.

### Controlled GitHub CI/CD scaffold

- Цель: добавить CI/CD без автодеплоя production от обычного `push`.
- Изменено:
  - добавлен `.github/workflows/ci.yml`: Python compile, unittest discovery, Alembic head check, VDS compose config, frontend build;
  - добавлен `.github/workflows/deploy-production.yml`: только ручной `workflow_dispatch`, GitHub Environment `production`, SSH deploy на VDS через secrets;
  - добавлен `deploy/vds/deploy_from_git.sh`: tracked dirty guard, restore point, Postgres backup, git checkout selected ref, Alembic upgrade, compose rebuild, `/health`, `/ready`, optional/required `acceptance_status.sh`, fresh log scan;
  - добавлен `tests/test_ci_cd_workflows.py`;
  - `docs/deploy-rollback-runbook.md` получил раздел `Controlled CI/CD`.
- Инварианты:
  - `push main` не деплоит production;
  - production secrets и `.env` не попадают в GitHub workflow или репозиторий;
  - CI/CD restore point исключает `.env*`, `node_modules`, `dist`, `__pycache__` и `*.pyc`;
  - deploy workflow требует `VDS_SSH_KNOWN_HOSTS`, а не отключает SSH host checking;
  - workflow не трогает PostgreSQL без backup.
- Проверено:
  - `bash -n deploy/vds/*.sh` - OK;
  - Ruby YAML parse для `.github/workflows/ci.yml`, `.github/workflows/deploy-production.yml`, `.github/workflows/build-windows-release.yml` - OK;
  - `PYTHONPATH=. ./.venv/bin/python -m unittest tests.test_ci_cd_workflows tests.test_vds_acceptance_scripts tests.test_windows_release_workflow` - 11 tests OK;
  - `TAKSKLAD_ENV_FILE=.env.example docker compose --env-file deploy/vds/.env.example -f deploy/vds/docker-compose.yml config --quiet` - OK;
  - `PYTHONPATH=. ./.venv/bin/python -m compileall -q deploy/vds tools tests` - OK;
  - `npm --prefix frontend run build` - OK;
  - `PYTHONPATH=. ./.venv/bin/python -m alembic -c backend/alembic.ini heads` - `20260701_0006 (head)`;
  - `git diff --check` - OK.
- Локальный full `unittest discover -s tests` был запущен, но текущий Homebrew Python в `.venv` не имеет `_tkinter`, поэтому Tk-dependent desktop test modules не импортируются. В GitHub CI добавлена установка `python3-tk` перед full discovery.

### YASMINA WH-R-202405 SkladBot blocks production DB repair

- Причина: по клиенту `"YASMINA GROUP 555" MCHJ` backend показывал `5` блоков, а уже созданная SkladBot-заявка `WH-R-202405` содержит `4` блока. Заявку SkladBot не меняли.
- Scope:
  - runtime host: `api.taksklad.uz`, app path `/opt/stacks/taksklad/app`;
  - order `a949d657-e20a-4def-944c-f20c560edba9`, SkladBot `WH-R-202405`, ID `202405`;
  - удалена только лишняя неотсканированная repair-позиция `d06afa87-471a-4cda-bd88-2429fc25cbb1`;
  - source repair import `4fbd25ae-9c27-4fb9-bf31-089f4021761e`, `smartup:257984858:1541071310:1`.
- Backup:
  - Postgres backup: `/opt/taksklad/backups/postgres/taksklad-postgres-20260701T075633Z.sql.gz`;
  - SHA256 `f4512ed14374039cecf3d3f73682e63314f1ae5b28988256d9ecd3c8f433659f`;
  - affected rows snapshot: `/opt/stacks/taksklad/repair_evidence/yasmina-wh-r-202405-blocks-before-20260701T075719Z.json`;
  - SHA256 `c3f0e5b736a31754b712308ff4bc5c58f71e0fba72a16c2a65d7b50ca33268f0`.
- Изменено:
  - удалена позиция `d06afa87-471a-4cda-bd88-2429fc25cbb1`, у которой `quantity_blocks=1`, `scanned_blocks=0`, `status=not_completed`;
  - заказ переведен в `completed`, потому оставшиеся 4 позиции имеют `quantity_blocks=1`, `scanned_blocks=1`, `status=completed`;
  - в `orders.raw_payload` добавлен `manual_skladbot_blocks_repair`;
  - в `audit_log` записаны `manual_skladbot_blocks_repair_item_deleted` и `manual_skladbot_blocks_repair_order_completed`;
  - поставлены и обработаны Google events `google_sheets_delete_import_records_export` и `google_sheets_archive_export`.
- Проверено:
  - production SQL: order `a949d657-e20a-4def-944c-f20c560edba9` = `db_blocks=4`, `scanned_blocks=4`, `item_count=4`, linked SkladBot amounts `[1, 1, 1, 1]`;
  - удаленная позиция в `order_items` отсутствует;
  - оба Google events завершились `completed`, `failed=0`, `remaining=0`;
  - актуальный rebuild dry-run для repair import: `linked_mismatch=0`, `orders=0`, `events_queued=0`;
  - новых `google_sheets_backend_sync_conflict` по order/item после repair нет;
  - `https://api.taksklad.uz/health` - OK, backend `2.0.25`;
  - `/ready` остается `degraded` из-за старых `telegram_excel_import` failed events и отдельного pending `google_sheets_export`, не из-за этой правки.

### Web panel admin table server-side filters

- Симптом: на `taksklad.uz` таблица заказов показывала `Показано 0 из 500 · всего 4 227` при фильтре `Активные`, хотя дневной summary показывал активные заказы.
- Причина: frontend загружал первые 500 строк всех статусов и применял фильтры уже в браузере; активные строки могли лежать после первой страницы.
- Исправлено:
  - `/api/v1/admin/table` принимает `status_bucket`, `shipment_date`, `search`, `scan_state`, `skladbot_filter`, `google_sheet_status`;
  - backend применяет фильтры до `limit/offset`, а `total_rows/has_more` считает по отфильтрованному набору;
  - frontend передает текущие фильтры в `getAdminTable`, перезагружает таблицу при смене фильтров и снимает selection с невидимых заказов.
- Проверено:
  - `.venv/bin/python -m unittest tests.test_backend_api_persistence.BackendApiPersistenceTests.test_admin_table_returns_flat_rows_totals_and_recent_activity tests.test_backend_api_persistence.BackendApiPersistenceTests.test_admin_table_totals_are_not_limited_by_row_limit tests.test_backend_api_persistence.BackendApiPersistenceTests.test_admin_table_supports_offset_pagination_metadata tests.test_backend_api_persistence.BackendApiPersistenceTests.test_admin_table_filters_status_before_limit tests.test_backend_api_persistence.BackendApiPersistenceTests.test_admin_table_supports_server_side_ui_filters` - 5 tests OK;
  - `npm --prefix frontend run build` - OK;
  - `.venv/bin/python -m py_compile backend/app/admin_service.py backend/app/main.py` - OK;
  - `git diff --check` - OK.

### SkladBot representative contacts in comments and daily report

- Причина: при создании SkladBot-заявок TakSklad должен передавать не только тип оплаты, но и торгового представителя с рабочим/личным телефоном из локального справочника.
- Изменено:
  - добавлена таблица `representative_contacts` и Alembic migration `20260701_0006_representative_contacts`;
  - добавлен импортёр `tools/import_representative_contacts.py` для XLSX с колонками `ТП`, `Раб номер`, `Лич номер`, `Раб зона`;
  - SkladBot create payload для отгрузок и возвратов строит multiline comment: тип оплаты, ТП, рабочий номер, личный номер;
  - daily SkladBot XLSX получил колонку `Торговый представитель` на листах `Заявки` и `Товары заявок`.
- Инварианты:
  - первая строка comment остается типом оплаты для совместимости с текущим matching;
  - реальные телефоны не добавлены в git;
  - остатки, статусы, КИЗы и Google Sheets rows не меняются этой правкой.
- Локальные проверки:
  - `PYTHONPATH=. ./.venv/bin/python -m unittest tests.test_representative_contacts tests.test_backend_skladbot_request_dry_run tests.test_skladbot_daily_report tests.test_backend_skeleton tests.test_backend_api_persistence.BackendApiPersistenceTests.test_readiness_accepts_representative_contacts_schema_head_revision` - 70 tests OK;
  - `PYTHONPATH=. ./.venv/bin/python -m py_compile backend/app/representative_contacts.py backend/app/skladbot_request_dry_run.py backend/app/skladbot_return_requests.py backend/app/skladbot_daily_report.py backend/app/models.py backend/app/health_service.py tools/import_representative_contacts.py tests/test_representative_contacts.py tests/test_backend_skladbot_request_dry_run.py tests/test_skladbot_daily_report.py tests/test_backend_skeleton.py` - OK;
  - `PYTHONPATH=. ./.venv/bin/python -m alembic -c backend/alembic.ini heads` - `20260701_0006 (head)`;
  - in-memory dry-run импорта `/Users/anton/Documents/Telegram/номера тп (2).xlsx` - `rows=8 created=8 skipped=0`, без записи в рабочую БД;
  - `git diff --check` - OK.

### Smartup KIZ source-file grouping

- Причина: Smartup один XLSX export дробился в backend на несколько `ImportJob` по `delivery_date + deal_id`, а меню KIZ source-files группировало по `backend_import_id` и показывало один filename несколькими строками.
- Изменено:
  - Smartup auto import считает общий `source_batch_key` по дате export, части и SHA256 XLSX-файла;
  - `source_batch_key` сохраняется в строках импорта и `OrderItem.raw_payload`;
  - KIZ source-files использует `source_batch_key` только для Smartup-строк;
  - legacy Smartup-строки без нового поля, но с filename вида `Терминал ДД.ММ.ГГГГ Часть N.xlsx`, объединяются по этому filename;
  - ручные одинаковые Excel без Smartup batch-key по-прежнему разделяются по `backend_import_id`.
- Инварианты:
  - существующие заказы, сканы, КИЗы, SkladBot-заявки и Google Sheets не меняются;
  - если одна позиция Smartup batch не завершена, весь исходный файл считается не готовым к выгрузке;
  - ручные повторные загрузки одного и того же filename не склеиваются.
- Локальные проверки:
  - `.venv/bin/python -m unittest tests.test_smartup_auto_import.SmartupAutoImportTests.test_smartup_kiz_source_files_group_one_export_file_across_deal_imports tests.test_backend_api_persistence.BackendApiPersistenceTests.test_kiz_source_file_report_groups_legacy_smartup_same_export_file tests.test_backend_api_persistence.BackendApiPersistenceTests.test_kiz_source_file_report_separates_same_filename_by_import` - 3 tests OK;
  - `.venv/bin/python -m unittest tests.test_smartup_auto_import` - 32 tests OK;
  - `.venv/bin/python -m unittest tests.test_smartup_auto_import tests.test_backend_api_persistence.BackendApiPersistenceTests.test_kiz_reports_show_source_file_progress_and_allow_partial_date_export tests.test_backend_api_persistence.BackendApiPersistenceTests.test_kiz_source_file_report_separates_same_filename_by_import tests.test_backend_api_persistence.BackendApiPersistenceTests.test_kiz_source_file_report_groups_legacy_smartup_same_export_file tests.test_backend_telegram_import.BackendTelegramImportTests.test_telegram_worker_shows_kiz_source_files_with_progress tests.test_backend_telegram_import.BackendTelegramImportTests.test_telegram_worker_downloads_kiz_source_file_by_import_key tests.test_backend_telegram_import.BackendTelegramImportTests.test_telegram_worker_keeps_kiz_source_key_when_file_selected_by_index` - 38 tests OK;
  - `.venv/bin/python -m py_compile backend/app/imports_service.py backend/app/kiz_reports_service.py backend/app/smartup_auto_import.py tests/test_smartup_auto_import.py tests/test_backend_api_persistence.py` - OK;
  - `git diff --check` - OK.
- Production deploy:
  - restore point: `/opt/stacks/taksklad/restore_points/pre-smartup-kiz-source-grouping-20260701T073941Z`;
  - DB backup: `/opt/taksklad/backups/postgres/taksklad-postgres-20260701T073941Z.sql.gz`;
  - selective rsync отправил только `backend/app/imports_service.py`, `backend/app/kiz_reports_service.py`, `backend/app/smartup_auto_import.py`, related tests and docs;
  - rebuilt/recreated `backend-api` and `smartup-auto-import-worker`;
  - container `py_compile` for changed backend modules - OK;
  - public `/health` OK, version `2.0.25`;
  - public `/ready` stays `degraded` from known old `telegram_excel_import` failures and one pending `google_sheets_export`; DB and migrations OK at `20260626_0005`;
  - `SMARTUP_AUTOMATION_RUNTIME_REQUIRED=1 ./deploy/vds/verify_smartup_automation.sh` - `status=ok`, pending SkladBot creates `0`;
  - read-only production DB check confirmed `Терминал 01.07.2026 Часть 1.xlsx` now appears as one source-file aggregate with `batch:legacy-smartup-file` prefix, `34` items, `47` planned blocks;
  - fresh `backend-api` and `smartup-auto-import-worker` logs after deploy had no `ERROR`, `CRITICAL`, `Traceback`, `Exception` or `panic`.

### SkladBot linked request mismatch guard

- Причина: поздний `smartup_auto_repair` мог добавить позицию в уже связанную WH-R заявку. Backend становился больше, чем уже созданная SkladBot-заявка, но dry-run показывал только `already_linked`.
- Изменено:
  - SkladBot dry-run для linked order сравнивает текущие блоки DB с сохраненным linked SkladBot payload/detail;
  - если блоки отличаются, статус становится `linked_mismatch`, payload на повторное создание не ставится в очередь;
  - import response, admin dry-run UI и import issues показывают отдельный счетчик `linked_mismatch`.
- Инварианты:
  - SkladBot API на запись не вызывается;
  - существующие WH-R, остатки, КИЗы, Google Sheets и scan state не меняются;
  - решение по ручному исправлению конкретной WH-R остается операторским до подтвержденного safe update path.
- Локальные проверки:
  - `PYTHONPATH=. ./.venv/bin/python -m unittest tests.test_backend_skladbot_request_dry_run tests.test_smartup_auto_import` - 59 tests OK;
  - `./.venv/bin/python -m py_compile backend/app/skladbot_request_dry_run.py backend/app/imports_service.py backend/app/schemas.py tests/test_backend_skladbot_request_dry_run.py` - OK;
  - `npm run build` in `frontend/` - OK;
  - `git diff --check` - OK.
- Production deploy:
  - restore point: `/opt/stacks/taksklad/restore_points/pre-skladbot-linked-mismatch-20260701T062139Z`;
  - DB backup: `/opt/taksklad/backups/postgres/taksklad-postgres-20260701T062139Z.sql.gz`;
  - selective rsync: `backend/app/skladbot_request_dry_run.py`, `backend/app/imports_service.py`, `backend/app/schemas.py`, `frontend/src/App.tsx`, `frontend/src/api.ts`, `frontend/src/styles.css`, test and docs;
  - rebuilt/recreated `backend-api`, `skladbot-worker`, `smartup-auto-import-worker`, `frontend`.
  - после deploy пересобран diagnostic dry-run для repair import `4fbd25ae-9c27-4fb9-bf31-089f4021761e`: `linked_mismatch=1`, DB `5` блок., SkladBot `4` блок., без SkladBot/Google/order writes.
- VDS verification:
  - public `/health` OK, version `2.0.25`;
  - public `/ready` degraded only because of known old queue/Google mirror items; DB and migrations OK;
  - `SMARTUP_AUTOMATION_RUNTIME_REQUIRED=1 ./deploy/vds/verify_smartup_automation.sh` - `status=ok`, pending SkladBot creates `0`;
  - fresh logs for rebuilt containers had no `ERROR`, `CRITICAL`, `Traceback`, `Exception` or `panic`;
  - live frontend bundle contains `linked_mismatch` / `Расхождение`;
  - container `py_compile` for changed backend modules - OK;
  - runtime SHA256 matched local for `backend/app/skladbot_request_dry_run.py` and `frontend/src/App.tsx`.

### Web panel recovery after main branch drift

- Симптом на `taksklad.uz`: web-панель выглядела как старая версия и показывала ошибку `Запрос /api/v1/admin/table?offset=0&activity_limit=30 не ответил за 15 сек.`
- Причина: production frontend был собран из устаревшего `main`, где не было актуальной навигации 2.0.25; при этом стартовый `admin/table` тянул все позиции без `limit`, что давало тяжелый ответ и browser-side abort.
- Решение:
  - merged 2.0.25 rollout changes into `main`;
  - сохранен быстрый auth startup gate из main;
  - `refreshAll` запрашивает `/api/v1/admin/table` с `limit=500`;
  - таблица получила кнопку догрузки следующих 500 строк;
  - sidebar получил стабильную высоту и собственный scroll.
- Локальные проверки:
  - frontend build OK;
  - targeted backend tests: 234 OK;
  - full unittest discovery: 669 OK;
  - release preflight: status `ok`, public backend health OK, version `2.0.25`;
  - docker compose config OK;
  - Smartup automation source verifier OK, local runtime skipped because local compose service is not running.
- Production deploy:
  - restore point: `/opt/stacks/taksklad/restore_points/pre-web-panel-recovery-20260701T055754Z`;
  - DB backup: `/opt/taksklad/backups/postgres/taksklad-postgres-20260701T055755Z.sql.gz`;
  - selective rsync excluded `.env*`, `node_modules`, `dist`, `__pycache__` and `*.pyc`;
  - rebuilt/recreated `backend-api`, `frontend`, `telegram-worker`, `google-sheets-sync-worker`, `skladbot-worker`, `smartup-auto-import-worker`;
  - Alembic upgrade head completed;
  - public `/health` OK, version `2.0.25`;
  - public `/ready` stays `degraded` because of known old `telegram_excel_import` failed events and temporary Google mirror pending, while DB and migrations are OK;
  - live frontend bundle includes `Календарь`, `Smartup`, `История действий`, `Загрузить еще`;
  - production `admin_table(limit=500, offset=0)` returned 500 of 4193 rows in 1.561 sec, `has_more=true`;
  - VDS Smartup runtime verifier returned `status=ok`;
  - fresh container logs after deploy had no `ERROR`, `CRITICAL`, `Traceback`, `Exception` or `panic`.

## 2026-06-30

### Smartup automation phase audit follow-up

- Свежий аудит acceptance criteria нашел production-compose gap: `smartup-auto-import-worker` использует Smartup reverse geocode, но `YANDEX_GEOCODER_API_KEY` был проброшен только в `backend-api` и `telegram-worker`.
- Дополнительно закрыты слабые места из второго read-only review:
  - partial Smartup `change_status` больше не может поставить real SkladBot create-event по неподтвержденному `deal_id`;
  - Smartup import дробится по `delivery_date + deal_id`, чтобы SkladBot after-status queue был точным по заказу;
  - audit JSON создается до backend preview и обновляется статусом `failed_preview`, если preview падает;
  - добавлен тест ручного override `is_non_working=false` для выходного дня логистики;
  - user guide явно фиксирует, что `Терминал` enforce-ится локально после Smartup export.
- Исправлено:
  - `YANDEX_GEOCODER_API_KEY` добавлен в environment `smartup-auto-import-worker`;
  - VDS acceptance test теперь проверяет geocoder/block-price/SKU env именно в блоке `smartup-auto-import-worker`, а не просто где-то в compose;
  - Smartup verifier теперь ловит отсутствие geocoder env в compose-блоке worker, отсутствие partial-status guard и отсутствие preview-failure audit guard;
  - user guide уточнен: код safe-by-default, production включается только явными флагами после backup/smoke/runtime verifier.
- Проверено:
  - `.venv/bin/python -m unittest tests.test_smartup_auto_import` - 31 tests OK;
  - `.venv/bin/python -m unittest tests.test_smartup_auto_import tests.test_google_sheets_sync_worker tests.test_vds_acceptance_scripts` - 50 tests OK;
  - `.venv/bin/python -m py_compile backend/app/smartup_auto_import.py` - OK;
  - `bash -n deploy/vds/verify_smartup_automation.sh deploy/vds/acceptance_status.sh` - OK;
  - `git diff --check` - OK;
  - `TAKSKLAD_ENV_FILE=.env.example docker compose --env-file deploy/vds/.env.example -f deploy/vds/docker-compose.yml config --quiet` - OK;
  - `deploy/vds/verify_smartup_automation.sh` - source checks OK, local runtime skipped because local compose service is not running.
- VDS deploy:
  - актуальный сервер: `159.195.138.95`, app path `/opt/stacks/taksklad/app`;
  - restore point: `/opt/stacks/taksklad/restore_points/pre-smartup-audit-followup-20260630T165654Z`;
  - DB backup: `/opt/taksklad/backups/postgres/taksklad-postgres-20260630T165654Z.sql.gz`;
  - selective rsync: `backend/app/smartup_auto_import.py`, `deploy/vds/docker-compose.yml`, `deploy/vds/verify_smartup_automation.sh`, Smartup/VDS tests and docs;
  - rebuilt/recreated: `backend-api`, `smartup-auto-import-worker`.
- VDS verification:
  - `SMARTUP_AUTOMATION_RUNTIME_REQUIRED=1 ./deploy/vds/verify_smartup_automation.sh` - `status=ok`, runtime status `ok`, pending SkladBot creates `0`;
  - `python -m app.smartup_auto_import_worker status --json` in container - `ok`, `enabled=True`, pending SkladBot creates `0`;
  - public `/health` - `ok`, version `2.0.25`;
  - public `/ready` - `degraded` only because of old `telegram_excel_import` failed events; DB and migrations `ok`;
  - fresh `backend-api`/`smartup-auto-import-worker` logs since deploy - no `error|traceback|exception|critical|failed`;
  - `acceptance_status.sh` did not produce JSON because `/opt/stacks/taksklad/app/outputs/taksklad_acceptance/acceptance_manifest.json` is missing on this server.

### Smartup automation deploy/status guard

- Цель: перед включением/деплоем Smartup automation иметь быстрый read-only guard, который показывает безопасную конфигурацию и ловит регрессии по ключевым инцидентным местам.
- Изменено:
  - добавлен `build_smartup_auto_import_status()` без вывода секретов и chat_id;
  - `smartup_auto_import_worker` получил команду `status --json`;
  - добавлен `deploy/vds/verify_smartup_automation.sh` со статическими проверками порядка операций, geocoder, Telegram routing, delivery-date guard, SkladBot-after-status queue, `source_import_id` dedupe и repriced totals;
  - `acceptance_status.sh` запускает Smartup verifier с обязательным runtime-check на VDS.
- Инварианты:
  - status не вызывает Smartup API и не пишет в БД;
  - локально verifier может пропустить runtime, если compose service не запущен;
  - в VDS acceptance runtime-check обязателен через `SMARTUP_AUTOMATION_RUNTIME_REQUIRED=1`.
- Проверено:
  - `.venv/bin/python -m py_compile backend/app/smartup_auto_import.py backend/app/smartup_auto_import_worker.py` - OK.
  - `bash -n deploy/vds/verify_smartup_automation.sh deploy/vds/acceptance_status.sh` - OK.
  - `.venv/bin/python -m unittest tests.test_smartup_auto_import tests.test_google_sheets_sync_worker tests.test_vds_acceptance_scripts` - 47 tests OK.
  - `deploy/vds/verify_smartup_automation.sh` - source checks `ok`, local runtime `skipped`, потому что локальный `smartup-auto-import-worker` не запущен.

### Smartup delivery-date guard for controlled runs

- Причина: controlled `run-once` должен ограничивать не только дату Smartup export, статус `Новые` и оплату `Терминал`, но и исходный Smartup `delivery_date`, чтобы в текущую выгрузку не попали будущие даты отгрузки.
- Изменено:
  - `run-once` получил параметр `--delivery-date YYYY-MM-DD|DD.MM.YYYY`;
  - `filter_smartup_orders()` умеет фильтровать исходные Smartup orders по `delivery_date`;
  - delivery date добавляется в idempotency/advisory key только для controlled run с явным `--delivery-date`, scheduled run остается совместимым с прежним ключом.
- Проверка: `.venv/bin/python -m unittest tests.test_smartup_auto_import`.

### Smartup production recovery prep

- Цель: подготовить локальный Smartup update к production без включения live write-флагов и без риска потерять заказ между Smartup и TakSklad.
- Найденный риск:
  - старый полный flow делал Smartup `change_status` до `create_delivery_group_imports`;
  - если Smartup статус уже переведен в `В ожидании`, а backend import после этого падает, заказ может исчезнуть из фильтра `Новые + Терминал` и не попасть в TakSklad;
  - failed слот нельзя было повторить штатно: существующий `PendingEvent` с тем же `smartup:auto_import:v1:{date}:{slot}` всегда возвращал `slot_already_claimed`;
  - если процесс падал после backend import, но до `mark_smartup_slot_failed`, слот мог остаться в `processing` и заблокировать повтор;
  - общий `create_import` мог поставить реальный `skladbot_request_create` до Smartup `change_status`, если на сервере включен `SKLADBOT_CREATE_REQUESTS_MODE=enabled`.
- Решение:
  - полный flow теперь сначала пишет backend import в Postgres через существующий importer, затем делает Smartup `change_status`;
  - если `change_status` падает, заказ уже есть в TakSklad, слот остается `failed`, audit/alert сохраняют причину;
  - `claim_smartup_slot` разрешает повтор только для `failed` события или старого зависшего `processing` старше 30 минут, переводит его в `processing`, увеличивает `attempts` и сохраняет `retry_claimed_at`/`retry_reason`;
  - Smartup import внутри `create_import` принудительно делает SkladBot dry-run, а реальную SkladBot create-очередь ставит отдельным шагом только после успешного Smartup `change_status`;
  - `completed` и свежий текущий `processing` слот по-прежнему не выполняются повторно.
- Инварианты:
  - Smartup automation по умолчанию остается выключенной env-флагами;
  - backend import все еще требует включенный `SMARTUP_AUTO_IMPORT_CHANGE_STATUS_ENABLED`;
  - повтор failed/stale слота не создает дубль заказа, потому что importer видит тот же `ID импорта` и считает строку duplicate;
  - реальное создание SkladBot-заявок остается под существующим `SKLADBOT_CREATE_REQUESTS_MODE=enabled`, но для Smartup оно запускается только после Smartup status change.
- Проверено локально:
  - `.venv/bin/python -m unittest tests.test_smartup_auto_import` - 19 tests OK.
  - `.venv/bin/python -m unittest tests.test_backend_skladbot_request_dry_run tests.test_backend_api_persistence` - 135 tests OK.
  - `git diff --check` - OK.
  - `.venv/bin/python -m compileall backend/app tests tools src/taksklad main.py pyinstaller_entry.py` - OK.
  - `cd frontend && npm run build` - OK.
  - `TAKSKLAD_ENV_FILE=.env.example docker compose --env-file deploy/vds/.env.example -f deploy/vds/docker-compose.yml config --quiet` - OK.
  - `bash -n deploy/vds/*.sh` - OK.
  - `.venv/bin/python -m alembic -c backend/alembic.ini heads` - `20260626_0005 (head)`.
  - `.venv/bin/python -m unittest discover -s tests -p 'test_*.py'` - 653 tests OK.
  - `.venv/bin/python tools/release_preflight.py` - status `ok`, public backend health OK, version `2.0.24`.
  - `.venv/bin/python tools/release_go_no_go.py` - `no_go` из-за незакрытой ручной acceptance: Telegram import, SkladBot matching, Windows desktop acceptance, cleanup.

### Hotfix 2.0.25 KIZ reuse after return/undo/reset

- Симптом: desktop показывал, что КИЗ уже есть в базе, хотя оператор отменил последние коды или КИЗ уже прошел возврат.
- Причина:
  - backend truth мог уже иметь latest movement `return`, `undo` или `reset`;
  - desktop при этом сначала смотрел локальный `all_existing_codes` и блокировал скан до backend POST.
- Решение:
  - backend получил read-only endpoint `GET /api/v1/kiz/availability`;
  - desktop при stale duplicate-cache запрашивает backend availability;
  - duplicate block снимается только для backend-confirmed reusable movement `return`, `undo` или `reset`;
  - активный дубль в текущих заказах остается hard-block.
- Деплой делается изолированным hotfix от `origin/main`, чтобы не подтягивать 25 feature-commits текущей рабочей ветки.
- Release:
  - `APP_VERSION` desktop/backend поднят до `2.0.25`;
  - GitHub workflow `28451931009` собрал `TakSklad.exe` и `TakSklad-windows-x64.zip`;
  - `version.json` переведен на forced `2.0.25`, `mandatory=true`, `block_workflow=true`;
  - SHA `TakSklad.exe`: `32fdef699d44cc7c565c18367d331e3b05dba78cf05802b6050664950cd2b31a`;
  - SHA `TakSklad-windows-x64.zip`: `2e49825d25c6c3332f20984f4b4998e223c65500714bd66f1a9763be493e218d`;
  - metadata уже примененной production migration `20260626_0005` добавлена в hotfix-ветку, чтобы `/ready` не показывал ложный `revision_mismatch`.
- Production deploy:
  - restore point: `/opt/stacks/taksklad/restore_points/pre-kiz-dedup-rollout-2-0-25-20260630T143801Z`;
  - Postgres backup: `/opt/taksklad/backups/postgres/taksklad-postgres-20260630T143801Z.sql.gz`;
  - `backend-api` rebuilt and restarted with version `2.0.25`;
  - `https://api.taksklad.uz/health` вернул `status=ok`, backend `2.0.25`;
  - `https://api.taksklad.uz/ready` показал DB/migrations OK at `20260626_0005`, `google_mirror=ok`; общий `degraded` держат старые `telegram_excel_import` failures;
  - live availability check для проблемного КИЗа вернул `available=true`, `latest_movement_type=return`.

## 2026-06-29

### SkladBot daily first-seen incident hotfix

- Симптом: плановый отчет `TakSklad_SkladBot_daily_29.06.2026.xlsx` отправил в клиентский чат `Приемка: 13 заявок, 29173 блоков`, хотя лист `Движения` был пустой (`приход 0`, `расход 0`), а все приемки были старыми.
- Факты по XLSX:
  - всего в отчете было `165` заявок;
  - `13` приемок на `29173` блоков имели даты создания `01.05.2026`-`19.06.2026`;
  - причина включения у всех этих приемок: `впервые найдена выполненной`;
  - first-seen слой всего добавил `80` старых заявок и `29718` блоков.
- Причина: после расширения daily по движениям код снова разрешал fallback `впервые найдена выполненной` для старых SkladBot-заявок `Выполнена` + `В архиве`, если SkladBot не отдавал `updated_at`, `completed_at` или `archived_at`. При первом плановом обнаружении такие старые карточки попали в сегодняшний отчет пачкой.
- Решение:
  - лист `Заявки` и Telegram-счетчики daily снова фильтруются только по `created_at`/`createdAt` на дату отчета;
  - `updated_at`, `unloading_date`, `completed_at`, `archived_at` и `впервые найдена выполненной` больше не включают старые заявки в сегодняшний отчет;
  - scheduled-отправка больше не передает `reported_request_ids` в сборщик daily, чтобы registry не влиял на отчетную дату;
  - `/warehouse/transactions` остается отдельным источником движений за дату отчета: старый WH-R с сегодняшним движением виден в листе `Движения`, но не считается сегодняшней заявкой;
  - строки `Движений` дополнительно фильтруются по дате внутри ответа SkladBot, чтобы в отчет не прошли строки вне выбранного дня.
- Инварианты:
  - SkladBot используется read-only;
  - SkladBot-остатки, статусы, Google Sheets и Postgres-данные не изменяются;
  - исправление меняет только сбор/отображение daily report.
- Проверено локально:
  - `PYTHONPATH=. ./.venv/bin/python -m unittest tests.test_skladbot_daily_report` - 26 tests OK.
- Дополнительное hardening-исправление после ручной отправки правильного отчета:
  - `fetch_daily_requests` теперь отсекает старые list items до `get_request_detail`;
  - старые заявки не тормозят scheduled-рассылку и не могут съесть `SKLADBOT_DAILY_REPORT_DETAIL_LIMIT`;
  - добавлен тест, который падает, если старая заявка попадает в detail-запрос;
  - тест с сегодняшней заявкой после старых строк теперь проверяет, что не появляется ошибка лимита.
- VDS deploy:
  - commit: `e256bab Fix SkladBot daily report date filters`;
  - runtime host: `api.taksklad.uz`, app path `/opt/stacks/taksklad/app`;
  - restore point: `/opt/stacks/taksklad/restore_points/pre-skladbot-daily-date-filter-20260629T175417Z`;
  - Postgres backup: `/opt/taksklad/backups/postgres/taksklad-postgres-20260629T175417Z.sql.gz`;
  - selective sync: `backend/app/skladbot_daily_report.py`, `backend/app/telegram_worker.py`, `tests/test_skladbot_daily_report.py`, `docs/changelog.md`, `docs/implementation-log.md`, `docs/report-source-rules.md`;
  - deployed SHA256: `backend/app/skladbot_daily_report.py` = `712cbaa571e3108e43c122d758a9f7500b460293eb1fc45ca801488fcef2892c`, `backend/app/telegram_worker.py` = `14f087a5bd44072e880547ec869e20d3f64c75da6071ddef710dda56cb0f5906`;
  - `docker compose --env-file deploy/vds/.env -f deploy/vds/docker-compose.yml up -d --build telegram-worker` пересобрал `telegram-worker` и пересоздал `backend-api` как compose-зависимость;
  - VDS `telegram-worker` compileall по `app/skladbot_daily_report.py` и `app/telegram_worker.py` - OK;
  - `https://api.taksklad.uz/health` - OK, backend `2.0.24`;
  - `https://api.taksklad.uz/ready` - DB/migrations OK, общий `degraded` из-за старых `telegram_excel_import` и одного pending `google_sheets_export`, не из-за daily report deploy;
  - свежие логи `telegram-worker`/`backend-api` после рестарта - без `ERROR`, `Traceback`, `Exception`, `CRITICAL`, `failed`;
  - `deploy/vds/acceptance_status.sh` на текущем сервере не прошел из-за отсутствующего `/opt/stacks/taksklad/app/outputs/taksklad_acceptance/acceptance_manifest.json`, это отдельный missing acceptance artifact, не runtime daily report failure.

## 2026-06-26

### Hotfix 2.0.24 forced desktop update

- Симптом: рабочий ПК показал `Версия приложения: 2.0.22` и отклонил Green OP короб `010400639610445821...` как `КИЗ распознан как: не распознан`.
- Причина:
  - текущий HEAD уже содержит mapping `0104006396104458 -> green:op`;
  - public `version.json` был paused на `1.1.7`, поэтому ПК на `2.0.22` не получил hotfix `2.0.23`;
  - это не ошибка дедупликации и не ручной конфликт КИЗа, а stale desktop build.
- Ручная production-правка:
  - перед записью создан backup Postgres `taksklad-postgres-20260626T095159Z.sql.gz`;
  - в заказ `WH-R-201125` добавлены 4 коробочных КИЗа через backend `create_scan`;
  - `Chapman Green OP 20`: 2 короба, стало `100/100 completed`;
  - `Chapman Brown SSL 100\`20`: 2 короба, стало `100/100 completed`;
  - коды не существовали в `scan_codes` до записи, backend создал scan/audit/KIZ movement и Google mirror export queue.
- Release guard:
  - `APP_VERSION` desktop/backend поднят до `2.0.24`;
  - release/preflight/VDS guards переключены на forced `2.0.24`;
  - tests покрывают реальные Green/Brown SSL КИЗы из инцидента.
- Release:
  - GitHub workflow `28231545689` собрал `TakSklad.exe` и `TakSklad-windows-x64.zip`;
  - `version.json` переведен на forced `2.0.24`, `mandatory=true`, `block_workflow=true`;
  - SHA `TakSklad.exe`: `7fa3b0b9c9526a3833e55b6d41a916edc433d0ecb775407713fad3ebfdd61973`;
  - SHA `TakSklad-windows-x64.zip`: `c0446e6293f477975347b1ac8fc426e9d41a6f5fc33420688fd6be87c2b6d94b`.

### Backend-only hot path Phase 9 final hardening

- Причина: финально replay-нуть инцидент 2026-06-25 и убедиться, что улучшения не вернули Google/Telegram timeout в складской hot path и не ухудшили логику.
- Incident replay matrix:

| Сценарий | Ожидаемое поведение | Evidence |
| --- | --- | --- |
| Google 429/quota | Backend scan/complete/import остаются committed в Postgres, Google уходит в mirror queue/backoff | `tests.test_backend_api_persistence`, `tests.test_backend_google_sheets_pending`, readiness `google_mirror` split |
| Backend timeout на desktop refresh | В backend-only shadow нет скрытого Google fallback; есть кэш или явная backend error | `tests.test_refresh_fallback`, refresh source `backend`/`google_emergency_fallback` |
| Telegram send/import timeout | Событие остаётся retryable/visible в `pending_events`, order completion не портится | `tests.test_backend_telegram_import`, operations `telegram_worker_state` |
| App close во время Google backoff | Desktop не освобождает Google Telegram lock, если не владел свежим lock | `tests.test_desktop_ui_contract`, `tests.test_telegram_lock` |
| Stale desktop/release drift | Helper блокирует старый exe без manifest; preflight проверяет version contract и backend-only guardrails | `tests.test_release_preflight`, `tests.test_windows_test_build_helper` |

- Security/log review:
  - startup self-check выводит только hashes/origin/yes-no/counts;
  - refresh diagnostics выводит counts/source/flags, но не payload, клиентов, адреса, токены или КИЗы;
  - operations summary больше не выводит raw `last_error` и filenames, только `error=present`/counts/next action;
  - `rg` по runtime/docs на fake secret/token/full-KIZ patterns не нашёл утечек; тестовые fake secrets остаются только в tests и assert-ах redaction.
- Docs changed:
  - `docs/project-knowledge-base.md` - Postgres/backend hot path, Google mirror/export/legacy fallback;
  - `docs/user-business-process-guide.md` - backend-mode vs legacy Google-line;
  - `docs/project-architecture.md` - desktop + backend architecture instead of old Google-primary snapshot;
  - `docs/report-source-rules.md` - backend/Postgres source-of-truth rule;
  - `docs/taksklad-system-stack-overview.md` - current version/paused manifest;
  - `docs/deploy-rollback-runbook.md`, `docs/windows-backend-acceptance.md`, `docs/manual-acceptance-runbook.md` - shadow rollout, rollback and dirty-tree deploy guards.
- Final recommendation:
  - code is ready for controlled shadow rollout on one Windows workstation/test profile;
  - do not broad-rollout backend-only to all PCs until Windows physical smoke passes;
  - keep emergency Google fallback off by default and enable it only manually/temporarily;
  - production deploy must be selective from reviewed diff/commit with restore point and `/ready` + `/api/v1/admin/operations` checks.
- Проверено:
  - `.venv/bin/python -m unittest discover -s tests` - 639 tests OK;
  - `npm --prefix frontend run build` - OK;
  - `.venv/bin/python -m compileall src/taksklad backend/app` - OK;
  - `.venv/bin/python tools/release_preflight.py` - `status=ok`, `backend_only_hot_path_contract=ok`, `deploy_runbook_contract=ok`, public backend `/health` OK `version=2.0.23`;
  - `git diff --check` - OK.

### Backend-only hot path Phase 8 release guards

- Причина: backend-only hot path нельзя выпускать из частично обновлённого desktop/helper/runbook или широким deploy из dirty tree.
- Изменено:
  - `tools/windows_backend_acceptance.ps1` получил workstation-local switches `-BackendOnlyRefresh`, `-EmergencyGoogleFallback`, `-EnableDesktopTelegramPolling`;
  - helper теперь задаёт и очищает `TAKSKLAD_BACKEND_ONLY_REFRESH`, `TAKSKLAD_BACKEND_EMERGENCY_GOOGLE_FALLBACK_ENABLED`, `TELEGRAM_DESKTOP_POLLING_ENABLED`;
  - readiness expected Alembic head обновлён до `20260626_0005`, чтобы deploy с logistics calendar migration не переводил `/ready` в ложный `degraded`;
  - `tools/release_preflight.py` получил checks `backend_only_hot_path_contract` и `deploy_runbook_contract`;
  - preflight падает, если пропали startup diagnostics, explicit emergency fallback, guarded Telegram lock release, refresh source diagnostics, `/admin/operations shadow_diagnostics`, dirty-tree deploy запрет или pending-preservation rollback;
  - `docs/deploy-rollback-runbook.md` теперь требует `git status --short`, restore point, selective deploy и запрещает broad rsync из dirty tree;
  - `docs/manual-acceptance-runbook.md` добавил Windows shadow smoke: startup diagnostics, backend refresh, network timeout, Google 429 simulation.
- Version contract:
  - `version.json` не менялся;
  - preflight подтвердил допустимое состояние `paused`: `latest_version=1.1.7`, `min_supported_version=1.1.7`, `mandatory=false`;
  - forced rollout `2.0.23` остаётся допустим только при заполненных GitHub Release URLs и SHA.
- Проверено:
  - `.venv/bin/python -m unittest tests.test_backend_api_persistence.BackendApiPersistenceTests.test_readiness_accepts_logistics_calendar_schema_head_revision tests.test_backend_skeleton.BackendSkeletonTests.test_sql_bootstrap_and_alembic_migrations_keep_forward_only_contract` - OK;
  - `.venv/bin/python -m unittest tests.test_release_preflight tests.test_windows_test_build_helper tests.test_vds_acceptance_scripts` - 22 tests OK;
  - `.venv/bin/python tools/release_preflight.py` - `status=ok`, `backend_only_hot_path_contract=ok`, `deploy_runbook_contract=ok`, public backend `/health` OK `version=2.0.23`;
  - `.venv/bin/python -m compileall src/taksklad backend/app` - OK;
  - `git diff --check` - OK.

### Backend-only hot path Phase 7 shadow cutover

- Причина: backend-only нельзя включать сразу на все ПК. Сначала нужен один workstation/test profile, видимые флаги режима, shadow diagnostics и проверяемый rollback без потери локальных очередей.
- Изменено:
  - startup self-check теперь показывает `backend_only_refresh=yes/no` и `backend_emergency_google_fallback=yes/no`;
  - desktop refresh diagnostics использует фактический `primary_source`, поэтому emergency fallback не маскируется как `source=backend`;
  - `fetch_sheet_data_with_sync()` передаёт в diagnostics флаги `backend_only_refresh`, `emergency_google_fallback` и `google_sheets_pending`;
  - `/api/v1/admin/operations` получил `shadow_diagnostics` с backend source, Google mirror lag/pending/failed/processing exports, stale processing и Telegram worker state;
  - Windows acceptance runbook получил отдельный Phase 7 shadow profile для одного ПК и rollback с pending-preservation checks.
- Feature flags:
  - `TAKSKLAD_BACKEND_ONLY_REFRESH`: default `false`, на одном shadow ПК ставить `1`;
  - `TAKSKLAD_BACKEND_EMERGENCY_GOOGLE_FALLBACK_ENABLED`: default `false`, временно ставить `1` только вручную;
  - `TELEGRAM_DESKTOP_POLLING_ENABLED`: default `false`, в backend-mode оставлять `0`.
- Sanitized samples:
  - desktop: `Startup self-check: ... telegram_desktop_polling=no backend_only_refresh=yes backend_emergency_google_fallback=no ...`;
  - refresh: `Refresh diagnostic summary: source=google_emergency_fallback primary_source=google_emergency_fallback backend_only_refresh=True emergency_google_fallback=True ... google_mirror_pending_exports=4 ...`;
  - backend: `shadow_diagnostics.backend_active_orders_source=postgres_backend`, `google_mirror_status=degraded`, `hot_path_stale_processing=1`, `telegram_worker_state=requires_attention`.
- Rollback proof:
  - rollback flips/removes only env flags;
  - pending counts `pending_backend_events`, `pending_saves`, `pending_prints`, `pending_telegram` must be recorded before rollback and rechecked after restart;
  - допустимо только уменьшение count после успешного sync/audit, внезапное обнуление без evidence запрещено.
- Проверено:
  - `.venv/bin/python -m unittest tests.test_startup_check tests.test_desktop_diagnostics` - 4 tests OK;
  - `.venv/bin/python -m unittest tests.test_refresh_fallback` - 16 tests OK;
  - `.venv/bin/python -m unittest tests.test_backend_api_persistence.BackendApiPersistenceTests.test_admin_operations_summarizes_attention_without_raw_payload_or_telegram_spam` - OK;
  - `.venv/bin/python -m unittest tests.test_refresh_fallback tests.test_backend_api_persistence tests.test_backend_telegram_import` - 190 tests OK;
  - `.venv/bin/python -m compileall src/taksklad backend/app` - OK;
  - `git diff --check` - OK.

### Backend-only hot path Phase 6 operations attention summary

- Причина: операторам и поддержке нужен короткий ответ "что требует внимания", без чтения сырого лога и без смешивания hot-path проблем с mirror-only lag.
- Изменено:
  - добавлен backend endpoint `/api/v1/admin/operations`;
  - endpoint возвращает `status`, `summary`, `items`, `readiness_status`, `google_mirror_status`, `telegram_summary`;
  - категории: `google_mirror`, `telegram`, `skladbot`, `queue`, `incident`, `import`;
  - каждый item содержит `impact`, `severity`, `count`, `oldest_age_seconds`, `next_action`, sanitized `details`;
  - web-admin диагностика показывает карточку `Требует внимания` и список next actions;
  - `telegram_summary` только возвращается текстом, автоматически в чат не отправляется.
- Sanitized sample:
  - API: `status=requires_attention`, `summary.hot_path=3`, `summary.mirror=1`, `items[].next_action=...`;
  - Telegram text: `TakSklad: требуется внимание` + краткие строки по категориям; тест подтверждает, что новых `telegram_notification` events endpoint не создаёт.
- Инварианты:
  - raw payload, chat_id, токены, Authorization, полные КИЗы и клиентские payload не выводятся;
  - mirror-only Google lag отделён от hot-path blockers;
  - UI использует существующие diagnostics styles с `overflow-wrap:anywhere`.
- Проверено:
  - `.venv/bin/python -m unittest tests.test_backend_api_persistence tests.test_backend_telegram_import` - 174 tests OK;
  - `npm --prefix frontend run build` - OK;
  - `.venv/bin/python -m compileall backend/app` - OK;
  - `git diff --check` - OK.

### Backend-only hot path Phase 5 Google mirror readiness split

- Причина: Google Sheets export является зеркалом, но readiness смешивал mirror-only ошибки с общей queue degradation. Из-за этого backend hot path мог выглядеть сломанным только из-за Google mirror lag/429.
- Изменено:
  - `/ready` и `/api/v1/readiness` получили отдельный блок `google_mirror`;
  - `google_mirror.role = mirror_export`, `event_type = google_sheets_export`;
  - в `google_mirror` выводятся `summary`, `oldest_pending_age_seconds`, `paused`, `next_attempt_at`, `last_errors`;
  - общий `status` больше не становится `degraded`, если сломан только Google mirror/export;
  - `queue` получила hot-path поля `hot_path_stale_processing_count` и `hot_path_last_errors`;
  - malformed `google_sheets_export` с invalid entity id не блокирует следующий валидный export.
- Sanitized readiness sample:
  - `status=ok`, `google_mirror.status=degraded`, `google_mirror.role=mirror_export`, `google_mirror.paused=true`, `queue.hot_path_last_errors=[]`.
- Инварианты:
  - DB commit и создание backend scan/order/import остаются независимыми от Google export;
  - Google 429 остаётся retryable mirror pause через `next_attempt_at`;
  - секреты и КИЗы в readiness редактируются через `redact_secrets`.
- Проверено:
  - `.venv/bin/python -m unittest tests.test_backend_api_persistence tests.test_reconciliation_service` - 112 tests OK;
  - `.venv/bin/python -m compileall backend/app` - OK;
  - `git diff --check` - OK.

### Backend-only hot path Phase 4 desktop outbox diagnostics

- Причина: desktop diagnostics показывал только общий `pending_backend_events`, без разреза по scan/order_complete, ошибочным событиям и попыткам. Для разбора timeout/idempotency это слишком грубо.
- Изменено:
  - `src/taksklad/desktop_diagnostics.py` добавляет агрегаты `pending_backend_scan_events`, `pending_backend_order_complete_events`, `pending_backend_other_events`, `pending_backend_failed_events`, `pending_backend_attempted_events`, `pending_backend_max_attempts`;
  - diagnostics по-прежнему не выводит payload, КИЗы, клиентов, адреса, `Authorization`, токены или raw errors;
  - `tests/test_desktop_diagnostics.py` проверяет новые агрегаты и отсутствие секретных/сырьевых значений в строке диагностики.
- Инварианты:
  - deterministic backend event ID и duplicate scan ack не менялись;
  - already-completed backend order complete остаётся acknowledged retry;
  - wrong-SKU и duplicate-other-order остаются blocked/visible;
  - print-before-complete и pending print queue checks сохранены.
- Sanitized diagnostic example:
  - `Refresh diagnostic summary: source=backend orders=12 groups=7 order_dates=2 known_codes=300 pending_saves=0 pending_prints=1 pending_backend_events=4 pending_backend_scan_events=2 pending_backend_order_complete_events=1 pending_backend_other_events=1 pending_backend_failed_events=2 pending_backend_attempted_events=3 pending_backend_max_attempts=3 pending_telegram=0 sync_synced=0 sync_failed=0 sync_remaining=0 backend_enabled=True backend_synced=1 backend_failed=1 backend_remaining=4 skladbot_enabled=True skladbot_matched=5 skladbot_not_found=0 skladbot_multiple=0 skladbot_errors=0`
- Проверено:
  - `.venv/bin/python -m unittest tests.test_desktop_ui_contract tests.test_backend_api_persistence tests.test_desktop_diagnostics` - 156 tests OK;
  - `.venv/bin/python -m compileall src/taksklad backend/app` - OK;
  - `git diff --check` - OK.

### Backend-only hot path Phase 3 backend-only refresh flag

- Причина: в backend-mode refresh скрыто падал обратно на Google Sheets при ошибке backend. Это сохраняло работоспособность, но возвращало Google в складской hot path и маскировало backend outage.
- Изменено:
  - добавлен флаг `TAKSKLAD_BACKEND_ONLY_REFRESH`, default `false`;
  - добавлен аварийный явный флаг `TAKSKLAD_BACKEND_EMERGENCY_GOOGLE_FALLBACK_ENABLED`, default `false`;
  - при `TAKSKLAD_BACKEND_ONLY_REFRESH=true` desktop не вызывает `get_today_orders()` после backend refresh failure, если emergency fallback не включён явно;
  - при явном emergency fallback source маркируется как `google_emergency_fallback`, а не обычный `google_fallback`;
  - error copy без кэша теперь говорит проверить связь с backend, а не Google Sheets;
  - Windows backend acceptance runbook получил оба новых env-флага.
- Инварианты:
  - default-поведение не меняет production без включения нового флага;
  - legacy non-backend mode продолжает читать Google;
  - при ошибке refresh с уже загруженными заказами текущая позиция сохраняется;
  - без загруженного списка scanning остаётся без текущей позиции и получает понятную backend connectivity ошибку.
- Проверено:
  - `.venv/bin/python -m unittest tests.test_refresh_fallback tests.test_desktop_ui_contract` - 67 tests OK;
  - `.venv/bin/python -m compileall src/taksklad backend/app` - OK;
  - `git diff --check` - OK.

### Backend-only hot path Phase 2 Telegram authority

- Причина: в backend-mode desktop polling уже выключен по умолчанию, но `on_close` всё ещё мог вызвать Google-backed `release_telegram_poll_lock()` даже если desktop не владел lock. Это оставляло лишний Google-запрос при закрытии клиента.
- Изменено:
  - `src/taksklad/app_runtime.py` освобождает Telegram lock только если desktop реально владеет свежим lock;
  - `src/taksklad/startup_check.py` пишет безопасный self-check флаг `telegram_desktop_polling=yes/no`, чтобы emergency fallback был виден в логах без токенов и chat IDs;
  - `tests/test_desktop_ui_contract.py` проверяет, что выключенный desktop polling не заходит в lock/state path и close без lock не трогает Google;
  - `tests/test_startup_check.py` проверяет self-check поле и отсутствие секретов в форматированном startup log.
- Инварианты:
  - штатный backend-mode Telegram listener остаётся `backend/app/telegram_worker.py`;
  - legacy desktop polling включается только явно через `TELEGRAM_DESKTOP_POLLING_ENABLED=true`;
  - Telegram admin gating и idempotency backend worker не менялись.
- Проверено:
  - `.venv/bin/python -m unittest tests.test_telegram_lock tests.test_backend_telegram_import tests.test_desktop_ui_contract tests.test_startup_check` - 129 tests OK;
  - `.venv/bin/python -m compileall src/taksklad backend/app` - OK;
  - `git diff --check` - OK.

### Web-admin календарь логистики и Smartup delivery_date policy

- Причина: Smartup-агенты могут ставить `delivery_date` на субботу, воскресенье или праздник, хотя логистика в эти дни не работает. Нужен управляемый календарь, чтобы не закреплять жесткое правило "пятница всегда на понедельник", а опираться на дату отгрузки из Smartup и календарь рабочих дней.
- Решение:
  - отдельная вкладка `Отчет` убрана из web-admin;
  - `Импорты`, `SkladBot dry-run`, `Инциденты`, `Активность` перенесены в нижнюю раскрывающую группу `История действий`;
  - добавлена вкладка `Календарь` с заказами, блоками, клиентами, выходными и ручными нерабочими днями;
  - добавлена таблица `logistics_calendar_days` и Alembic migration `20260626_0005_logistics_calendar`;
  - Smartup import сохраняет исходный `delivery_date`, effective-дата попадает в `Дата отгрузки`, а перенос фиксируется в audit/export metadata;
  - финальный отчет логистики пропускается для нерабочих дат.
- Инварианты:
  - источник даты остается Smartup `delivery_date`;
  - перенос идет только вперед до ближайшего рабочего дня;
  - суббота/воскресенье не являются единственным правилом, ручные праздники/нерабочие дни имеют приоритет;
  - write-действие календаря доступно только через admin write permission.
- Проверено:
  - `.venv/bin/python -m py_compile backend/app/logistics_calendar_service.py backend/app/smartup_auto_import.py backend/app/main.py backend/app/schemas.py tests/test_smartup_auto_import.py tests/test_backend_api_persistence.py tests/test_backend_skeleton.py` - OK;
  - `.venv/bin/python -m unittest tests.test_smartup_auto_import tests.test_backend_api_persistence.BackendApiPersistenceTests.test_admin_logistics_calendar_lists_orders_and_saves_non_working_day tests.test_backend_skeleton.BackendSkeletonTests.test_required_backend_files_exist tests.test_backend_skeleton.BackendSkeletonTests.test_initial_schema_contains_mvp_tables_and_constraints tests.test_backend_skeleton.BackendSkeletonTests.test_sql_bootstrap_and_alembic_migrations_keep_forward_only_contract` - OK;
  - `cd frontend && npm run build` - OK;
  - `docker compose -f deploy/vds/docker-compose.yml --env-file deploy/vds/.env.example config` - OK;
  - `git diff --check` - OK.

## 2026-06-25

### Telegram Excel import: stale waiting file and same-payload duplicates

- Симптом: бот показывал, что получил новый Excel, но после ввода даты ставил в очередь другой файл. По факту дата могла привязаться к старому `waiting_shipment_date` событию этого же чата.
- Причина:
  - выбор ожидающего Excel шел по старейшему pending event, а не по последнему загруженному файлу;
  - старые ожидания даты не закрывались при отправке нового Excel в том же чате;
  - `create_import()` не отсекал повторные строки внутри одного payload до создания backend-позиции и постановки строки в Google export queue.
- Решение:
  - новый Excel в чате переводит старые `waiting_shipment_date` / `waiting_date_choice` события этого чата в `cancelled`;
  - ручной ввод даты берет последний ожидающий Excel этого чата;
  - повторные строки внутри одного import payload пропускаются по `ID импорта` или `item_key` до создания позиции и до Google export queue.
- Проверено:
  - `.venv/bin/python -m unittest tests.test_backend_telegram_import` - 65 tests OK;
  - `.venv/bin/python -m unittest tests.test_backend_api_persistence` - 97 tests OK;
  - `.venv/bin/python -m py_compile backend/app/telegram_worker.py backend/app/imports_service.py tests/test_backend_telegram_import.py tests/test_backend_api_persistence.py` - OK.

## 2026-06-24

### Emergency pause forced desktop auto-update

- Симптом: рабочие Windows-клиенты после публикации forced `2.0.23` попадали в состояние `Требуется обновить приложение перед работой`, не могли пикать заказы, а автообновление не завершалось.
- Причина:
  - публичный `version.json` был `latest_version=2.0.23`, `min_supported_version=2.0.23`, `mandatory=true`;
  - старые клиенты считали это hard-block через `below_min_version`;
  - lock включался до доказанной успешной установки, поэтому отказ, cooldown или падение updater оставляли склад без рабочего приложения.
- Срочное решение:
  - `version.json` переведен в paused rollout: `latest_version=1.1.7`, `min_supported_version=1.1.7`, `mandatory=false`;
  - download URL/SHA очищены, чтобы случайный запуск обновления не стартовал сломанную установку;
  - `mandatory=true` в desktop updater больше не блокирует workflow сам по себе;
  - hard-block теперь возможен только при отдельном явном флаге manifest `block_workflow=true`.
- Release guards:
  - `release_preflight`, VDS acceptance и Windows test helper принимают только два явных состояния manifest: paused `1.1.7` или forced `2.0.23`.
- Проверено:
  - `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest tests.test_app_updates` - 13 tests OK;
  - `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:. .venv/bin/python -m unittest tests.test_update_service tests.test_release_preflight tests.test_vds_acceptance_scripts tests.test_windows_test_build_helper` - 25 tests OK;
  - `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:. .venv/bin/python -m compileall -q src/taksklad/app_updates.py src/taksklad/update_service.py tools/release_preflight.py` - OK;
  - `git diff --check -- version.json src/taksklad/app_updates.py tests/test_app_updates.py src/taksklad/update_service.py tests/test_update_service.py tools/release_preflight.py tests/test_release_preflight.py deploy/vds/acceptance_status.sh tests/test_vds_acceptance_scripts.py tools/build_windows_test_archive.ps1 tests/test_windows_test_build_helper.py` - OK.

### WEB daily top metrics

- Причина: верхние KPI web-панели показывали общие цифры по базе, а оператору нужна краткая информация за день.
- Решение:
  - верхняя строка теперь подписана как `Информация за день`;
  - метрики берутся из уже существующего `DayReport`, а не из частично загруженной admin-таблицы;
  - показываются `Акт. заказы`, `Отскан. блоков`, `Всего блоков`, `Всего заказов`;
  - дата рядом с заголовком соответствует `report_date`.
- Проверено:
  - `npm --prefix frontend run build` - OK;
  - `git diff --check -- frontend/src/App.tsx frontend/src/styles.css docs/implementation-log.md` - OK.

### WEB client points order history drilldown

- Причина: в панели `Клиенты и таймслоты` число в колонке `Заказы` показывало только общий счетчик и последнюю дату, но менеджеру нужно быстро открыть юрлицо и увидеть даты отгрузок, сколько было позиций и какие товары.
- Решение:
  - добавлен read-only endpoint `GET /api/v1/admin/client-points/order-summary`;
  - endpoint группирует заказы по нормализованному `client_name`, затем по дате отгрузки и товару;
  - общий список `GET /api/v1/admin/client-points` остался легким и не тянет товарную историю на все 600+ юрлиц;
  - web UI лениво грузит историю только по клику на число заказов конкретного юрлица;
  - в раскрытой строке показаны дата, количество заказов, количество позиций, товары, блоки и штуки.
- Ограничения:
  - endpoint не раскрывает КИЗы, scan codes, raw payload и внутренние order id;
  - группировка идет по юрлицу, а не по адресу, потому текущий контракт точек хранит таймслот за `client_name`.
- Проверено:
  - `.venv/bin/python -m unittest tests.test_backend_api_persistence.BackendApiPersistenceTests.test_admin_client_point_order_summary_groups_dates_and_products tests.test_backend_api_persistence.BackendApiPersistenceTests.test_admin_client_points_lists_order_points_and_updates_timeslot` - 2 tests OK;
  - `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m compileall -q backend/app` - OK;
  - `npm --prefix frontend run build` - OK;
  - `git diff --check -- backend/app/client_points_service.py backend/app/schemas.py backend/app/main.py frontend/src/api.ts frontend/src/App.tsx frontend/src/styles.css tests/test_backend_api_persistence.py` - OK.

### Hotfix 2.0.23: Green короб и автообновление

- Симптом 1: короб Green OP с кодом `010400639610445821...` отклонялся как wrong-SKU/не распознанный, хотя позиция была `Chapman Green OP 20`.
- Причина 1: в mapping коробов был Green GTIN `0104006396104448`, но новые этикетки FALCON пришли с коробочным GTIN `0104006396104458`.
- Решение 1:
  - новый GTIN `0104006396104458` добавлен в desktop/backend mapping как `green:op`;
  - старый GTIN оставлен;
  - добавлены regression tests на живой Green-код из FALCON.
- Симптом 2: после неудачной принятой попытки обязательного автообновления повторный запуск старого exe попадал в cooldown-блок и склад был вынужден вручную ставить архив с GitHub.
- Решение 2:
  - если последняя попытка обязательного обновления была принята пользователем (`last_user_action=accepted`), cooldown больше не блокирует повторную установку той же версии;
  - mandatory lock теперь включается только для реально устаревшей версии, а package-only переход на onedir при той же версии не блокирует сканирование;
  - отказ пользователя от mandatory update и неизвестное старое состояние по-прежнему блокируют работу старой версии, чтобы не сканировать устаревшим exe.
- Проверено:
  - `.venv/bin/python -m unittest tests.test_app_updates tests.test_update_service` - 14 tests OK;
  - `.venv/bin/python -m unittest tests.test_scan_quantities tests.test_backend_api_persistence.BackendApiPersistenceTests.test_scan_create_accepts_live_green_aggregate_box_gtin tests.test_backend_api_persistence.BackendApiPersistenceTests.test_scan_create_accepts_aggregate_box_when_next_ai_is_not_serial tests.test_backend_api_persistence.BackendApiPersistenceTests.test_scan_create_rejects_aggregate_box_for_wrong_product` - 12 tests OK;
  - `.venv/bin/python -m unittest tests.test_release_preflight tests.test_windows_test_build_helper tests.test_vds_acceptance_scripts` - 19 tests OK.
- Релиз/deploy:
  - GitHub release `v2.0.23` опубликован; workflow `28090484689` собрал `TakSklad.exe` и `TakSklad-windows-x64.zip`;
  - public `version.json` на GitHub показывает `latest_version=min_supported_version=2.0.23`, `mandatory=true`, `package_type=onefile_exe`;
  - SHA `TakSklad.exe`: `72740494cf7342624e98a1cb4d19130882cd346fe9b363840db11f84f3b6e7d7`;
  - SHA `TakSklad-windows-x64.zip`: `e2ab0dc3ad46ab203161210389508543451cb3f42cf9d3b658af3373df7e998a`;
  - перед VDS deploy создан restore point `/opt/taksklad/restore_points/pre-2023-green-box-updater-20260624T100252Z`;
  - создан Postgres backup `/opt/taksklad/backups/postgres/taksklad-postgres-20260624T100252Z.sql.gz`;
  - на VDS синхронизированы только hotfix/runtime файлы и пересобран `backend-api`;
  - container smoke: `APP_VERSION=2.0.23`, live Green-код распознан как `green:op`, `block_quantity=50`;
  - `https://api.taksklad.uz/health` - OK, `version=2.0.23`;
  - `/ready` остается `degraded` из-за старого failed `telegram_excel_import`, не из-за hotfix;
  - `./deploy/vds/acceptance_status.sh` подтвердил `version_json`, Google/backend sync и SkladBot coverage OK, общий status `failed` только из-за known readiness/manual GO-NO-GO.

### Aggregate box scan hotfix for new Chapman SKU

- Симптом: в приложении не пикаются короба новых SKU, хотя короб должен закрывать `50` блоков.
- Причина: короб распознавался по префиксу `01 + GTIN короба + 21`. Для новых этикеток после GTIN короба может идти другой GS1 AI, например `10` или `17`, поэтому код не классифицировался как `aggregate_box` и дальше отклонялся как неизвестный/wrong-SKU для известной Chapman-позиции.
- Решение:
  - desktop `src/taksklad/scan_quantities.py` распознает короб по `01 + GTIN короба`;
  - backend `backend/app/scan_quantities.py` использует тот же mapping;
  - добавлен тест синхронности desktop/backend mapping;
  - добавлен backend regression test, где Brown SSL короб после GTIN содержит `10...21...` и все равно засчитывается как `+50`.
- Инварианты:
  - короб по-прежнему засчитывается только как `50` блоков;
  - wrong-SKU остается заблокирован;
  - короб больше остатка позиции остается заблокирован;
  - обычные unit-КИЗы продолжают распознаваться по unit GTIN.
- Проверено:
  - `.venv/bin/python -m unittest tests.test_scan_quantities` - 9 tests OK;
  - `.venv/bin/python -m unittest tests.test_backend_api_persistence.BackendApiPersistenceTests.test_scan_create_counts_aggregate_box_as_fifty_blocks tests.test_backend_api_persistence.BackendApiPersistenceTests.test_scan_create_accepts_aggregate_box_when_next_ai_is_not_serial tests.test_backend_api_persistence.BackendApiPersistenceTests.test_scan_create_rejects_aggregate_box_when_remaining_blocks_are_less_than_fifty tests.test_backend_api_persistence.BackendApiPersistenceTests.test_scan_create_rejects_aggregate_box_for_wrong_product tests.test_backend_api_persistence.BackendApiPersistenceTests.test_scan_undo_subtracts_aggregate_box_block_quantity tests.test_backend_api_persistence.BackendApiPersistenceTests.test_scan_create_rejects_unit_kiz_for_wrong_chapman_product` - 15 tests OK;
  - `.venv/bin/python -m unittest tests.test_backend_api_persistence` - 94 tests OK;
  - `.venv/bin/python -m compileall -q src/taksklad backend/app tests/test_scan_quantities.py tests/test_backend_api_persistence.py` - OK.

## 2026-06-23

### Отдельная строка `Отгрузка в браке` в SkladBot daily report

- Симптом: заявка SkladBot типа `Отгрузка в браке` попадала в общую строку `Отгрузка`, поэтому в рабочем Telegram-уведомлении брак не был виден без открытия XLSX.
- Причина: `categorize_request_type()` классифицировал любой тип с подстрокой `отгруз` как обычную `Отгрузка`.
- Решение:
  - добавлена категория `Отгрузка в браке`;
  - проверка `брак + отгруз/расход` выполняется до общей отгрузки;
  - Telegram daily message выводит отдельную строку `Отгрузка в браке`;
  - XLSX-лист `Сводка` получил отдельную строку брака, а формула начального остатка учитывает ее как отрицательный расход;
  - обычная строка `Отгрузка` больше не включает такие заявки.
- Source of truth:
  - SkladBot API остается источником для daily report;
  - Google Sheets не используется.
- Проверено:
  - `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:. .venv/bin/python -m unittest tests.test_skladbot_daily_report` - 20 tests OK;
  - `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:. .venv/bin/python -m unittest tests.test_skladbot_daily_report tests.test_backend_telegram_import` - 84 tests OK;
  - `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:. .venv/bin/python -m compileall -q backend/app/skladbot_daily_report.py tests/test_skladbot_daily_report.py` - OK.
  - `git diff --check -- backend/app/skladbot_daily_report.py tests/test_skladbot_daily_report.py docs/report-source-rules.md docs/changelog.md docs/implementation-log.md` - OK.
- VDS deploy:
  - перед заменой создан restore point `/opt/taksklad/restore_points/pre-defect-shipment-daily-20260623T174716Z`;
  - создан Postgres backup `/opt/taksklad/backups/postgres/taksklad-postgres-20260623T174716Z.sql.gz`;
  - перед рестартом проверен антидубль: `pending_events` для `skladbot_daily_report:2026-06-23:*` имел `completed=1`;
  - синхронизирован только runtime-файл `backend/app/skladbot_daily_report.py`;
  - SHA локального и серверного файла совпал: `00cb0e42c16ebea649a304bb935da5e477c9199771e0a580e14346a1071f65c4`;
  - выполнен `docker compose --env-file deploy/vds/.env -f deploy/vds/docker-compose.yml up -d --build telegram-worker`;
  - compose также пересоздал `backend-api` как зависимость `telegram-worker`;
  - VDS `telegram-worker` compileall по `app/skladbot_daily_report.py` и `app/telegram_worker.py` - OK;
  - production smoke внутри `telegram-worker` подтвердил отдельные строки `Отгрузка` и `Отгрузка в браке` без Telegram-send;
  - `https://api.taksklad.uz/health` - OK, `version=2.0.21`;
  - `https://api.taksklad.uz/ready` - БД и миграции OK, общий статус `degraded` из-за старой failed `telegram_excel_import`;
  - VDS `./deploy/vds/acceptance_status.sh` - marker/google/skladbot/menu OK, общий `status=failed` только из-за `ready=degraded` и незакрытого GO/NO-GO чеклиста;
  - после рестарта anti-duplicate count остался `completed=1`, новой отправки daily report не появилось;
  - свежие логи `telegram-worker` и `backend-api` после деплоя - без `ERROR/Traceback/Exception/CRITICAL/failed` и без повторной отправки `SkladBot отчет`.
- Telegram:
  - реальные сообщения в рабочую группу не отправлялись;
  - проверка выполнена через unit-тесты с fake worker/client.

### WEB/LOG client identity for logistics points

- Уточнен контракт справочника точек:
  - `client_name` является названием точки, юрлицом и клиентом;
  - таймслот хранится за `client_name`;
  - адрес, координаты и ТП считаются изменяемыми деталями и обновляются из новых импортов.
- Backend:
  - `client_points_service` группирует derived/saved точки по нормализованному клиенту;
  - повторный импорт с тем же клиентом и новым адресом обновляет существующую точку, но не сбрасывает `delivery_from/delivery_to`;
  - логистический XLSX ищет таймслот по клиенту, поэтому изменение адреса не возвращает точку к `10:00-18:00`.
- Web UI:
  - поиск теперь явно подписан как поиск по текущим клиентам;
  - создание точки вынесено в отдельный раскрываемый блок по кнопке `Создать точку`;
  - отдельное поле `Название точки` убрано, потому название точки равно клиенту/юрлицу.
- Проверено:
  - `.venv/bin/python -m unittest tests.test_backend_api_persistence.BackendApiPersistenceTests.test_admin_client_points_lists_order_points_and_updates_timeslot tests.test_backend_api_persistence.BackendApiPersistenceTests.test_admin_client_points_use_client_identity_when_address_changes tests.test_backend_api_persistence.BackendApiPersistenceTests.test_import_updates_client_point_address_and_keeps_timeslot_by_client tests.test_backend_api_persistence.BackendApiPersistenceTests.test_logistics_report_uses_saved_client_point_timeslot tests.test_backend_api_persistence.BackendApiPersistenceTests.test_readiness_accepts_user_password_hash_schema_head_revision tests.test_backend_skeleton.BackendSkeletonTests.test_initial_schema_contains_mvp_tables_and_constraints tests.test_backend_skeleton.BackendSkeletonTests.test_sql_bootstrap_and_alembic_migrations_keep_forward_only_contract` - 7 tests OK;
  - `npm --prefix frontend run build` - OK.
- Production deploy:
  - деплой сделан targeted bundle из VDS remote-base, без соседних локальных изменений;
  - изменены только `backend/app/client_points_service.py`, `frontend/src/App.tsx`, `frontend/src/styles.css`;
  - VDS restore point: `/opt/taksklad/restore_points/pre-client-identity-timeslots-20260623T102546Z`;
  - VDS Postgres backup: `/opt/taksklad/backups/postgres/taksklad-postgres-20260623T102544Z.sql.gz`;
  - rebuilt services: `backend-api`, `frontend`;
  - Alembic current остается `20260623_0004`, новой миграции нет;
  - `https://api.taksklad.uz/health` - OK;
  - `https://api.taksklad.uz/ready` - migrations OK, общий статус остается degraded из-за старой очереди `telegram_excel_import`;
  - `https://taksklad.uz/` отдает frontend asset `index-U-lmCpOW.js` / `index-afgwTo0A.css`;
  - live rollback smoke подтвердил: один тестовый `client_points` по клиенту, адрес обновляется, слот `08:31-09:32` попадает в логистический XLSX, после rollback тестовые `client_points/orders` не сохранены.

### WEB/RBAC logistics slots limited user

- Добавлена DB-backed auth-модель поверх существующего env-admin:
  - `users.password_hash` добавляется миграцией `20260623_0004_user_password_hash`;
  - env login остается `admin`;
  - DB user с ролью `logistics_slots` получает permission `client_points:write`.
- Session payload теперь возвращает web UI поля `role` и `permissions`.
- Backend RBAC:
  - read endpoints остаются за обычной auth/session защитой;
  - warehouse/admin write endpoints требуют `admin:write`;
  - `POST /api/v1/admin/client-points/timeslot` требует `client_points:write`;
  - `GET /api/v1/reports/reconciliation/day` закрыт как write-like endpoint, потому что создает audit/incidents.
- Frontend proxy:
  - `/api/` больше не подставляет `Bearer ${TAKSKLAD_API_TOKEN}` после `auth_request`;
  - browser-запросы доходят до backend с cookie, поэтому role checks применяются к реальному web-пользователю;
  - service token остается для прямых backend/service вызовов, не для web proxy.
- Web UI:
  - для non-admin скрыты кнопки Google queue, Google/SkladBot sync, order actions, dry-run rebuild, incident retry/resolve/ignore и audit-log download;
  - вкладка `Клиенты` остается доступной для role `logistics_slots`;
  - добавлен сброс кастомного таймслота до `10:00-18:00`, что является безопасным удалением индивидуального времени без удаления точки.
- Проверено:
  - `.venv/bin/python -m unittest tests.test_backend_api_persistence.BackendApiPersistenceTests.test_web_auth_login_sets_cookie_and_check_accepts_session tests.test_backend_api_persistence.BackendApiPersistenceTests.test_web_auth_session_allows_admin_api_without_service_token tests.test_backend_api_persistence.BackendApiPersistenceTests.test_web_auth_configured_without_service_token_still_requires_session tests.test_backend_api_persistence.BackendApiPersistenceTests.test_logistics_slots_user_can_write_only_client_point_timeslots tests.test_backend_api_persistence.BackendApiPersistenceTests.test_admin_client_points_lists_order_points_and_updates_timeslot tests.test_backend_api_persistence.BackendApiPersistenceTests.test_admin_client_points_rejects_invalid_timeslot_order` - OK;
  - `.venv/bin/python -m unittest tests.test_backend_api_persistence` - 91 tests OK;
  - `.venv/bin/python -m unittest tests.test_vds_acceptance_scripts.VdsAcceptanceScriptsTests.test_frontend_uses_same_origin_api_proxy_contract` - OK;
  - `npm --prefix frontend run build` - OK.
- Production deploy:
  - деплой сделан targeted bundle из VDS remote-base, без локальных незадеплоенных фич;
  - VDS restore point: `/opt/taksklad/restore_points/pre-web-rbac-logistics-user-20260623T092150Z`;
  - VDS Postgres backup: `/opt/taksklad/backups/postgres/taksklad-postgres-20260623T092149Z.sql.gz`;
  - rebuilt services: `backend-api`, `frontend`;
  - Alembic current после deploy: `20260623_0004`;
  - `https://api.taksklad.uz/health` - OK;
  - `https://api.taksklad.uz/ready` - migrations OK, общий статус остается degraded из-за старой очереди `telegram_excel_import`, не из-за RBAC-миграции;
  - `https://taksklad.uz/` отдает новый frontend asset `index-BvJQTIug.js` / `index-hs_2yKGM.css`;
  - live RBAC smoke через same-origin `/api`: `998933456753` получает `role=logistics_slots`, `permissions=["client_points:write"]`, `GET /api/v1/admin/table` - 200, admin-write endpoints - 403, `POST /api/v1/admin/client-points/timeslot` проходит auth и на пустом payload возвращает validation `422`.

### WEB/LOG client points and logistics time slots

- Добавлен минимальный справочник точек клиентов без переписывания `orders`:
  - `client_points` хранит `client_name`, `address`, optional point metadata и `delivery_from/delivery_to`;
  - уникальный ключ: нормализованные `client_name + address`;
  - default window сохранен как `10:00-18:00`.
- Backend API:
  - `GET /api/v1/admin/client-points` возвращает сохраненные точки и derived-точки из уже существующих заказов;
  - `POST /api/v1/admin/client-points/timeslot` создает/обновляет точку и пишет audit `client_point_timeslot_updated`.
- Импорт новых Excel-заказов теперь синхронизирует точку в `client_points`, но не меняет order key и не влияет на дедупликацию заказов.
- `backend/app/logistics_service.py` больше не хардкодит `10:00/18:00` для всех: если по `client + address` есть активная сохраненная точка, XLSX получает ее `Доставка С/ПО`; иначе остается fallback.
- Web UI:
  - добавлена вкладка `Клиенты`;
  - добавлена ручная форма создания точки с юрлицом, адресом, названием точки, ТП, координатами и окном доставки;
  - поиск по юрлицу/точке/адресу/ТП;
  - фильтр `Уникальный слот` / `По умолчанию 10-18`;
  - inline-редактирование таймслотов.
- Migration/readiness:
  - добавлена forward-only миграция `20260623_0003_client_points`;
  - `/ready` ожидает новый head `20260623_0003`, чтобы код без примененной таблицы не считался здоровым.
- Проверено:
  - `.venv/bin/python -m py_compile backend/app/client_points_service.py backend/app/models.py backend/app/schemas.py backend/app/main.py backend/app/imports_service.py backend/app/logistics_service.py backend/app/health_service.py tests/test_backend_api_persistence.py tests/test_backend_skeleton.py` - OK;
  - `.venv/bin/python -m unittest tests.test_backend_api_persistence tests.test_backend_skeleton` - 99 tests OK;
  - `npm run build` в `frontend/` - OK;
  - `git diff --check` - OK;
  - локальный demo smoke: SQLite backend на `127.0.0.1:8008`, Vite proxy на `127.0.0.1:5173`, `/api/v1/auth/session` отвечает через frontend proxy.
- Production deploy:
  - деплой сделан targeted bundle из remote-base, без соседних локальных изменений `delete-active`, pagination и import-preview;
  - VDS restore point: `/opt/taksklad/restore_points/pre-client-points-timeslots-20260623T082754Z`;
  - VDS Postgres backup: `/opt/taksklad/backups/postgres/taksklad-postgres-20260623T082754Z.sql.gz`;
  - rebuilt services: `backend-api`, `frontend`;
  - Alembic current после deploy: `20260623_0003 (head)`;
  - `https://api.taksklad.uz/health` - OK;
  - `https://api.taksklad.uz/ready` - migrations OK, общий статус остается degraded из-за старой очереди `telegram_excel_import`, не из-за этой миграции;
  - `GET /api/v1/admin/client-points?limit=3` внутри `backend-api` с service token - 200;
  - `https://taksklad.uz/` отдает новый frontend asset `index-BV8dEOlv.js` / `index-hs_2yKGM.css`;
  - logistics smoke в rollback-транзакции подтвердил подстановку `08:31-09:32` в XLSX и отсутствие сохраненной тестовой записи после rollback.
- Осталось после deploy:
  - manual browser smoke вкладки `Клиенты` после входа в web UI.

## 2026-06-22

### WEB-03 same-origin API proxy contract

- По `WEB-03/GAP-026` проверен web reports/activity diagnostics transport contract:
  - frontend `defaultApiUrl()` возвращает пустую базу и ходит в relative `/api/v1/*`;
  - `frontend/nginx.conf.template` проксирует `/api/` в backend container;
  - `/api/` защищен `auth_request` и внутренним Bearer service token;
  - CSP держит `connect-src 'self'`;
  - локальная Vite-разработка использует отдельный явный proxy env `VITE_TAKSKLAD_DEV_API_URL`.
- Runtime-код не менялся: текущая реализация уже соответствовала contract, но canonical register держал gap как `needs_validation`.
- Исправлено:
  - `tests/test_vds_acceptance_scripts.py` добавил регрессионный тест `test_frontend_uses_same_origin_api_proxy_contract`;
  - `docs/taksklad-feature-user-stories.xlsx` перевел `GAP-026` в `fixed_retested`;
  - manual browser smoke для `WEB-03` оставлен `pending`, потому реальный web UI/VDS экран не проходил в этом цикле.
- Проверено:
  - `.venv/bin/python -m unittest tests.test_vds_acceptance_scripts.VdsAcceptanceScriptsTests.test_frontend_uses_same_origin_api_proxy_contract` - 1 test OK.

### API-09 configurable SkladBot SKU mapping

- По `API-09/GAP-029` проверен SkladBot dry-run/create flow:
  - default режим `SKLADBOT_CREATE_REQUESTS_MODE=dry_run` не делает live POST;
  - `enabled` ставит durable `skladbot_request_create` events только для `ready` dry-runs;
  - unknown SKU и zero blocks блокируют dry-run без падения import;
  - текущий Chapman mapping покрывает 6 SKU.
- Найден архитектурный риск: mapping был только hardcoded в `backend/app/skladbot_request_dry_run.py`, из-за чего новые/измененные SkladBot `product_data_id` нельзя было безопасно прокинуть через VDS config.
- Исправлено:
  - добавлен `SKLADBOT_SKU_MAPPING_JSON` как override/extension поверх текущего default mapping;
  - `load_sku_mapping()` валидирует `product_data_id`, `barcode`, `is_main_barcode`;
  - невалидный mapping блокирует dry-run заказа и не создает `skladbot_request_create`;
  - переменная добавлена в `deploy/vds/docker-compose.yml` и `deploy/vds/.env.example`;
  - docs описывают формат и safe failure.
- `GAP-029` закрыт как `fixed_retested` по локальному/config contract. Live SkladBot API/tokens acceptance остается pending.
- Проверено:
  - `.venv/bin/python -m unittest tests.test_backend_skladbot_request_dry_run tests.test_backend_skladbot_worker` - 76 tests OK;
  - `.venv/bin/python -m unittest tests.test_vds_acceptance_scripts tests.test_backend_skeleton` - 14 tests OK;
  - `.venv/bin/python -m py_compile backend/app/skladbot_request_dry_run.py tests/test_backend_skladbot_request_dry_run.py tests/test_vds_acceptance_scripts.py` - OK.

### API-08 Google export retry cooldown

- По `API-08/GAP-028` проверен backend Google mirror/export pending flow:
  - Google `429` / `quota` оставляет событие в `pending`;
  - `payload.next_attempt_at` задает следующий момент retry;
  - готовые события очереди не должны блокироваться более старым событием на cooldown.
- Найден дефект: `select_pending_export_events()` записывал `next_attempt_at`, но не учитывал его при следующем выборе pending events. В итоге один и тот же Google export мог немедленно повториться после 429.
- Исправлено:
  - `backend/app/google_sheets_pending.py` фильтрует pending exports по `next_attempt_at`;
  - future retry events остаются в `pending`;
  - более новые ready events продолжают обрабатываться;
  - при отсутствии ready events `remaining` показывает количество pending/deferred exports.
- `GAP-028` закрыт как `fixed_retested`. Live Google credentials/quota и фактическая mirror drift acceptance остаются pending.
- Проверено:
  - `.venv/bin/python -m unittest tests.test_backend_google_sheets_pending.GoogleSheetsPendingLockTests.test_future_retry_after_event_does_not_block_newer_ready_event tests.test_backend_google_sheets_pending.GoogleSheetsPendingLockTests.test_rate_limit_keeps_event_pending_and_stops_batch tests.test_backend_google_sheets_pending.GoogleSheetsPendingLockTests.test_postgres_pending_selection_uses_skip_locked_row_lock tests.test_backend_google_sheets_pending.GoogleSheetsPendingLockTests.test_bad_event_does_not_block_newer_valid_event` - 4 tests OK;
  - `.venv/bin/python -m py_compile backend/app/google_sheets_pending.py tests/test_backend_google_sheets_pending.py` - OK.

### API-06 import Google export failure isolation

- По `API-06/GAP-023` проверен backend Excel import:
  - Postgres `ImportJob`, `Order`, `OrderItem` создаются до Google mirror/export;
  - Google export queue является post-commit side effect и не должен превращать успешный import в 500;
  - частичный сбой должен быть виден оператору через `google_sheets_status`, incident и audit.
- Найден дефект: если `queue_google_sheets_export()` падал после Postgres commit, backend-данные оставались, но API отдавал exception вместо `201` с понятным Google status.
- Исправлено:
  - `backend/app/imports_service.py` ловит сбой постановки import records в Google export queue;
  - возвращает `google_sheets_status=error` и `google_sheets_error`;
  - пишет incident `google_sheets_import_export` и audit `google_sheets_import_export_failed`;
  - не откатывает уже созданные Postgres orders/items.
- `GAP-023` закрыт как `fixed_retested`. Ручная проверка реального Excel import через desktop/Telegram и live Google credentials остается pending.
- Проверено:
  - `.venv/bin/python -m unittest tests.test_backend_api_persistence.BackendApiPersistenceTests.test_import_reports_google_queue_failure_without_rolling_back_backend_data tests.test_backend_api_persistence.BackendApiPersistenceTests.test_import_keeps_backend_data_when_google_sheets_export_fails tests.test_backend_api_persistence.BackendApiPersistenceTests.test_duplicate_backend_import_still_can_backfill_google_sheets tests.test_backend_api_persistence.BackendApiPersistenceTests.test_import_preview_reports_duplicates_invalid_rows_and_does_not_write` - 4 tests OK;
  - `.venv/bin/python -m py_compile backend/app/imports_service.py tests/test_backend_api_persistence.py` - OK.

### API-04 scan Google export local lock coverage

- По `API-04/GAP-021` проверен backend scan/undo контракт:
  - scan API пишет `scan_codes`, audit и durable Google export event;
  - Google export queue failure не блокирует успешный scan;
  - Google pending export на PostgreSQL использует row locking через `FOR UPDATE SKIP LOCKED`;
  - non-Postgres/local path защищен process-local lock и возвращает `busy`, не трогая события, если обработчик уже запущен.
- Runtime-код не менялся: существующая реализация `backend/app/google_sheets_pending.py` уже использовала `LOCAL_EXPORT_LOCK`, но это не было закреплено отдельным тестом в canonical register.
- Исправлено:
  - `tests/test_backend_google_sheets_pending.py`: добавлен контракт `test_non_postgres_export_lock_returns_busy_without_processing`;
  - `docs/taksklad-feature-user-stories.xlsx`: `GAP-021` переведен в `fixed_retested`, manual operator/UI acceptance оставлен `pending`;
  - `docs/event-queue-lifecycle.md`: уточнен local/SQLite lock для Google exports.
- Проверено:
  - `.venv/bin/python -m unittest tests.test_backend_google_sheets_pending.GoogleSheetsPendingLockTests.test_non_postgres_export_lock_returns_busy_without_processing tests.test_backend_google_sheets_pending.GoogleSheetsPendingLockTests.test_rate_limit_keeps_event_pending_and_stops_batch tests.test_backend_api_persistence.BackendApiPersistenceTests.test_scan_create_queues_google_sheets_export_when_google_is_down tests.test_backend_api_persistence.BackendApiPersistenceTests.test_scan_create_exports_scan_state_to_google_sheets_best_effort` - 4 tests OK;
  - `.venv/bin/python -m py_compile backend/app/google_sheets_pending.py tests/test_backend_google_sheets_pending.py tests/test_backend_api_persistence.py` - OK.

### API-01 empty service-token auth guard

- По `API-01/GAP-018` проверен auth guard для `/api/v1/*`.
- Подтвержден риск: прежняя логика считала любой запрос авторизованным, если `TAKSKLAD_API_TOKEN` пустой, даже когда web-auth уже настроен. Это могло открыть admin API без web-session в ошибочной конфигурации.
- Исправлено:
  - `backend/app/main.py`: Bearer token path валиден только при непустом configured service token;
  - если web-auth настроен, guard проверяет session cookie;
  - local no-auth fallback разрешен только когда не настроены ни service token, ни web-auth.
- Закреплено тестами:
  - настроенный service token без session требует Bearer или web login;
  - пустой service token + настроенная web-auth возвращает `401` до login;
  - после login session cookie допускает admin API;
  - пустой service token + пустая web-auth остается local no-auth режимом.
- `GAP-018` закрыт как `fixed_retested`. Browser login/session/CORS smoke на реальном frontend/proxy остается manual pending.
- Проверено:
  - `.venv/bin/python -m unittest tests.test_backend_api_persistence.BackendApiPersistenceTests.test_web_auth_login_sets_cookie_and_check_accepts_session tests.test_backend_api_persistence.BackendApiPersistenceTests.test_web_auth_session_allows_admin_api_without_service_token tests.test_backend_api_persistence.BackendApiPersistenceTests.test_web_auth_configured_without_service_token_still_requires_session tests.test_backend_api_persistence.BackendApiPersistenceTests.test_api_allows_local_no_auth_only_when_no_auth_is_configured tests.test_backend_cors` - 5 tests OK;
  - `.venv/bin/python -m py_compile backend/app/main.py tests/test_backend_api_persistence.py tests/test_backend_cors.py` - OK.

### WEB-02 admin event retry and payload redaction

- По `WEB-02/GAP-025` проверен backend-контракт центра incidents/events:
  - `/api/v1/admin/events` показывает retryable events, linked order/import/entity fields и redacted payload;
  - `/api/v1/admin/events/{event_id}` возвращает тот же redacted view для детали;
  - retry требует `reason`, переводит событие в `pending`, очищает `last_error` и пишет audit;
  - completed/state events не retryable;
  - `telegram_excel_import` нельзя поставить на retry, если в payload нет исходного `document.file_id`.
- Runtime-код в этом цикле не менялся: существующая реализация уже соответствовала безопасному контракту, но canonical spreadsheet ещё держал `GAP-025` как `needs_validation`.
- Обновлено:
  - `docs/taksklad-feature-user-stories.xlsx`: `GAP-025` -> `fixed_retested`, `WEB-02` manual browser acceptance оставлен `pending`;
  - `docs/event-queue-lifecycle.md`: добавлено правило manual retry и redaction;
  - `docs/changelog.md` и этот журнал получили evidence.
- Проверено:
  - `.venv/bin/python -m unittest tests.test_backend_api_persistence.BackendApiPersistenceTests.test_admin_event_detail_retry_redacts_payload_and_writes_audit tests.test_backend_api_persistence.BackendApiPersistenceTests.test_admin_event_retry_rejects_telegram_import_when_original_file_is_unavailable` - 2 tests OK;
  - `.venv/bin/python -m py_compile backend/app/event_queue_service.py tests/test_backend_api_persistence.py` - OK.

### TS-TG-002 invalid Telegram notification queue handling

- По `TS-TG-002/GAP-034` проверен worker queue flow:
  - `getUpdates` 409 не останавливает scheduled imports/notifications/daily reports;
  - stale `telegram_notification` в `processing` сбрасывается в `pending`;
  - валидное queued notification отправляется и завершается `completed`.
- Найден локальный UX/ops gap: `telegram_notification` без текста или без получателя попадал в `failed`, хотя повтор не мог исправить payload. В admin events это выглядело как временный retryable сбой.
- Исправлено:
  - `backend/app/telegram_worker.py` переводит notification без `text` или без target chat в `blocked`;
  - для таких событий пишется audit `telegram_notification_blocked`;
  - реальные исключения отправки Telegram остаются `failed`, чтобы их можно было retry после восстановления внешнего канала.
- `GAP-034` закрыт как `fixed_retested` по локальной части. Ручная проверка real allowed/admin chats и доставки уведомлений остается pending.
- Проверено:
  - `.venv/bin/python -m unittest tests.test_backend_telegram_import.BackendTelegramImportTests.test_telegram_worker_sends_pending_notification_event tests.test_backend_telegram_import.BackendTelegramImportTests.test_telegram_worker_blocks_invalid_notification_events_without_retry tests.test_backend_telegram_import.BackendTelegramImportTests.test_telegram_worker_resets_stale_processing_notification_before_processing tests.test_backend_telegram_import.BackendTelegramImportTests.test_telegram_worker_runs_scheduled_jobs_after_getupdates_conflict` - 4 tests OK;
  - `.venv/bin/python -m py_compile backend/app/telegram_worker.py tests/test_backend_telegram_import.py` - OK.

### DESK-14 return totals display for legacy Google rows

- По `DESK-14/GAP-014` проверен локальный UX возвратов:
  - backend mode отправляет `confirmed_items` в backend;
  - backend list/lookup не читает Google fallback;
  - Google-only order без `_backend_order_id` отклоняется в backend mode;
  - legacy Google fallback сохранен только вне backend mode.
- Найден UX-дефект отображения: окно `Возвраты` считало общий блок/сумму только по backend-полям `quantity_blocks` и `line_total`. Для старых Google fallback-заявок состав мог быть валидным, но оператор видел `0 блоков` и `0 сум`.
- Исправлено:
  - `src/taksklad/app_returns.py` добавил helper'ы `return_item_blocks()`, `return_item_line_total()`, `return_order_total_blocks()`, `return_order_total_price()`;
  - окно найденного возврата и список последних возвратов используют эти helper'ы;
  - backend и legacy Google item shapes оба покрыты тестом.
- Бизнес-правила не менялись: возврат в backend по-прежнему требует строгие `confirmed_items`; успешный return пишет movement `return`, неуспешный return не освобождает КИЗ.
- Проверено:
  - `.venv/bin/python -m unittest tests.test_desktop_ui_contract.DesktopUiContractTests.test_return_mark_sends_confirmed_items_to_backend tests.test_desktop_ui_contract.DesktopUiContractTests.test_backend_returns_list_reads_backend_without_google_fallback tests.test_desktop_ui_contract.DesktopUiContractTests.test_backend_return_lookup_reads_backend_without_google_fallback tests.test_desktop_ui_contract.DesktopUiContractTests.test_backend_return_rejects_google_order_without_backend_id tests.test_desktop_ui_contract.DesktopUiContractTests.test_backend_return_uses_backend_id_for_google_order tests.test_desktop_ui_contract.DesktopUiContractTests.test_legacy_return_keeps_google_write_fallback_for_google_order tests.test_desktop_ui_contract.DesktopUiContractTests.test_return_confirmed_items_are_built_from_backend_items tests.test_desktop_ui_contract.DesktopUiContractTests.test_return_totals_support_backend_and_google_item_shapes tests.test_backend_api_persistence.BackendApiPersistenceTests.test_return_lookup_and_mark_returned_excludes_order_from_active_list tests.test_backend_api_persistence.BackendApiPersistenceTests.test_return_releases_kiz_for_new_outbound_scan_with_history tests.test_backend_api_persistence.BackendApiPersistenceTests.test_failed_return_does_not_release_kiz_for_new_order tests.test_backend_api_persistence.BackendApiPersistenceTests.test_mark_return_exports_archive_and_returns_to_google_sheets_best_effort tests.test_backend_api_persistence.BackendApiPersistenceTests.test_mark_return_rejects_mismatched_confirmed_items_without_side_effects` - 13 tests OK;
  - `.venv/bin/python -m py_compile src/taksklad/app_returns.py tests/test_desktop_ui_contract.py backend/app/orders_service.py tests/test_backend_api_persistence.py` - OK.

### DESK-16 day-end Telegram result clarity

- По `DESK-16/GAP-016` проверен локальный закрывающий сценарий смены:
  - отчет строится из scan backup;
  - undo учитывается;
  - сменные XLSX делятся по датам отгрузки и частям;
  - итоговое окно показывает результат отправки каждого файла.
- Найден UX gap: при `queued` или `failed` оператор видел только общий статус `в очереди Telegram` / `не отправлен`, но не видел причину. При внешней проблеме Telegram это оставляло неясность: отчет сохранен, не настроен бот, упала сеть или файл поставлен в очередь.
- Исправлено:
  - `src/taksklad/app_day_end.py` добавил `format_day_end_telegram_status()` и `format_day_end_report_line()`;
  - для `queued` и `failed` итог закрытия смены показывает причину Telegram-результата;
  - длинные причины обрезаются, чтобы не раздувать UI;
  - `sent` остается коротким статусом без технического сообщения.
- `GAP-016` не закрыт полностью: реальная доставка Telegram, chat permissions, сеть и retry `pending_telegram` остаются ручной validation.
- Проверено:
  - `.venv/bin/python -m unittest tests.test_daily_report tests.test_desktop_ui_contract` - 50 tests OK;
  - `.venv/bin/python -m py_compile src/taksklad/app_day_end.py src/taksklad/reports.py src/taksklad/telegram_service.py tests/test_daily_report.py` - OK.

### DESK-15 updater launch recovery

- По `DESK-15/GAP-015` проверена локальная часть запуска installer:
  - успешный `.ps1` запускается через `powershell -NoProfile -ExecutionPolicy Bypass -File ...`;
  - после успешного `Popen` приложение закрывается, как раньше;
  - failure path не должен закрывать приложение и должен показывать recovery.
- Найден локальный UX/reliability gap: `run_update_installer()` не ловил ошибку `subprocess.Popen`. Если PowerShell/cmd не стартовал, ошибка могла выйти из Tkinter callback, а оператор не получал нормальный текст восстановления.
- Исправлено:
  - `src/taksklad/app_updates.py` ловит exception запуска installer;
  - показывает `show_critical_error()` с причиной, `TakSklad_update.log` и инструкцией не использовать старую версию для сканирования;
  - возвращает `False` при ошибке и не вызывает `destroy()`;
  - возвращает `True` и закрывает приложение только после успешного запуска updater.
- `GAP-015` не закрыт полностью: фактический copy flow, замена файлов, shortcut/restart и работа опубликованного GitHub release updater остаются ручной Windows validation.
- Проверено:
  - `.venv/bin/python -m unittest tests.test_app_updates tests.test_update_service tests.test_windows_release_workflow tests.test_startup_check` - 14 tests OK;
  - `.venv/bin/python -m py_compile src/taksklad/app_updates.py tests/test_app_updates.py src/taksklad/update_service.py tests/test_update_service.py` - OK.

### DESK-13 desktop Excel backend preview and coordinates

- По `DESK-13/GAP-013` проверен desktop import flow:
  - file dialog показывает `.xlsx/.xlsm`;
  - preview собирает файлы, source rows, новые позиции, дубли, warnings/errors, geocoding counters;
  - commit идет в backend или legacy Google sheet.
- Найдены локальные дефекты:
  - при выборе неподдерживаемого файла через `All files` parser сразу передавал путь в `openpyxl`, из-за чего оператор получал низкоуровневую ошибку открытия файла вместо понятного ограничения формата;
  - в backend-mode desktop preview считал все parsed records новыми и принудительно ставил `duplicate_records=[]`, хотя backend затем отбрасывал дубли только после подтверждения;
  - desktop parser находил координаты в Excel, но не передавал поле `Координаты` в итоговый record для backend import, из-за чего логистика могла потерять маршрутные данные.
- Исправлено:
  - `src/taksklad/excel_import.py` проверяет расширение до `openpyxl.load_workbook`;
  - разрешены только расширения из `EXCEL_IMPORT_EXTENSIONS`: `.xlsx`, `.xlsm`;
  - неподдерживаемый файл добавляет понятную ошибку в preview и не создает records.
  - `backend/app/imports_service.py` добавил read-only preview import без записи в БД, Google queue и SkladBot dry-run;
  - `backend/app/main.py` открыл `POST /api/v1/imports/preview`;
  - `src/taksklad/app_imports.py` использует backend preview для расчета новых/дублей/invalid rows до подтверждения;
  - `src/taksklad/excel_import.py` сохраняет `Координаты` в record;
  - docs приведены к правилу: пустой адрес без координат = `Самовывоз со склада`, координаты сохраняются отдельно.
- `GAP-013` не закрыт полностью: полный Tkinter e2e, реальные `.xlsx/.xlsm`, backend/Google commit и внешний геокодинг остаются ручной acceptance на Windows.
- Проверено:
  - `.venv/bin/python -m unittest tests.test_excel_normalizer tests.test_app_imports tests.test_backend_bridge tests.test_backend_api_persistence.BackendApiPersistenceTests.test_import_preview_reports_duplicates_invalid_rows_and_does_not_write tests.test_backend_api_persistence.BackendApiPersistenceTests.test_import_stores_coordinates_blocks_and_prices tests.test_backend_api_persistence.BackendApiPersistenceTests.test_import_marks_missing_address_as_pickup` - 27 tests OK;
  - `.venv/bin/python -m py_compile backend/app/imports_service.py backend/app/main.py backend/app/schemas.py src/taksklad/backend_client.py src/taksklad/app_imports.py src/taksklad/excel_import.py tests/test_app_imports.py tests/test_excel_normalizer.py tests/test_backend_bridge.py tests/test_backend_api_persistence.py` - OK;
  - `.venv/bin/python -m unittest tests.test_backend_api_persistence tests.test_excel_normalizer tests.test_app_imports tests.test_backend_bridge tests.test_feature_user_stories_register tests.test_feature_acceptance_status` - 122 tests OK;
  - `.venv/bin/python tools/feature_acceptance_status.py` - `status=ok`, `manual_complete=false`, `no_open_errors=false`;
  - `.venv/bin/python -m unittest discover -s tests` - 558 tests OK;
  - `.venv/bin/python -m py_compile main.py sitecustomize.py taksklad/__init__.py src/taksklad/*.py tests/*.py backend/app/*.py tools/feature_acceptance_status.py tools/release_preflight.py` - OK;
  - `.venv/bin/python tools/release_preflight.py --skip-network` - `status=ok`;
  - `git diff --check` - OK.

### DESK-12 current print settings flow

- По `DESK-12/GAP-012` проверена локальная часть настроек и очереди печати:
  - поддержанные размеры `100x100`, `100x150`, `75x50`, `58x40`;
  - сохраненный принтер не заменяется первым найденным;
  - pending-сводки предлагаются при запуске;
  - Windows-путь проверяет `PrinterSettings.IsValid` и логирует stdout/stderr.
- Найден локальный UX-дефект: диалог позволял выбрать принтер/размер, но `print_summary()` затем заново читал настройки из storage. Если оператор не сохранял параметры или запись настроек не проходила, текущая печать могла уйти со старыми параметрами.
- Исправлено:
  - `src/taksklad/printing.py` принимает явные `print_settings` для текущей печати и нормализует их отдельно от сохраненного storage;
  - `src/taksklad/app_printing.py` сохраняет выбранные настройки в памяти приложения сразу после подтверждения диалога;
  - `src/taksklad/app_finish.py` и `src/taksklad/app_printing.py` передают выбранные настройки в `print_summary()` для finish и pending retry.
- `GAP-012` не закрыт полностью: физическая печать, Windows-драйвер, custom paper size, поля, масштаб и читаемость этикеток остаются ручной acceptance.
- Проверено:
  - `.venv/bin/python -m unittest tests.test_printing tests.test_pending_store tests.test_main_refactor_contract tests.test_desktop_ui_contract` - 62 tests OK;
  - `.venv/bin/python -m unittest discover -s tests` - 552 tests OK;
  - `.venv/bin/python -m py_compile src/taksklad/printing.py src/taksklad/app_printing.py src/taksklad/app_finish.py tests/test_printing.py tests/test_main_refactor_contract.py tests/test_desktop_ui_contract.py` - OK;
  - `.venv/bin/python -m py_compile main.py sitecustomize.py taksklad/__init__.py src/taksklad/*.py tests/*.py backend/app/*.py tools/feature_acceptance_status.py tools/release_preflight.py` - OK;
  - `.venv/bin/python -m unittest tests.test_feature_user_stories_register tests.test_feature_acceptance_status` - 14 tests OK;
  - `.venv/bin/python tools/feature_acceptance_status.py` - `status=ok`, `manual_complete=false`, `no_open_errors=false`;
  - `.venv/bin/python tools/release_preflight.py --skip-network` - `status=ok`;
  - `git diff --check` - OK.

### DESK-11 pending print queue safety

- По `DESK-11/GAP-011` проверен локальный finish/print flow:
  - `add_pending_print` вызывается до печати;
  - `print_summary` идет до backend complete и Google archive;
  - pending print удаляется после успешной печати;
  - физическая Windows-печать остается manual validation из-за зависимости от драйвера и принтера.
- Найден локальный reliability gap: код не проверял результат записи/удаления `pending_prints`. При сбое локального storage успешная печать могла продолжить backend complete/archive, оставив сводку в очереди и создавая риск повторной печати.
- Исправлено:
  - `src/taksklad/pending_store.py` теперь возвращает явный результат для `add_pending_print` и `remove_pending_print`;
  - `src/taksklad/app_finish.py` не печатает без подтвержденной pending-записи и не завершает заказ без подтвержденного удаления pending после печати;
  - `src/taksklad/app_printing.py` при ручной допечатке pending-сводок проверяет удаление из очереди и сообщает ошибку при сбое.
- `GAP-011` не закрыт полностью: нужна ручная Windows acceptance с реальным принтером, подтверждением физической печати, очистки pending queue и закрытия заказа только после успешной печати.
- Проверено:
  - `.venv/bin/python -m unittest tests.test_pending_store tests.test_printing tests.test_main_refactor_contract tests.test_desktop_ui_contract` - 60 tests OK;
  - `.venv/bin/python -m unittest discover -s tests` - 550 tests OK;
  - `.venv/bin/python -m py_compile src/taksklad/pending_store.py src/taksklad/app_finish.py src/taksklad/app_printing.py tests/test_pending_store.py tests/test_printing.py tests/test_desktop_ui_contract.py` - OK;
  - `.venv/bin/python -m py_compile main.py sitecustomize.py taksklad/__init__.py src/taksklad/*.py tests/*.py backend/app/*.py tools/feature_acceptance_status.py tools/release_preflight.py` - OK;
  - `.venv/bin/python -m unittest tests.test_feature_user_stories_register tests.test_feature_acceptance_status` - 14 tests OK;
  - `.venv/bin/python tools/feature_acceptance_status.py` - `status=ok`, `manual_complete=false`, `no_open_errors=false`;
  - `.venv/bin/python tools/release_preflight.py --skip-network` - `status=ok`;
  - `git diff --check` - OK.

### DESK-10 hard-error position action recovery

- По `DESK-10/GAP-010` найден подтвержденный локальный UX-дефект: при hard error во время сохранения последней позиции `next_product(finish_after_save=True)` оставлял оператора на текущей позиции, но включал кнопку `Следующая позиция`, хотя следующей позиции нет.
- Последствие дефекта: оператор мог видеть неправильное доступное действие после ошибки сохранения/finish retry, что выглядит как переход к несуществующей позиции.
- Исправлено:
  - `src/taksklad/app_scanning.py` теперь после ошибки восстанавливает кнопки по фактическому состоянию текущей позиции;
  - на последней полной позиции после ошибки `Следующая позиция` остается disabled, а `ЗАВЕРШИТЬ ЗАКАЗ` снова normal для повторной попытки;
  - на неполной позиции обе action-кнопки остаются disabled.
- `GAP-010` не закрыт полностью: локальная логика покрыта тестом, но видимый Windows UI сценарий сохранения/ошибки/повторной попытки остается в ручной acceptance.
- Проверено:
  - `.venv/bin/python -m unittest tests.test_desktop_ui_contract.DesktopUiContractTests.test_next_product_hard_error_keeps_final_position_actions_consistent` - OK;
  - `.venv/bin/python -m unittest tests.test_desktop_ui_contract tests.test_desktop_pending_store tests.test_backend_bridge` - 59 tests OK;
  - `.venv/bin/python -m unittest discover -s tests` - 546 tests OK;
  - `.venv/bin/python -m py_compile main.py sitecustomize.py taksklad/__init__.py src/taksklad/*.py tests/*.py backend/app/*.py tools/feature_acceptance_status.py tools/release_preflight.py` - OK.
  - `.venv/bin/python -m unittest tests.test_feature_user_stories_register tests.test_feature_acceptance_status` - 14 tests OK;
  - `.venv/bin/python tools/feature_acceptance_status.py` - `status=ok`, `manual_complete=false`, `no_open_errors=false`, 27 open validation gaps;
  - `.venv/bin/python tools/release_preflight.py --skip-network` - `status=ok`;
  - `git diff --check` - OK.

### DESK-09 pending-save undo state consistency

- По `DESK-09/GAP-009` найден подтвержденный локальный UX/logistics дефект: если оператор откатывал уже сохраненный КИЗ, который еще лежал в `pending_saves`, приложение обновляло pending-запись, но оставляло `saved_codes_count` прежним.
- Последствие дефекта: UI мог считать больше КИЗов сохраненными, чем фактически осталось после отката. Это влияло на следующий undo/save/finish state без необходимости ходить в Google Sheets или backend.
- Исправлено:
  - `src/taksklad/app_scanning.py` теперь уменьшает `saved_codes_count` до `len(remaining_codes)`, когда saved-код успешно откатан через локальную pending-save запись;
  - добавлен app-level regression test, что pending-save undo работает без Google/backend, обновляет active row, блокирует finish при неполном плане и не вызывает live sync.
- `GAP-009` не закрыт полностью: откат уже синхронизированного saved-кода без pending-записи всё еще требует доступного backend или Google и ручной Windows validation.
- Проверено:
  - `.venv/bin/python -m unittest tests.test_desktop_ui_contract.DesktopUiContractTests.test_undo_saved_pending_save_keeps_state_consistent_without_google_or_backend` - OK;
  - `.venv/bin/python -m unittest tests.test_desktop_ui_contract tests.test_desktop_pending_store tests.test_backend_bridge` - 58 tests OK;
  - `.venv/bin/python -m unittest discover -s tests` - 545 tests OK;
  - `.venv/bin/python -m py_compile main.py sitecustomize.py taksklad/__init__.py src/taksklad/*.py tests/*.py backend/app/*.py tools/feature_acceptance_status.py tools/release_preflight.py` - OK.
  - `.venv/bin/python -m unittest tests.test_feature_user_stories_register tests.test_feature_acceptance_status` - 14 tests OK;
  - `.venv/bin/python tools/feature_acceptance_status.py` - `status=ok`, `manual_complete=false`, `no_open_errors=false`, 27 open validation gaps;
  - `.venv/bin/python tools/release_preflight.py --skip-network` - `status=ok`;
  - `git diff --check` - OK.

### DESK-08 duplicate/backend conflict guard coverage

- По `DESK-08/GAP-008` усилена автоматическая проверка локальной части двух-PC защиты:
  - duplicate в текущей позиции, уже известных кодах и completed orders проверяется до локального backup и до постановки backend scan event;
  - pending backend scan codes попадают в `all_existing_codes` при refresh, чтобы локальная очередь backend не позволяла повторно принять тот же КИЗ до синхронизации;
  - уже существующие backend/API tests продолжают проверять `409 Code already scanned in another order item`, сохранение `existing_order` и откат локально принятого backend-blocked кода из текущей позиции.
- `GAP-008` не закрыт полностью: финальная cross-PC гарантия всё равно требует live backend sync и ручной Windows acceptance на двух рабочих местах.
- Проверено:
  - `.venv/bin/python -m unittest tests.test_desktop_ui_contract.DesktopUiContractTests.test_scan_rejects_duplicates_before_local_backup_and_backend_queue tests.test_desktop_ui_contract.DesktopUiContractTests.test_backend_blocked_scan_removes_code_and_keeps_position_open tests.test_refresh_fallback.RefreshFallbackTests.test_refresh_exposes_pending_backend_codes_as_known_duplicates tests.test_backend_bridge.BackendBridgeTests.test_backend_queue_drops_non_retryable_duplicate_scan_conflict tests.test_backend_api_persistence.BackendApiPersistenceTests.test_scan_create_is_idempotent_for_same_item_and_rejects_cross_order_duplicate` - 5 tests OK;
  - `.venv/bin/python -m unittest tests.test_refresh_fallback tests.test_desktop_ui_contract` - 50 tests OK;
  - `.venv/bin/python -m unittest discover -s tests` - 541 tests OK;
  - `.venv/bin/python -m py_compile main.py sitecustomize.py taksklad/__init__.py src/taksklad/*.py tests/*.py backend/app/*.py tools/feature_acceptance_status.py tools/release_preflight.py` - OK;
  - `.venv/bin/python tools/feature_acceptance_status.py` - `status=ok`, `manual_complete=false`, `no_open_errors=false`;
  - `.venv/bin/python tools/release_preflight.py --skip-network` - `status=ok`;
  - `git diff --check` - OK.

### Readiness redaction and DESK-03 backend live evidence

- По `DESK-03/GAP-003` добавлена фактическая live-проверка безопасной части refresh-интеграции:
  - `.venv/bin/python tools/release_preflight.py` - `status=ok`, public backend health `version=2.0.21`;
  - `curl -fsS https://api.taksklad.uz/health` - `status=ok`, `version=2.0.21`;
  - `curl -fsS https://api.taksklad.uz/ready` - `status=ok`, PostgreSQL/Alembic ready.
- `GAP-003` не закрыт полностью: live SkladBot API без токена и ручной Windows refresh остаются `Manual Acceptance`.
- При live-проверке обнаружен риск публичной диагностики: `/ready` показывал queue `event_type` с dynamic suffix после `:`, например state-store ключи.
- Исправлено:
  - `backend/app/health_service.py` санитизирует readiness queue summary: `event_type` с suffix после `:` агрегируется как `prefix:*`;
  - compact readiness errors больше не отдают raw payload, idempotency key и linked fields;
  - `tests/test_refresh_fallback.py` получил прямой regression test, что `refresh_from_sheet(initial=False)` сохраняет выбранную текущую позицию и показывает статус `текущая позиция сохранена`;
  - admin `/api/v1/admin/events` не менялся, потому он находится за обычной auth/session защитой.
- Проверено:
  - `.venv/bin/python -m unittest tests.test_refresh_fallback` - 9 tests OK;
  - `.venv/bin/python -m unittest tests.test_backend_api_persistence.BackendApiPersistenceTests.test_health_is_lightweight_and_readiness_reports_sanitized_db_queue_status` - OK;
  - `.venv/bin/python -m unittest discover -s tests` - 539 tests OK;
  - `.venv/bin/python -m py_compile main.py sitecustomize.py taksklad/__init__.py src/taksklad/*.py tests/*.py backend/app/*.py tools/feature_acceptance_status.py tools/release_preflight.py` - OK;
  - `.venv/bin/python tools/feature_acceptance_status.py` - `status=ok`, `manual_complete=false`, `no_open_errors=false`;
  - `.venv/bin/python tools/release_preflight.py --skip-network` - `status=ok`;
  - `git diff --check` - OK.

### Full feature register and first automated test/fix loop

- Создан канонический реестр `docs/taksklad-feature-user-stories.xlsx`:
  - 47 user stories из desktop, backend, web/admin, Telegram, Google Sheets, SkladBot, reports, reconciliation, deploy/runtime;
  - expected behaviour сформулирован по текущему коду, docs и тестам;
  - добавлены листы `Test Loop`, `Errors`, `Sources`, `Manual Acceptance`;
  - auto/manual/live статус разделен, чтобы не выдавать unit coverage за ручную приемку склада.
- Добавлен тест `tests/test_feature_user_stories_register.py`, который валидирует workbook как единственный канонический tracker: обязательные листы/колонки, уникальность Feature ID, соответствие `Test Loop`/`Manual Acceptance`, отсутствие `pytest`-команд и существование evidence files.
- Добавлен `tools/feature_acceptance_status.py` как машинный статусный gate для этого tracker:
  - JSON output с `scope=feature_register_status`;
  - обязательные колонки проверяются внутри CLI;
  - `Manual Acceptance` должен иметь точный набор строк по manual/auto+manual user stories, поэтому удаление строк не может сделать gate зеленым;
  - неизвестные manual/error статусы считаются проблемой;
  - `--require-manual-complete` падает с exit `3`, пока manual rows не приняты;
  - `--require-no-open-errors` падает с exit `4`, пока в `Errors` есть открытые строки.
- Явно разведены два gate:
  - `feature_acceptance_status.py` - статус полного реестра функций и user stories;
  - `tools/release_go_no_go.py` + `outputs/taksklad_acceptance/ACCEPTANCE_RESULTS.md` - production release GO/NO-GO.
- Исправлена классификация `DESK-07` и `DESK-17`: они имели auto coverage, но требуют ручной Windows UI acceptance, поэтому `Manual Result=pending`.
- `tools/prepare_acceptance_kit.py` больше не хранит текущую версию текстом `2.0.15`; version labels в acceptance kit берутся из `src/taksklad/config.py`.
- Закрыты два локально проверяемых gaps без live-доступа:
  - `GAP-004`: добавлен large-list contract/performance тест для карточного списка заказов на 2000 групп;
  - `GAP-005`: добавлен asset-level тест, что все configured product image files существуют и не пустые, а packaging add-data остается под existing Windows workflow/helper tests.
- После отдельного auto-closable selector run закрыты уже покрытые локальными тестами gaps:
  - `GAP-006`, `GAP-007`, `GAP-019`, `GAP-030`, `GAP-040`, `GAP-042`, `GAP-043`;
  - это закрывает строки `Errors/GAP`, но не заменяет ручную Windows/Telegram/Google/SkladBot/VDS acceptance там, где она указана в `Manual Acceptance`.
- Найдены и исправлены 3 подтвержденные ошибки:
  - daily reconciliation ошибочно считал mismatch, когда активный multi-SKU заказ имел одну уже выполненную позицию и Google row `Выполнено`;
  - reconciliation endpoint без `report_date` выбирал UTC-день, а не складской день `Asia/Tashkent`;
  - desktop search показывал в карточке multi-SKU заказа только найденную SKU и занижал план блоков, хотя выбор открывал полный заказ.
- Исправлены логистические gaps `GAP-027` и `GAP-044`:
  - delivery-заказы без координат больше не исчезают из логистического XLSX;
  - основной лист `Заявки` остается только для маршрутизируемых строк;
  - no/invalid coordinates попадают в отдельный лист `Требуют координаты`;
  - если на дату есть только delivery без координат, backend возвращает XLSX с листом проблем вместо `404`;
  - pickup-only и stock-shortage blocked заказы по-прежнему исключаются из логистики.
- Закрыты локально проверяемые return gaps `GAP-014` и `GAP-022`:
  - backend `/api/v1/returns/{order_id}` остается source of truth для возврата;
  - desktop в backend mode отправляет confirmed items в backend и не пишет Google-only возврат без backend order id;
  - successful return пишет return movement, освобождает КИЗ для будущего `re_outbound`, ставит Google mirror/export в очередь и создает `skladbot_return_request_create`;
  - failed return не меняет заказ, не освобождает КИЗ и не создает pending events.
- Закрыт локально проверяемый Google queue gap `GAP-046`:
  - PostgreSQL branch у `acquire_google_sheets_export_lock()` намеренно не берет session-level advisory lock;
  - внешняя гарантия параллельной обработки находится в `select_pending_export_events()` через `FOR UPDATE SKIP LOCKED`;
  - добавлен contract test, чтобы это не выглядело как непроверенный no-op.
- Закрыт локально проверяемый migration gap `GAP-031`:
  - `001_initial_schema.sql` и Alembic baseline проверяются на одинаковый набор core tables/indexes;
  - incident migration проверяется как head `20260617_0002` после baseline `20260616_0001`;
  - downgrade posture остается forward-only: restore backup или forward repair migration.
- Закрыт локально проверяемый web/admin gap `GAP-024`:
  - frontend action bar теперь показывает действие `Удалить из активных`;
  - действие доступно только для одного активного заказа без отсканированных КИЗов и без pending Google exports;
  - UI отправляет `reason`, `idempotency_key` и `expected_updated_at`;
  - backend остается source of truth для удаления, audit и постановки Google delete export в очередь;
  - ручной browser/web smoke по-прежнему pending в `Manual Acceptance`.
- Закрыт локально проверяемый desktop/runtime gap `GAP-017`:
  - `show_critical_error()` и `report_callback_exception()` больше не отправляют operational documents/error log автоматически;
  - аварийный путь оставляет non-blocking UI status/toast и короткий Telegram alert с текстом ошибки;
  - лог остается локальным диагностическим артефактом, а отправка документов выполняется только отдельными отчетными/операторскими сценариями.
- Закрыт локально проверяемый desktop/startup gap `GAP-001`:
  - добавлен `src/taksklad/desktop_smoke.py` с безопасным `run_tk_app_smoke()`;
  - entrypoints `main.py` и `pyinstaller_entry.py` получили `--smoke-gui`;
  - Windows release workflow запускает `--smoke-import` и `--smoke-gui` для onefile и onedir из clean temp dirs;
  - обычный startup, credentials/backend-read guard и ручной Windows UI acceptance не подменялись.
- Закрыт локально проверяемый desktop/UI gap `GAP-002`:
  - `--smoke-gui` теперь не только строит окно, но и проверяет semantic snapshot главного складского экрана;
  - smoke проверяет наличие order list/search/current position/photo/GTIN/scan/actions/stats/backend status/status toast widgets;
  - начальные состояния undo/next/finish остаются disabled, product photo canvas остается 170x170, order list обязан иметь canvas+scrollbar;
  - ручной Windows UI acceptance остается pending отдельно.
- Дополнительно зафиксированы ошибки качества самого test register:
  - старый `.venv` проекта на Python 3.9 не подходил для текущего backend-кода;
  - `.venv` пересобрана на Python 3.12.13 с `requirements.txt` и `backend/requirements.txt`, старая среда сохранена как `archive/local-venv-backups/.venv.py39-backup-20260622T1408`;
  - первичная таблица ошибочно использовала pytest-style команды, хотя проектный runbook использует `unittest`;
  - live VDS acceptance отделен от локальной unittest-команды.
- Проверено:
  - `/tmp/taksklad-test-py312/bin/python -m unittest tests.test_order_list_models tests.test_reconciliation_service` - 10 tests OK;
  - targeted logistics retest по routeable/no-coordinate/pickup-only/stock-shortage сценариям - 6 tests OK;
  - targeted returns retest по backend source-of-truth, desktop backend-mode, KIZ return/re_outbound и Google best-effort export - 12 tests OK;
  - targeted Google pending lock/rate-limit retest - 3 tests OK;
  - targeted migration contract retest - 4 tests OK;
  - targeted web delete-active retest по backend reason/idempotency/no-scan/Google delete queue/Telegram safe path - 6 tests OK;
  - targeted desktop critical-error retest по alert-without-documents, warning/info status notice, non-blocking status notice и diagnostic summary - 4 tests OK;
  - targeted desktop startup/release/semantic GUI smoke retest - 12 tests OK;
  - `/tmp/taksklad-test-py312/bin/python -m unittest tests.test_order_list_models tests.test_product_images` - 8 tests OK;
  - auto-closable selector run по scan quantities, readiness, reconciliation, SkladBot daily, day report и KIZ reports - 17 tests OK;
  - `/tmp/taksklad-test-py312/bin/python -m unittest tests.test_acceptance_excel_generator tests.test_feature_acceptance_status tests.test_feature_user_stories_register` - 19 tests OK;
  - 28/28 уникальных локальных команд из `Test Loop` - OK;
  - `.venv/bin/python -m unittest discover -s tests` - 538 tests OK;
  - `.venv/bin/python -m py_compile main.py sitecustomize.py taksklad/__init__.py src/taksklad/*.py tests/*.py backend/app/*.py tools/feature_acceptance_status.py` - OK;
  - `.venv/bin/python tools/feature_acceptance_status.py` - `status=ok`, `manual_complete=false`, `no_open_errors=false`;
  - `.venv/bin/python tools/feature_acceptance_status.py --require-manual-complete` - exit `3`, потому 45 manual rows pending;
  - `.venv/bin/python tools/feature_acceptance_status.py --require-no-open-errors` - exit `4`, потому 27 open Errors rows remain;
  - `.venv/bin/python tools/release_preflight.py --skip-network` - `status=ok`;
  - `npm run build` в `frontend` - OK;
  - `docker compose --env-file deploy/vds/.env.example -f deploy/vds/docker-compose.yml config` - OK;
  - `bash -n deploy/vds/*.sh` - OK;
  - `npm audit --audit-level=high` в `frontend` - 0 vulnerabilities.
- Не закрыто:
  - ручная приемка Windows UI, принтера, updater, live Telegram/Google/SkladBot и VDS acceptance. Эти сценарии вынесены в лист `Manual Acceptance` и остаются обязательными перед утверждением цели как полностью завершенной.

## 2026-06-21

### Ежедневный SkladBot отчет по дате создания заявки

- Симптом: отчет за `21.06.2026` включил заявки с `Дата создания = 20.06.2026`, потому что прежняя логика относила закрытые заявки к дню выполнения/архивации или первому обнаружению.
- Решение:
  - daily report теперь включает только заявки, которые одновременно `Выполнена` и `В архиве`;
  - дата попадания заявки в отчет берется из `created_at`/`createdAt` в бизнес-таймзоне;
  - `completedAt`, `archivedAt`, `updatedAt` и `Дата выгрузки` больше не переносят заявку между daily reports;
  - причина включения в XLSX теперь `создана`;
  - registry `pending_events` сохранен как антидубль плановой отправки, но не определяет отчетную дату заявки.
- Риск нового правила: заявка, созданная в один день и закрытая позже, не попадет в отчет за день закрытия. Это осознанно принято в пользу правила по дате создания.
- Проверено:
  - `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:. /tmp/taksklad-fulltest-codex-venv/bin/python -m unittest tests.test_skladbot_daily_report` - 19 tests OK.
  - `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:. /tmp/taksklad-fulltest-codex-venv/bin/python -m unittest tests.test_skladbot_daily_report tests.test_backend_telegram_import` - 82 tests OK.
  - `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:. /tmp/taksklad-fulltest-codex-venv/bin/python -m compileall -q backend/app/skladbot_daily_report.py tests/test_skladbot_daily_report.py` - OK.
  - `git diff --check` - OK.
- VDS deploy:
  - restore point: `/opt/taksklad/restore_points/pre-daily-report-created-date-20260621T175350Z`;
  - Postgres backup: `/opt/taksklad/backups/postgres/taksklad-postgres-20260621T175350Z.sql.gz`;
  - selective sync: `backend/app/skladbot_daily_report.py`, `tests/test_skladbot_daily_report.py`, `docs/report-source-rules.md`, `docs/implementation-log.md`, `docs/changelog.md`;
  - пересобран и перезапущен только `telegram-worker`, потому что daily report импортируется из `backend/app/telegram_worker.py`;
  - VDS `telegram-worker` compileall по `app/skladbot_daily_report.py` и `app/telegram_worker.py` - OK;
  - `https://api.taksklad.uz/health` - OK, `https://api.taksklad.uz/ready` - OK, Alembic `current_revision=20260617_0002`;
  - VDS `./deploy/vds/acceptance_status.sh` - общий `status=ok`;
  - свежие логи `telegram-worker` и `backend-api` за 10 минут - без `ERROR/Traceback/Exception/CRITICAL/failed`.

## 2026-06-20

### Ежедневный SkladBot отчет по факту закрытия заявок

- Симптом: приемка на `10 000` блоков была видна через прирост остатков между отчетами `19.06.2026` и `20.06.2026`, но строка `Приемка` оставалась `0`, потому что заявка была создана раньше, а выполнена позже.
- Причина: daily report включал заявку по `created_at`, `updated_at` или `unloading_date`; если SkladBot не обновлял эти даты при проведении, закрытая приемка могла не попасть ни во вчерашний, ни в сегодняшний отчет.
- Решение:
  - daily report теперь включает только заявки, которые одновременно `Выполнена` и `В архиве`;
  - если SkladBot отдает дату закрытия/архивации, она используется как дата факта с учетом cutoff отчета `22:00`;
  - заявки, закрытые после cutoff, относятся к следующему отчетному дню;
  - если SkladBot не отдает дату факта, закрытая заявка из текущего/предыдущего отчетного окна включается как `впервые найдена выполненной`;
  - для приемочных типов detail проверяется даже при старой list-дате, потому что `completedAt`/`acceptedAmount` могут быть доступны только в карточке заявки;
  - после успешной плановой отправки Telegram worker записывает request registry в `pending_events`, чтобы та же закрытая заявка не повторялась в следующих daily reports;
  - ручная команда `/skladbot_daily` не пишет в registry, чтобы тестовые отправки в ЛС не ломали будущий рабочий отчет.
- Проверено без Telegram-отправок:
  - `./.venv/bin/python -m unittest tests.test_skladbot_daily_report` - 19 tests OK;
  - `./.venv/bin/python -m unittest tests.test_backend_telegram_import` - 63 tests OK;
  - `./.venv/bin/python -m compileall -q backend/app/skladbot_daily_report.py backend/app/skladbot_worker.py backend/app/telegram_worker.py tests/test_skladbot_daily_report.py` - OK.
- VDS deploy:
  - из-за грязного локального worktree на VDS доставлены только runtime-файлы `backend/app/skladbot_daily_report.py`, `backend/app/skladbot_worker.py`, `backend/app/telegram_worker.py`;
  - restore point: `/opt/taksklad/restore_points/pre-daily-report-fact-date-20260620T191024Z`;
  - Postgres backup: `/opt/taksklad/backups/postgres/taksklad-postgres-20260620T191024Z.sql.gz`;
  - пересобраны и перезапущены `backend-api`, `telegram-worker`, `skladbot-worker`;
  - VDS in-container `compileall` по обновленным runtime-файлам - OK;
  - `https://api.taksklad.uz/health` - OK, `https://api.taksklad.uz/ready` - OK, Alembic `current_revision=20260617_0002`;
  - VDS `./deploy/vds/acceptance_status.sh` - общий `status=ok`;
  - свежие логи `backend-api`, `telegram-worker`, `skladbot-worker` - без `ERROR/Traceback/Exception`;
  - тестовые Telegram-отправки не выполнялись.

## 2026-06-16

### Production hardening roadmap 2.0.x

- Выполнен полный supergoal hardening без deploy/push в production.
- Добавлены server-side readiness checks: `/health` остается легким, `/ready` и `/api/v1/readiness` проверяют DB, Alembic, event queue и import errors.
- Добавлен Alembic baseline и runbook миграций. Production baseline stamp перед первым `upgrade head` обязателен, если схема уже создана старым SQL.
- Ужесточены DB-инварианты КИЗов: per-KIZ advisory lock, audit movements, payload-idempotency для Google mirror событий, preflight SQL перед будущими уникальными ограничениями.
- Event queue получила lifecycle-диагностику, stale-processing reset и retry/cooldown для Google 429.
- Admin/web/Telegram ручные действия теперь требуют reason, пишут actor/source/idempotency и защищены от stale повторов.
- Web panel показывает readiness, event queue/import diagnostics, sanitized audit details и disabled reasons.
- Telegram ручное управление admin-gated, stale delete-confirm заново проверяет активный заказ и сканы, ошибки отчетов стали короткими и actionable.
- Отчеты зафиксированы как DB-first там, где source of truth TakSklad: day report, logistics, KIZ date/source-file. Ежедневный SkladBot report берет данные из SkladBot API, Google не участвует.
- Исправлены edge cases отчетов: `acceptedAmount=0` не заменяется планом, плохая дата `/skladbot_daily` больше не подставляет сегодня, самовывоз-варианты исключаются из логистики.
- Rollback: откат к предыдущему good commit + `docker compose up -d --build`, при необходимости `alembic downgrade` только по отдельному плану и после backup/restore drill.

Проверено:

- `./.venv/bin/python -m unittest discover -s tests` - 458 tests OK.
- `./.venv/bin/python -m unittest tests.test_daily_report tests.test_skladbot_daily_report tests.test_backend_api_persistence tests.test_backend_google_sheets_exporter tests.test_backend_telegram_import` - 160 tests OK.
- `./.venv/bin/python -m compileall -q backend/app src/taksklad tests tools` - OK.
- `npm --prefix frontend run build` - OK.
- `docker compose --env-file deploy/vds/.env.example -f deploy/vds/docker-compose.yml config` - OK.

## 2026-06-09

### План и факт в списке заявок ежедневного SkladBot отчета

- Причина: после исправления сводки кейс `план 1 / принято 1750` был виден в итогах, но не был очевиден на листе `Заявки`.
- Решение:
  - лист `Заявки` теперь показывает `Блоков план`, `Блоков факт`, `Отклонение`;
  - лист `Товары заявок` показывает `Блоков план`, `Принято факт`, `Блоков факт`, `Отклонение`;
  - для приемки `Блоков факт` строится из SkladBot `acceptedAmount`;
  - для отгрузок и возвратов факт совпадает с количеством заявки, если SkladBot не отдал отдельного фактического поля.
- Поведение расписания:
  - ручная переотправка не запускалась;
  - ежедневный триггер остается только на `22:00`.
- Проверено:
  - `./.venv/bin/python -m unittest tests.test_skladbot_daily_report` - 7 tests OK;
  - `./.venv/bin/python -m unittest discover tests` - 402 tests OK;
  - `./.venv/bin/python -m compileall -q backend/app src/taksklad tools main.py tests` - OK;
  - `docker compose --env-file deploy/vds/.env.example -f deploy/vds/docker-compose.yml config` - OK;
  - `git diff --check` - OK.

### Фактическая приемка в ежедневном SkladBot отчете

- Симптом: в отчете `Приемка` показывала `2`, хотя SkladBot по заявке `WH-R-194859` показывал фактически принято:
  - `Chapman Red OP 20 UZ - KingSize`: `acceptedAmount=1250`;
  - `Chapman Brown OP 20 UZ - KingSize`: `acceptedAmount=1750`.
- Причина: отчет использовал плановое поле `products.amount`. В заявках приемки это может быть техническое `1`, а не фактическая приемка.
- Дополнительный факт:
  - `/warehouse/transactions` и `/report/transactions` за `09.06.2026` не вернули приходные движения;
  - `/report/stock` вернул только общий остаток `3818`, без SKU;
  - `/products` вернул текущие остатки по SKU: Red `1421`, Brown `2122`, Gold `275`.
- Решение:
  - `backend/app/skladbot_worker.py` сохраняет `accepted_amount` из SkladBot detail;
  - `backend/app/skladbot_daily_report.py` для категории `Приемка` берет `accepted_amount`, если он есть;
  - `acceptedAmount` в SkladBot уже приходит в блоках, поэтому отчет берет его как есть;
  - SKU-остатки на конец дня берутся из `/products`, общий `/report/stock` остается контрольным итогом.
- Проверено:
  - `./.venv/bin/python -m unittest tests.test_skladbot_daily_report` - 7 tests OK;
  - `./.venv/bin/python -m unittest discover tests` - 402 tests OK;
  - `./.venv/bin/python -m compileall -q backend/app src/taksklad tools main.py tests` - OK;
  - `docker compose --env-file deploy/vds/.env.example -f deploy/vds/docker-compose.yml config` - OK;
  - `git diff --check` - OK;
  - VDS restore point: `/opt/taksklad/restore_points/pre-daily-report-accepted-amount-20260609T173947Z`;
  - VDS Postgres backup: `/opt/taksklad/backups/postgres/taksklad-postgres-20260609T173947Z.sql.gz`;
  - VDS пересобраны и перезапущены `backend-api`, `telegram-worker`, `skladbot-worker`;
  - VDS live-smoke по `WH-R-194859`: Red `acceptedAmount=1250 -> 1250` блоков, Brown `acceptedAmount=1750 -> 1750` блоков;
  - ручная переотправка отчета за `09.06.2026` выполнена в настроенный Telegram-чат;
  - VDS `./deploy/vds/acceptance_status.sh` - общий `status=ok`;
  - свежие логи `backend-api`, `telegram-worker`, `skladbot-worker` - без `ERROR/Traceback/Exception`.

### SKU-колонки в ежедневном SkladBot отчете

- Симптом: в ежедневном Excel-отчете на листе `Сводка` оставались заглушки `SKU1/SKU2/SKU3`, хотя там должны быть реальные товары и движение по каждому SKU.
- Причина: `backend/app/skladbot_daily_report.py` жестко писал три пустые SKU-колонки, а лист `Остатки` сворачивал `/report/stock` в одну агрегированную строку по клиенту.
- Решение:
  - `product_breakdown_for_summary()` собирает товары из текущего остатка SkladBot и товаров заявок за день;
  - один товар склеивается по названию, артикулу или штрихкоду, чтобы остаток и заявка не превращались в разные колонки;
  - `Сводка` строит динамические колонки с реальными названиями SKU;
  - для каждой SKU заполняются `Остаток на начало дня`, `Приемка`, `Отгрузка`, `Возврат`, `Остаток на конец дня`;
  - `Остатки` снова показывает построчные остатки SkladBot по товарам.
- Source of truth:
  - SkladBot API остается источником ежедневного отчета;
  - Google Sheets в этом отчете не участвует.
- Проверено:
  - `./.venv/bin/python -m unittest tests.test_skladbot_daily_report` - 6 tests OK;
  - `./.venv/bin/python -m unittest discover tests` - 401 tests OK;
  - `./.venv/bin/python -m compileall -q backend/app src/taksklad tools main.py tests` - OK;
  - `docker compose --env-file deploy/vds/.env.example -f deploy/vds/docker-compose.yml config` - OK;
  - `git diff --check` - OK;
  - VDS restore point: `/opt/taksklad/restore_points/pre-daily-report-sku-summary-20260609T171702Z`;
  - VDS Postgres backup: `/opt/taksklad/backups/postgres/taksklad-postgres-20260609T171702Z.sql.gz`;
  - VDS пересобраны и перезапущены `backend-api`, `telegram-worker`;
  - VDS synthetic XLSX-smoke внутри `telegram-worker`: вместо `SKU1/SKU2/SKU3` в сводке стоят `Chapman Brown OP 20`, `Chapman Gold SSL`, `Chapman RED OP 20`;
  - VDS `./deploy/vds/acceptance_status.sh` - общий `status=ok`;
  - свежие логи `backend-api` и `telegram-worker` после деплоя - без `ERROR/Traceback/Exception`.

### Оперативная разблокировка склада при полной позиции

- Симптом: Windows-приложение на складе показало `КИЗы не записаны`, `Осталось в очереди: 7` по WH-R-194868.
- Факт по backend: позиция `Chapman RED OP 20` для `"TABAK TORG" MCHJ Андижан` уже была полностью сохранена: `150/150`, статус `completed`.
- Причина блокировки: локальная Windows-очередь пыталась дослать лишние scan-события в уже полную позицию и получала `409 Order item is already fully scanned`.
- Оперативное решение:
  - backend для полной позиции возвращает успешный ответ по последнему сохраненному скану и не добавляет лишний КИЗ;
  - desktop-очередь тоже считает такой `409` принятым, чтобы будущие сборки не блокировались.
- Проверено:
  - точечные backend/desktop tests - 5 tests OK;
  - compileall по затронутым модулям - OK;
  - VDS smoke: лишний smoke-КИЗ в полную позицию WH-R-194868 вернул `201`, счетчик scan_codes остался `150 -> 150`;
  - `https://api.taksklad.uz/health` - 200 OK.

### Самовывоз и логистический отчет без ручной чистки

- Причина: в боевом Telegram upload часть заказов без адреса попадала в логистический отчет. Их приходилось удалять руками, потому что это самовывоз или строки без маршрутизируемых координат.
- Решение:
  - `backend/app/excel_importer.py` и `src/taksklad/excel_import.py` нормализуют пустой/технический адрес без координат в `Самовывоз со склада`;
  - явные варианты `Самовывоз` и `Самовывоз со склада` приводятся к одному значению;
  - Telegram/desktop строки с координатами остаются доставкой: адрес берется reverse geocode по координатам, либо пишется `Координаты: ...`;
  - `backend/app/imports_service.py` сохраняет `Самовывоз со склада` как backend-норму для прямого API import без реального адреса;
  - `backend/app/logistics_service.py` строит даты по delivery-заказам, где адрес не самовывоз и заказ не заблокирован stock-shortage;
  - delivery-заказы с валидными координатами попадают в основной маршрутный лист, а без валидных координат выводятся отдельным листом `Требуют координаты`;
  - координаты для логистики теперь проверяются по диапазонам широты/долготы, поэтому `999,999` не считается маршрутом;
  - `backend/app/google_sheets_exporter.py` пишет в mirror/export такой же fallback `Самовывоз со склада`.
- Source of truth:
  - backend DB остается основным источником;
  - Google Sheets только отражает backend-состояние.
- Проверено:
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

### Обновление шаблона ежедневного SkladBot отчета

- Причина: Антон прислал рабочий XLSX-пример `TakSklad_SkladBot_daily_08.06.2026 (2).xlsx`; старый лист `Сводка` был техническим и неудобным для ежедневного отчета.
- Решение:
  - `backend/app/skladbot_daily_report.py` теперь генерирует лист `Сводка` в формате примера: `Дата отчета`, `Сформировано`, `customer_id`, блок `Отчет о движении остатков за день`;
  - в сводке `Приемка` и `Возврат` положительные, `Отгрузка` отрицательная;
  - `Остаток на начало дня` считается Excel-формулой `=B12-B9-B10-B11`;
  - колонки `SKU1/SKU2/SKU3` заменены на реальные названия товаров из SkladBot, по каждой колонке считается начало дня, приемка, отгрузка, возврат и конец дня;
  - `Остаток на конец дня` берется из SkladBot `/report/stock`;
  - лист `Остатки` показывает построчные остатки SkladBot по товарам, а не одну агрегированную строку по клиенту;
  - сохранены листы `Заявки`, `Товары заявок`, `Движения`, `Остатки`, `Ошибки`;
  - добавлены точные ширины колонок и границы таблицы движения остатков.
- После VDS dry-run обнаружен SkladBot `429 Too Many Requests` на одном detail-запросе. Добавлена защита:
  - общий `SKLADBOT_DAILY_REPORT_REQUEST_DELAY_SECONDS` теперь применяется и между списками заявок;
  - `get_daily_request_detail()` повторяет detail-запрос при `429`;
  - retry управляется `SKLADBOT_DAILY_REPORT_429_RETRIES` и `SKLADBOT_DAILY_REPORT_429_RETRY_SECONDS`.
- Источник данных:
  - SkladBot API остается source of truth;
  - Google Sheets не используется для ежедневного отчета.
- Проверено:
  - `./.venv/bin/python -m unittest tests.test_skladbot_daily_report` - 5 tests OK.
  - `./.venv/bin/python -m unittest tests.test_skladbot_daily_report tests.test_backend_telegram_import` - 49 tests OK.
  - `./.venv/bin/python -m unittest discover tests` - 379 tests OK.
  - `./.venv/bin/python -m compileall -q backend/app src/taksklad tools main.py tests` - OK.
  - `docker compose --env-file deploy/vds/.env.example -f deploy/vds/docker-compose.yml config` - OK.
  - `git diff --check` - OK.
  - VDS restore point: `/opt/taksklad/restore_points/pre-daily-report-template-20260609T082636Z`.
  - VDS Postgres backup: `/opt/taksklad/backups/postgres/taksklad-postgres-20260609T082636Z.sql.gz`.
  - VDS `telegram-worker` пересобран и перезапущен.
  - VDS `.env` обновлен: `SKLADBOT_DAILY_REPORT_429_RETRIES=2`, `SKLADBOT_DAILY_REPORT_429_RETRY_SECONDS=15.0`.
  - VDS live read-only dry-run за `09.06.2026`: `requests_total=27`, `category_counts={Отгрузка: 26, Возврат: 0, Приемка: 1, Прочее: 0}`, `blocks={Отгрузка: 1069, Возврат: 0, Приемка: 2, Прочее: 0}`, `stock_total=931`, `errors_count=0`, строки сводки `[2, -1069, 0, 931]`.
  - VDS `./deploy/vds/acceptance_status.sh` - общий `status=ok`, `backend_health.status=ok`, `telegram_menu.status=ok`, `skladbot_coverage.status=ok`.

### Backend scan 500 из-за порядка записи scan_codes и kiz_movements

- Симптом: складское Windows-приложение показывало `КИЗы не записаны`, причина `Backend не принял все КИЗы позиции. Осталось в очереди: 3/4`.
- По desktop-логу видно, что локальная `pending_backend_events` очередь не очищалась и блокировала повторные сохранения.
- По VDS backend-логу за тот же период найден корень: `psycopg.errors.ForeignKeyViolation` по `kiz_movements_scan_code_id_fkey`.
- Причина: `backend/app/orders_service.py` создавал объект `ScanCode`, но до `record_kiz_movement(...)` не делал `db.flush()`. В PostgreSQL `kiz_movements.scan_code_id` ссылался на `scan_codes.id`, который еще не был вставлен.
- Решение: добавлен явный `db.flush()` сразу после `db.add(scan)` и до записи движения КИЗа.
- Защита: добавлен тест `test_scan_flushes_scan_code_before_kiz_movement`, который падает без flush и подтверждает правильный порядок записи.

## 2026-06-08

### SkladBot daily client activity and stock report

- Причина: Антону нужен ежедневный отчет в Telegram по клиенту: сколько было заявок, сколько возвратов, приемок, отгрузок, по каким юрлицам/точкам и датам выгрузки, плюс актуальный остаток на текущий день.
- Решение:
  - добавлен `backend/app/skladbot_daily_report.py`;
  - отчет собирается только из SkladBot API: `/requests`, `/requests/show/{id}`, `/warehouse/transactions`, `/report/stock`;
  - Google Sheets в этом процессе не используется и не считается источником данных;
  - Telegram отправляет короткую сводку текстом и полный XLSX отдельным файлом;
  - листы XLSX: `Сводка`, `Заявки`, `Товары заявок`, `Движения`, `Остатки`, `Ошибки`;
  - ручная проверка через admin-команду `/skladbot_daily ДД.ММ.ГГГГ`;
  - ежедневная отправка включается env-флагом `SKLADBOT_DAILY_REPORT_ENABLED=true`;
  - список получателей задается только явно через `SKLADBOT_DAILY_REPORT_CHAT_IDS`;
  - время отправки задается через `SKLADBOT_DAILY_REPORT_HOUR=22` и `SKLADBOT_DAILY_REPORT_MINUTE=0`;
  - между detail-запросами к SkladBot используется `SKLADBOT_DAILY_REPORT_REQUEST_DELAY_SECONDS=3.0`, потому что live dry-run на `0.25` поймал `429 Too Many Requests`;
  - защита от повторной отправки за тот же день и чат хранится в `pending_events`.
- Ограничение:
  - без реального `chat_id` автоматическая отправка не включается;
  - если SkladBot временно не отдаст один endpoint, отчет все равно сформируется с листом `Ошибки`.
- Проверено:
  - `./.venv/bin/python -m unittest tests.test_skladbot_daily_report tests.test_backend_telegram_import` - 46 tests OK.
  - `./.venv/bin/python -m unittest discover tests` - 375 tests OK.
  - `./.venv/bin/python -m compileall -q backend/app src/taksklad tools main.py tests` - OK.
  - `npm run build` в `frontend` - OK.
  - `docker compose --env-file deploy/vds/.env.example -f deploy/vds/docker-compose.yml config` - OK.
  - VDS deploy выполнен через `rsync` + `docker compose up -d --build backend-api telegram-worker`.
  - VDS restore point: `/opt/taksklad/restore_points/pre-skladbot-daily-report-20260608T122858Z`.
  - VDS Postgres backup: `/opt/taksklad/backups/postgres/taksklad-postgres-20260608T122910Z.sql.gz`.
  - VDS live dry-run на `SKLADBOT_DAILY_REPORT_REQUEST_DELAY_SECONDS=0.25` поймал `429`, поэтому дефолт повышен до `3.0`.
  - VDS live read-only dry-run на дефолте `3.0` завершился без ошибок: `requests_total=71`, `category_counts={Отгрузка: 67, Возврат: 3, Приемка: 1, Прочее: 0}`, `stock_total=1578`, `errors_count=0`.
  - VDS `./deploy/vds/acceptance_status.sh` - общий `status=ok`.
  - После live deploy в логах был Telegram `409 Conflict`, значит второй poller существует вне текущего VDS compose-стека или конфликт был внешним. `telegram_worker` изменён так, чтобы при `getUpdates` `409` не падать всем циклом и всё равно запускать scheduled jobs.
  - Повторный VDS acceptance сначала поймал 3 свежих активных заказа без WH-R; после ожидания цикла `skladbot-worker` все 29 активных заказов получили SkladBot номера, финальный `acceptance_status.sh` вернул общий `status=ok`.
- Runtime:
  - на VDS автоматическая отправка пока выключена: `SKLADBOT_DAILY_REPORT_ENABLED` не задан;
  - `SKLADBOT_DAILY_REPORT_CHAT_IDS` пустой, потому что Антон ещё не указал целевой чат.

### Telegram Excel import: обязательный ручной ввод даты после файла

- Причина: после исправления приоритета Excel-даты Антон утвердил более жёсткий рабочий процесс: при загрузке файла бот всегда спрашивает дату отгрузки, а оператор вводит её вручную в формате `ДД.ММ.ГГГГ`.
- Решение:
  - новый статус Telegram import-события `waiting_shipment_date`;
  - Excel-документ после загрузки не попадает в `pending` сразу и не уходит в backend;
  - следующий ручной ввод даты переводит самое раннее ожидающее событие этого чата в `pending`;
  - `telegram_worker` запускает очередь только после подтверждения даты;
  - `excel_importer` получил явный режим `force_shipment_date`, в котором ручная дата переопределяет дату из Excel;
  - сохранённая дата чата больше не используется для автоматического Excel import.
- Проверено:
  - `./.venv/bin/python -m unittest tests.test_backend_telegram_import` - 43 tests OK.
  - `./.venv/bin/python -m unittest discover tests` - 372 tests OK.
  - `./.venv/bin/python -m compileall -q backend/app src/taksklad tools main.py tests` - OK.
  - `git diff --check` - OK.

### Telegram Excel import date-source guard

- Причина: боевой Telegram import файла `Шаблон_отправки_заказов_на_склад_09_06_2026.xlsx` получил старую сохранённую Telegram-дату `08.06.2026`; из-за прежнего приоритета `shipment_date` перед датой Excel заказы и SkladBot-заявки создались с `unloading_date=2026-06-08`.
- Решение:
  - `backend/app/excel_importer.py` теперь берёт дату из Excel в первую очередь: колонка/контекст/имя файла;
  - Telegram `shipment_date` используется только как fallback, если Excel не содержит даты;
  - конфликт Telegram-даты с Excel-даты переводит pending import в `waiting_date_choice` до backend import и до создания SkladBot-заявок;
  - бот показывает inline-кнопки `Использовать дату Excel: ...` и `Отменить импорт`;
  - выбор Excel очищает старую Telegram-дату в событии и запускает импорт заново по дате файла;
  - отмена переводит событие в `cancelled`.
- Текущие ошибочно созданные боевые WH-R за `08.06.2026` по решению Антона оставлены как есть, без repair данных.
- Проверено:
  - `./.venv/bin/python -m unittest tests.test_backend_telegram_import` - 40 tests OK.
  - `./.venv/bin/python -m unittest discover tests` - 369 tests OK.
  - `./.venv/bin/python -m compileall -q backend/app src/taksklad tools main.py tests` - OK.
  - `git diff --check` - OK.

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

### Web complete-without-KIZ partial scans

- Причина: в web-панели действие `В архив как выполнено` было недоступно, если среди выбранных активных заказов была частично отсканированная позиция. Для ручного закрытия боевых исключений это мешало завершать заказ в нужный момент.
- Изменение:
  - web больше не блокирует `В архив как выполнено` из-за частичных сканов;
  - backend `/api/v1/admin/orders/bulk/complete-without-kiz` тоже разрешает частично отсканированные активные заказы;
  - уже записанные `scanned_blocks` и `scan_codes` сохраняются, действие только переводит заказ и позиции в `completed` и ставит Google archive export в очередь.
- Ограничения сохранены:
  - `В архив без КИЗов` и `Отменить` по-прежнему недоступны, если в заказе уже есть сканы;
  - неактивные заказы и заказы с pending Google export по-прежнему не закрываются этим bulk-действием.

### Desktop 2.0.15 scan error UX

- Причина: при отклонении КИЗа сотрудник видел короткую ошибку в нижней строке, длинный текст обрезался, а дубль КИЗа не объяснял, в каком заказе код уже занят.
- Backend:
  - `/api/v1/scans` для дубля в другой позиции теперь возвращает `existing_order` с юрлицом, датой отгрузки, товаром и номером SkladBot;
  - старый `message=Code already scanned in another order item` сохранен как машинный маркер для совместимости.
- Desktop:
  - ошибка скана показывается отдельным красным rounded toast внизу приложения и исчезает через `5` секунд;
  - текст не заменяет рабочий статус и переносится строками;
  - дубль КИЗа теперь показывает: юрлицо, дату отгрузки, товар, SkladBot-номер и сам код;
  - при локальном совпадении в уже загруженных заказах приложение также ищет владельца КИЗа в текущих данных и не пишет больше, что причина только в Google Sheets.
- Версия desktop поднята до `2.0.15` для production rollout.

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

### SkladBot request auto-create enabled

- Причина: убрать ручной этап создания заявок SkladBot после Telegram Excel import. Целевой поток: Excel в Telegram -> VDS/Google import -> автоматическое создание заявки SkladBot -> сохранение WH-R в VDS -> экспорт WH-R в Google.
- Backend:
  - добавлен `PendingEvent.idempotency_key` и уникальный индекс, чтобы один заказ не мог создать SkladBot-заявку дважды;
  - `SKLADBOT_CREATE_REQUESTS_MODE=enabled` теперь ставит `skladbot_request_create` events для ready-заказов после import;
  - `rebuild` dry-run остается read-only и не вызывает POST;
  - `skladbot-worker` обрабатывает `skladbot_request_create` перед обычным WH-R backfill;
  - перед повторным POST после retry worker ищет уже созданную заявку, чтобы не плодить дубли после timeout/process crash;
  - после `POST /v1/requests` worker обязательно делает `GET /v1/requests/show/{id}` и сохраняет канонический WH-R из detail;
  - созданный номер/ID пишутся в `Order.raw_payload`, затем ставится точечный `google_sheets_skladbot_export` по `order_id`.
- SkladBot API verification:
  - `GET /v1/requests/form-data` подтвердил `request_type_id=3389` и обязательные поля `address`, `comment`, `company_name`, `unloading_date`;
  - product lookup подтвердил mapping Red `2189390`, Brown `2189391`, Gold SSL `2189394`;
  - создано 2 разрешенные тестовые заявки API на `company_name=ИП Даврон`: `WH-R-193682`, `WH-R-193683`;
  - важное наблюдение: POST response вернул некорректно повторяющийся `delivery_number`, а `show/{id}` вернул правильные WH-R, поэтому canonical read после POST обязателен.
- VDS:
  - перед деплоем создан backup `/opt/taksklad/backups/postgres/taksklad-postgres-20260604T180955Z.sql.gz`;
  - обновлены `backend/`, `frontend/`, `deploy/vds/`;
  - применена schema с `pending_events.idempotency_key`;
  - на VDS выставлено `SKLADBOT_CREATE_REQUESTS_MODE=enabled`;
  - пересобраны и перезапущены `backend-api`, `skladbot-worker`, `frontend`;
  - активных заказов без WH-R на момент включения нет, поэтому задним числом лишние заявки не создавались.
- Web:
  - вкладка `SkladBot dry-run` теперь показывает статусы `queued`, `created`, `recovered`, `create_failed`;
  - история импортов показывает queued/created/blocked summary.
- Проверено:
  - `./.venv/bin/python -m unittest tests.test_backend_skladbot_request_dry_run` - 15 tests OK;
  - `./.venv/bin/python -m unittest tests.test_backend_skladbot_worker` - 47 tests OK;
  - `./.venv/bin/python -m unittest tests.test_backend_google_sheets_exporter tests.test_backend_google_sheets_pending` - 21 tests OK;
  - `./.venv/bin/python -m unittest tests.test_backend_api_persistence` - 50 tests OK;
  - `./.venv/bin/python -m unittest discover -s tests` - 344 tests OK;
  - `npm run build` в `frontend` - OK;
  - `https://api.taksklad.uz/health` - OK.

### Release 2.0.8 forced update rollout

- Причина: складские ПК должны перейти на актуальную рабочую сборку `2.0.8`, где собраны последние исправления backend-first логики, SkladBot auto-create и Windows release artifacts.
- Изменено:
  - `version.json` переведен в принудительный режим: `latest_version=2.0.8`, `min_supported_version=2.0.8`, `mandatory=true`;
  - текст update message прямо просит нажать `Да`, дождаться установки и запускать только новый `TakSklad.exe`;
  - release preflight, Windows test archive helper, VDS acceptance status, GO/NO-GO и acceptance kit теперь считают корректным только forced rollout `2.0.8`;
  - `main/version.json` обновлен отдельным commit, потому автообновление старых клиентов читает публичный manifest из `main`;
  - серверная копия `/opt/taksklad/app/version.json` и acceptance scripts синхронизированы на VDS.
- Проверено:
  - `./.venv/bin/python -m unittest tests.test_release_preflight tests.test_vds_acceptance_scripts tests.test_windows_test_build_helper tests.test_release_go_no_go` - 25 tests OK;
  - `./.venv/bin/python tools/release_preflight.py --verify-downloads --timeout 120` - `status=ok`;
  - GitHub release assets `TakSklad.exe` и `TakSklad-windows-x64.zip` скачаны и совпали по SHA256;
  - публичный `https://raw.githubusercontent.com/1fear/TakSklad/main/version.json` отдает `mandatory=true` и `min_supported_version=2.0.8`;
  - VDS health: `https://api.taksklad.uz/health` возвращает `version=2.0.8`.
- Ограничение:
  - старые desktop-клиенты `2.0.5/2.0.6` технически показывают обязательность через `below_min_version`, но в их коде окно обновления еще содержит кнопку отказа. Полностью убрать отказ можно только следующим desktop release с hard-block обновлений и версионным header/backend gate.

### Returns confirmation and SkladBot Возврат 3PL auto-create

- Причина: возвраты должны проходить через понятное подтверждение состава на складе и автоматически создавать отдельную заявку SkladBot `Возврат 3PL`, не затирая исходный WH-R отгрузки.
- Desktop:
  - после поиска возврата приложение показывает окно подтверждения с исходной заявкой, клиентом, датой отгрузки, адресом, типом оплаты и SKU/блоками;
  - оператор подтверждает полный возврат без редактирования количества;
  - в backend отправляются `return_reference`, `returned_by` и `confirmed_items`.
- Backend:
  - `POST /api/v1/returns/{order_id}` принимает строгую схему возврата;
  - `confirmed_items` сверяются с исходным заказом по item id, SKU, блокам и штукам;
  - при расхождении возврат отклоняется без изменения заказа и без pending events;
  - при успешном возврате создается отдельный `PendingEvent` типа `skladbot_return_request_create`.
- SkladBot:
  - добавлен worker для реального создания `Возврат 3PL`;
  - payload: `customer_id=6211`, `request_type_id=3403`, поля `address`, `comment`, `company_name`, `unloading_date`, товары в блоках;
  - возвратные номер/ID пишутся отдельно: `skladbot_return_request_number`, `skladbot_return_request_id`, `skladbot_return_request_status`;
  - исходные `skladbot_request_number` и `skladbot_request_id` от отгрузки не меняются.
- Google:
  - в `Архив` и `Возвраты` добавлены отдельные колонки возвратной заявки SkladBot;
  - повторный экспорт возврата обновляет Google после создания возвратного WH-R.
- Проверено:
  - `./.venv/bin/python -m unittest discover -s tests` - 353 tests OK.

### Returns 3PL VDS deploy and smoke

- Причина: вывести новый контур возвратов на VDS и проверить реальное создание заявки SkladBot до боевого теста.
- Перед деплоем создан backup Postgres: `/opt/taksklad/backups/postgres/taksklad-postgres-20260605T074030Z.sql.gz`.
- На VDS обновлен `backend/`, пересобраны и перезапущены `backend-api`, `skladbot-worker`, `telegram-worker`, `google-sheets-sync-worker`.
- Добавлена защита от дублей возвратных заявок:
  - при retry `skladbot_return_request_create` worker сначала ищет уже созданную `Возврат 3PL`;
  - если заявка найдена по клиенту, дате, типу оплаты, SKU и блокам, сохраняется статус `created_recovered` без повторного POST.
- Smoke на VDS:
  - создан тестовый заказ `TAKSKLAD_RETURN_TEST_20260605_10610b80`;
  - исходный тестовый WH-R: `WH-R-TEST-10610b80`;
  - создана реальная возвратная заявка SkladBot: `WH-R-193808`, id `193808`;
  - order_id в VDS: `916144c7-ac11-4a3e-8f79-a8dd4199ff0c`;
  - Google events `google_sheets_archive_export` и `google_sheets_return_export` завершились успешно.
- Проверено:
  - `./.venv/bin/python -m unittest tests.test_backend_skladbot_request_dry_run` - 21 tests OK;
  - `./.venv/bin/python -m unittest discover -s tests` - 354 tests OK;
  - `git diff --check` - OK;
  - `https://api.taksklad.uz/health` - OK, версия backend `2.0.8`.

### Release 2.0.9 Windows rollout

- Причина: складским ПК нужна новая desktop-сборка с окном подтверждения возврата; backend уже требует `confirmed_items` при возврате.
- Изменено:
  - `APP_VERSION` повышен до `2.0.9`;
  - backend `APP_VERSION` повышен до `2.0.9`, чтобы `/health` совпадал с текущей release-линей;
  - `version.json` переведен на `latest_version=2.0.9`, `min_supported_version=2.0.9`, `mandatory=true`;
  - release/preflight/acceptance guard'ы переведены с `2.0.8` на `2.0.9`.
- GitHub release:
  - создан tag/release `v2.0.9`;
  - Windows workflow `27003534719` завершился успешно;
  - smoke-test `TakSklad.exe --smoke-import` прошел для onefile и onedir;
  - assets опубликованы: `TakSklad.exe`, `TakSklad-windows-x64.zip` и SHA-файлы.
- SHA:
  - `TakSklad.exe`: `10ea2376ec194dc87f6007fec8a476e9444fdb04aeb79352f399aa7aca70e8f4`;
  - `TakSklad-windows-x64.zip`: `e07f4ff712ebd962922cc25e43bba499886a0b9db0fb6b74ac2a84293d5f04c3`.

### Telegram KIZ export modes

- Причина: менеджеру нужны оба сценария выгрузки КИЗов:
  - по дате отгрузки, например если сегодня склад собирал за `08.06`;
  - по конкретным Excel-файлам, которые ранее были загружены в Telegram-бот.
- Backend:
  - `GET /api/v1/reports/kiz/source-files` теперь возвращает все загруженные source-файлы с прогрессом `scanned_blocks/planned_blocks`, `remaining_blocks` и `completed`;
  - готовые source-файлы по-прежнему выгружаются через `GET /api/v1/reports/kiz/source-file`;
  - `GET /api/v1/reports/kiz/dates` теперь показывает даты, где уже есть отсканированные КИЗы, и отдаёт прогресс по всей дате;
  - отчёт по дате/диапазону выгружает уже отсканированные завершённые позиции, даже если по этой дате есть ещё незавершённые позиции.
- Telegram:
  - кнопка `Выгрузка КИЗов` сначала показывает выбор режима: `По датам отгрузки` или `По загруженным Excel-файлам`;
  - список файлов показывает каждый загруженный файл и прогресс `сколько/сколько` блоков отпикано;
  - inline-кнопка выгрузки появляется только у готовых файлов, чтобы случайно не отправить неполный файл по source-file;
  - прямые команды `/kiz 08.06.2026` и `/kiz 04.06.2026 05.06.2026` сохранены.

### Returns source of truth and KIZ movement lifecycle

- Причина: первый боевой возврат мог записаться напрямую в Google Sheets и затем зеркалиться в Postgres без `order_returned`, без `confirmed_items` и без события `skladbot_return_request_create`. Отдельно глобальный unique на `scan_codes.code` не давал повторно отгрузить КИЗ после возврата.
- Desktop:
  - в backend-режиме список и lookup возвратов читаются из backend;
  - `mark_return_for_display()` больше не пишет Google-only возврат через `mark_return_order_in_gsheet`;
  - если у Google fallback-заказа нет `_backend_order_id`, возврат отклоняется понятной ошибкой;
  - legacy Google write fallback сохранен только когда backend read mode выключен.
- Google sync:
  - ручные возвратные колонки из Google больше не переводят заказ в `returned`;
  - такие строки пишутся в `audit_log` как `google_sheets_backend_sync_conflict`;
  - backend -> Google mirror после настоящего backend-возврата сохранен.
- Backend KIZ lifecycle:
  - добавлены таблицы `kiz_codes` и `kiz_movements`;
  - `scan_codes` теперь хранит события сканирования отгрузок и больше не имеет глобального unique по `code`;
  - один и тот же КИЗ блокируется для другой позиции, пока последний movement не `return`, `undo` или `reset`;
  - после backend-возврата по всем сканам заказа пишется movement `return`, и этот КИЗ можно снова сканировать в новую отгрузку как `re_outbound`;
  - неуспешный возврат не освобождает КИЗ.
- SQL/deploy:
  - добавлена миграция `backend/sql/002_kiz_movements.sql`;
  - миграция создает `kiz_codes`, `kiz_movements`, backfill-ит outbound movements для существующих `scan_codes`, return movements для уже returned-заказов и снимает `uq_scan_codes_code`;
  - `deploy/vds/apply_schema.sh` теперь применяет все SQL-файлы по порядку.
- Проверено:
  - `./.venv/bin/python -m unittest tests.test_backend_api_persistence tests.test_google_sheets_sync_worker tests.test_desktop_ui_contract tests.test_backend_skeleton` - 91 tests OK;
  - `./.venv/bin/python -m unittest discover tests` - 363 tests OK.

### Returns/KIZ lifecycle pre-merge and VDS readiness gate

- Цель: перед merge/push/deploy проверить, что backend остаётся source of truth, Google Sheets работает только как зеркало, возвраты создают SkladBot return event, а возвращённый КИЗ можно снова отгружать без старой ошибки global duplicate.
- Локальные проверки:
  - `./.venv/bin/python -m unittest discover tests` - 363 tests OK;
  - `./.venv/bin/python -m compileall -q backend/app src/taksklad tools main.py tests` - OK;
  - `npm run build` в `frontend` - OK, `tsc -b && vite build`;
  - `for f in deploy/vds/*.sh; do bash -n "$f"; done` - OK;
  - `docker compose --env-file deploy/vds/.env.example -f deploy/vds/docker-compose.yml config` - OK;
  - `docker compose --env-file deploy/traefik/.env.example -f deploy/traefik/docker-compose.yml config` - OK;
  - `./.venv/bin/python tools/release_preflight.py --verify-downloads --timeout 120` - `status=ok`, GitHub release assets SHA совпали;
  - `git diff --check` - OK.
- PostgreSQL migration dry-run:
  - fresh schema: `001_initial_schema.sql` + `002_kiz_movements.sql` применились в disposable Postgres;
  - legacy schema из `HEAD` с `uq_scan_codes_code` + seed data + `002_kiz_movements.sql` применились в disposable Postgres;
  - после legacy migration остался только `uq_kiz_codes_code`, returned-КИЗ получил latest movement `return`, active-КИЗ остался latest `outbound`.
- VDS readiness до deploy:
  - `ssh root@135.181.245.84 'cd /opt/taksklad/app && ./deploy/vds/acceptance_status.sh'` - общий `status=ok`;
  - `backend-api`, `frontend`, `postgres`, `telegram-worker`, `skladbot-worker`, `google-sheets-sync-worker` running;
  - `google_backend_sync.status=ok`, `field_mismatch_count=0`, `backend_active_orders=24`, `backend_active_items=50`;
  - `skladbot_coverage.status=ok`;
  - `telegram_menu.status=ok`.
- Ограничение:
  - локальный `./deploy/vds/acceptance_status.sh` без локального docker stack падает на `service "backend-api" is not running`; для онлайн-состояния использовать запуск на VDS через SSH.

### Returns/KIZ lifecycle VDS deploy and production return repair

- На VDS задеплоен коммит `bdc08b2` через штатный `rsync`, потому что `/opt/taksklad/app` не является Git checkout.
- Перед изменениями создан backup Postgres:
  - `/opt/taksklad/backups/postgres/taksklad-postgres-20260607T110107Z.sql.gz`.
- Применена схема:
  - `backend/sql/001_initial_schema.sql`;
  - `backend/sql/002_kiz_movements.sql`.
- Пересобраны и перезапущены:
  - `backend-api`;
  - `google-sheets-sync-worker`;
  - `skladbot-worker`;
  - `telegram-worker`.
- VDS migration check:
  - `kiz_codes=1517`;
  - `kiz_movements=1518`;
  - старый constraint `uq_scan_codes_code` снят;
  - новый constraint `uq_kiz_codes_code` есть.
- Полный тестовый прогон на VDS выполнен в отдельном временном `python:3.12-bookworm` контейнере с полными зависимостями:
  - `python -m unittest discover tests` - 363 tests OK.
- Онлайн-проверки после deploy:
  - `https://api.taksklad.uz/health` - 200 OK, backend `2.0.9`;
  - `https://taksklad.uz/` - 200 OK;
  - `./deploy/vds/acceptance_status.sh` на VDS - общий `status=ok`;
  - `google_backend_sync.status=ok`, `field_mismatch_count=0`, `backend_active_orders=24`, `backend_active_items=50`;
  - `skladbot_coverage.status=ok`;
  - `telegram_menu.status=ok`.
- Боевой возврат `WH-R-193081`:
  - заказ в DB уже был `returned`, но без `skladbot_return_request_create`;
  - КИЗ имел movement history `outbound -> return`, latest movement `return`;
  - создано idempotent pending event `skladbot_return_request_create`;
  - SkladBot API вернул `201 Created`;
  - создана возвратная заявка `WH-R-194284`, id `194284`;
  - pending event завершен `completed`, `failed=0`, `remaining=0`;
  - Google mirror обработал очередь: `pending_synced=2`, `pending_failed=0`;
  - повторная VDS acceptance после боевой операции осталась `status=ok`.
- Итог:
  - DB остается source of truth для возвратов и КИЗов;
  - Google Sheets работает как зеркало;
  - возвращенный КИЗ доступен для новой отгрузки через movement `re_outbound`, если последним движением был `return`.

### SkladBot request Telegram notifications

- Причина: SkladBot при создании заявок отправлял в Telegram только короткое сообщение `Новая заявка #WH-R создана`, без состава заявки.
- По новой схеме SkladBot API добавлен параметр `notify`, который включает Telegram-уведомление клиента при создании заявки.
- Изменено:
  - обычные `3PL отгрузка` заявки TakSklad отправляют в `POST /v1/requests` поле `notify: true`;
  - возвратные `Возврат 3PL` заявки TakSklad тоже отправляют `notify: true`;
  - retry/reconcile/idempotency логика не менялась.
- Проверено:
  - `python -m unittest tests.test_backend_skladbot_request_dry_run` - 21 tests OK;
  - `python -m unittest discover tests` - 363 tests OK;
  - `python -m compileall -q backend/app src/taksklad tools main.py tests` - OK;
  - `npm run build` в `frontend` - OK.

### Release 2.0.10 aggregate box KIZ rollout

- Причина: склад должен сканировать агрегационный КИЗ короба как `+50` блоков без ручного сканирования каждого блока, при этом SKU должен проверяться по коду.
- Изменено:
  - `APP_VERSION` desktop и backend поднят до `2.0.10`;
  - backend хранит для скана `scan_type` и `block_quantity` в `scan_codes.raw_payload`;
  - один агрегационный код короба закрывает `50` блоков;
  - backend и desktop отклоняют короб, если SKU кода не совпадает с текущей позицией;
  - backend и desktop отклоняют короб, если в позиции осталось меньше `50` блоков;
  - отчеты считают блоки с учетом `block_quantity`, а не только количества строк КИЗов;
  - SkladBot API throttling оставлен с безопасной задержкой между запросами.
- GitHub release:
  - создан `v2.0.10`;
  - GitHub Actions `Build Windows Release` загрузил `TakSklad.exe` и `TakSklad-windows-x64.zip`;
  - onefile и onedir smoke `--smoke-import` прошли в workflow.
- SHA:
  - `TakSklad.exe`: `947610bc0f3afef0c047f72c9a5f48dc0029bbfb12c86b6a8a5442ca8b9b70fa`;
  - `TakSklad-windows-x64.zip`: `54727d347b55294dc0b70ea1b5f3655d2ac7e421e0a6678785f8d0ed617eb770`.
- VDS:
  - перед деплоем создан backup Postgres `/opt/taksklad/backups/postgres/taksklad-postgres-20260609T114700Z.sql.gz`;
  - restore point `/opt/taksklad/restore_points/pre-2010-aggregate-release-20260609T114711Z`;
  - пересобраны `backend-api`, `frontend`, `telegram-worker`, `skladbot-worker`, `google-sheets-sync-worker`;
  - `https://api.taksklad.uz/health` вернул backend `2.0.10`.

### Google Archive grid expansion hotfix

- После релиза `2.0.10` VDS acceptance выявил blocker в Google mirror:
  - `Range ('Архив'!A986:AI1031) exceeds grid limits. Max rows: 985`.
- Причина: batch archive export писал в `Архив` за пределы текущего размера worksheet без предварительного `resize`.
- Исправлено:
  - `archive_backend_orders_rows()` расширяет worksheet перед batch append;
  - добавлен regression test со strict grid.
- Проверено:
  - `python -m unittest tests.test_backend_google_sheets_exporter` - 16 tests OK;
  - `python -m unittest discover -s tests` - 394 tests OK.

### Unit KIZ SKU validation

- Причина: desktop и backend проверяли SKU только у агрегационных коробов. Обычный Red-КИЗ мог быть принят в позицию Gold/Brown.
- Исправлено:
  - обычные GTIN-префиксы теперь распознаются как `brown`, `red`, `gold`;
  - desktop отклоняет wrong-SKU до локального backup и backend queue;
  - backend `POST /api/v1/scans` отклоняет wrong-SKU с `409` и не меняет `scan_codes`/`scanned_blocks`;
  - неизвестный GTIN для известной Chapman-позиции теперь считается ошибкой.
- Боевые данные:
  - найден один текущий wrong-SKU скан: `WH-R-195084`, позиция `Chapman Gold SSL 100\`20`, Red-КИЗ `0104006396053947217p"-30o933ZXHZKjx`;
  - backup сохранен локально: `/tmp/taksklad_wrong_sku_scans_20260609T125050Z.json`;
  - скан снят через backend `undo_scan`;
  - после очистки `wrong_sku_remaining=0`;
  - `WH-R-195084` снова имеет три пустые позиции `Brown`, `Gold`, `Red` со статусом `not_completed`.
- VDS:
  - перед заменой файлов создан restore point `/opt/taksklad/restore_points/pre-wrong-sku-validation-20260609T125100Z`;
  - пересобраны и перезапущены `backend-api`, `google-sheets-sync-worker`, `skladbot-worker`, `telegram-worker`;
  - live-проверка на Gold-позиции `WH-R-195084` с Red-КИЗом вернула `409 Scan product does not match order item`;
  - `./deploy/vds/acceptance_status.sh` вернул общий `status=ok`.
- Release:
  - `APP_VERSION` desktop и backend поднят до `2.0.11`;
  - создан GitHub release `v2.0.11`;
  - GitHub Actions `Build Windows Release` завершился успешно, onefile и onedir smoke прошли;
  - `TakSklad.exe`: `9427e26491634e2f4ad4ea2522edca1043a23e2b22c73604ef09e5d35d6821e2`;
  - `TakSklad-windows-x64.zip`: `9300e76dee46fb19f2666638ba5b8e7629c9ad3f192bd98a3a093dcef7db4364`;
  - public `version.json` переведен на forced rollout `2.0.11`.
- Проверено:
  - `python -m unittest tests.test_scan_quantities tests.test_desktop_ui_contract.DesktopUiContractTests.test_scan_rejects_wrong_sku_before_local_backup_and_backend_queue tests.test_backend_api_persistence.BackendApiPersistenceTests.test_scan_create_rejects_unit_kiz_for_wrong_chapman_product tests.test_backend_api_persistence.BackendApiPersistenceTests.test_scan_create_rejects_aggregate_box_for_wrong_product tests.test_backend_api_persistence.BackendApiPersistenceTests.test_scan_undo_subtracts_aggregate_box_block_quantity` - 10 tests OK;
  - `python -m unittest tests.test_release_preflight tests.test_vds_acceptance_scripts tests.test_windows_test_build_helper tests.test_app_updates tests.test_startup_check` - 21 tests OK;
  - `python -m unittest discover -s tests` - 399 tests OK;
  - `python -m py_compile main.py src/taksklad/*.py backend/app/*.py` - OK.

### Release 2.0.12 non-blocking desktop errors

- Причина: на складе ошибки сканирования и синхронизации открывались через модальное окно `OK`, из-за чего оператор не мог продолжать пикать без ручного закрытия окна.
- Изменено:
  - `show_error()` больше не открывает `messagebox.showerror`;
  - ошибки показываются в нижней статусной полосе красным цветом и исчезают через 5 секунд;
  - критические ошибки по-прежнему пишутся в лог и отправляются в Telegram с документами, но не блокируют UI модальным `OK`;
  - старые `showwarning/showinfo` для “нет заказов”, импорта и закрытия смены заменены на нижние уведомления;
  - подтверждения `askyesno` оставлены, потому что там нужен выбор пользователя;
  - release guard поднят на `2.0.12`.
- Проверено до релиза:
  - `python -m unittest tests.test_desktop_ui_contract tests.test_scan_quantities tests.test_release_preflight tests.test_vds_acceptance_scripts tests.test_windows_test_build_helper` - 46 tests OK;
  - `python -m py_compile main.py src/taksklad/*.py backend/app/*.py tools/release_preflight.py` - OK.
- Release:
  - `APP_VERSION` desktop и backend поднят до `2.0.12`;
  - создан GitHub release `v2.0.12`;
  - GitHub Actions `Build Windows Release` завершился успешно, onefile и onedir smoke прошли;
  - `TakSklad.exe`: `f8e1cd72c10085b897f74edc216a0387957345b4624c85a2be820ac58a34c560`;
  - `TakSklad-windows-x64.zip`: `0690ae0e93158dcb4d4c4eb36ce19f520f36b4a98ef05853bf0072fed6ac7acd`;
  - public `version.json` подготовлен к forced rollout `2.0.12`.
- VDS:
  - перед обновлением создан backup Postgres `/opt/taksklad/backups/postgres/taksklad-postgres-20260609T132938Z.sql.gz`;
  - restore point: `/opt/taksklad/restore_points/pre-2012-nonblocking-errors-20260609T132945Z`;
  - синхронизированы `backend`, `deploy/vds`, `tools`, `version.json`;
  - пересобраны и перезапущены `backend-api`, `telegram-worker`, `skladbot-worker`, `google-sheets-sync-worker`;
  - `https://api.taksklad.uz/health` вернул backend `2.0.12`;
  - VDS `./deploy/vds/acceptance_status.sh` вернул общий `status=ok`;
  - свежие логи после рестарта без `ERROR/Traceback/Exception`.
- Финальный preflight:
  - `python tools/release_preflight.py --verify-downloads --timeout 30` - `status=ok`, оба release artifact SHA совпали.

### Desktop backend queue unblock fix

- Боевой симптом из лога `TakSklad (87).log`:
  - `pending_backend_events=1` переживал перезапуск приложения;
  - каждое сохранение позиции падало с `Backend не принял все КИЗы позиции. Осталось в очереди: 1`;
  - склад видел это как “КИЗ вводится и исчезает” и “Следующая позиция ничего не делает”.
- Причина:
  - `next_product()` блокировал текущую позицию из-за любого старого события в общей backend-очереди;
  - финальные `409` по scan-событиям (`wrong SKU`, `code already scanned in another order item`, неверный короб/превышение остатка) ретраились бесконечно;
  - при выборе заказа desktop начинал с первой позиции, даже если backend уже вернул ее полностью отпиканной.
- Исправлено:
  - финальные scan-конфликты удаляются из retry-очереди и возвращаются как `blocked_events`;
  - сохранение позиции блокируется только если проблема относится к текущему `order_item_id`;
  - завершение заказа блокируется только если проблема относится к текущим позициям/заказам;
  - при выборе заказа приложение открывает первую незавершенную позицию, а уже сохраненные позиции добавляет в сводку;
  - уже записанные backend-КИЗы не переочередятся повторно при переходе;
  - ошибки дополнительно показываются рядом с полем скана, чтобы оператор видел причину даже если нижняя статусная строка не заметна.
- Проверено:
  - `python -m unittest tests.test_backend_bridge tests.test_desktop_ui_contract` - 41 tests OK;
  - `python -m unittest discover tests` - 408 tests OK;
  - `python -m compileall -q src backend tests` - OK.

### Release 2.0.14 backend scan conflict rollback

- Боевой симптом из лога `TakSklad (89).log`:
  - после обновления до `2.0.13` старая backend-очередь очистилась;
  - новый конфликт `Backend HTTP 409: Code already scanned in another order item` по текущей позиции показывался как критическая ошибка приложения `КИЗы не записаны`;
  - спорный КИЗ уже оставался в локальном списке текущей позиции, поэтому повторное сохранение снова ловило тот же `409`.
- Причина:
  - backend мог обнаружить дубль позднее, чем desktop принял локальный скан;
  - `blocked_events` удалялись из retry-очереди, но не откатывали текущий `scanned_codes`;
  - `next_product()` превращал рабочий отказ backend в критическую ошибку с отправкой в Telegram.
- Исправлено:
  - `blocked_events` текущего `order_item_id` теперь обрабатываются как обычный отказ скана;
  - конфликтный КИЗ удаляется из текущей позиции, прогресс уменьшается, кнопки перехода/завершения блокируются до досканирования;
  - оператор видит понятное сообщение `КИЗ уже использован в другой позиции. Сканируйте другой код`;
  - фоновая backend-синхронизация тоже откатывает конфликтный КИЗ, если поймала его до нажатия `Следующая позиция`;
  - критическая Telegram-ошибка для этого штатного `409` больше не отправляется.
- Проверено:
  - `python -m unittest tests.test_desktop_ui_contract tests.test_backend_bridge` - 43 tests OK;
  - `python -m unittest discover tests` - 411 tests OK;
  - `python -m compileall -q src backend tests` - OK.

### Telegram bot UX cleanup

- Причина: у части пользователей в Telegram оставались старые нижние reply-кнопки, часть кнопок была навязчивой на телефоне, а на desktop-клиенте могла не отображаться.
- Изменено:
  - добавлена команда `/menu` с inline-меню основных действий;
  - `/start`, `/help` и `/menu` явно скрывают старую нижнюю клавиатуру через `remove_keyboard`;
  - основные действия вынесены в inline-кнопки: логистика, выгрузка КИЗов, статус, последние импорты, дата отгрузки;
  - старые текстовые кнопки оставлены совместимыми, чтобы старые Telegram-клиенты не ломались;
  - неизвестные сообщения и устаревшие callback-кнопки теперь открывают свежее меню вместо тупика.
- Проверено:
  - `.venv/bin/python -m unittest tests.test_backend_telegram_import` - 49 tests OK;
  - `.venv/bin/python -m unittest discover -s tests` - 415 tests OK;
  - `.venv/bin/python -m compileall -q backend tests src taksklad main.py` - OK.

### SkladBot shortage auto-cancel

- Причина: если SkladBot отказывает в создании заявки из-за нехватки остатка, заказ уже успевал попасть в backend, Google mirror и приложение склада, хотя в WMS заявки нет.
- Изменено:
  - Telegram import сохраняет `telegram_chat_id` в `ImportJob.raw_payload`;
  - при shortage-ошибке SkladBot unscanned-заказ удаляется из `orders/order_items`;
  - pending Google import очищается до записи, а если строка уже попала в `data`, ставится `google_sheets_delete_import_records_export`;
  - бот ставит queued Telegram-уведомление в чат импорта: заказ отменен из-за недостатка товара;
  - если у заказа уже есть сканы, автодаление запрещено, заказ остается в статусе `create_failed` для ручной проверки;
  - ошибки SkladBot HTTP 4xx/5xx теперь сохраняют текст ответа API, чтобы `422` с нехваткой товара распознавался как shortage, а не как общий HTTP-сбой.
- Проверено:
  - `.venv/bin/python -m unittest tests.test_backend_skladbot_request_dry_run` - 23 tests OK;
  - `.venv/bin/python -m unittest tests.test_backend_google_sheets_exporter tests.test_backend_google_sheets_pending` - 24 tests OK;
  - `.venv/bin/python -m unittest tests.test_backend_telegram_import` - 50 tests OK;
  - `.venv/bin/python -m unittest tests.test_backend_skladbot_worker tests.test_backend_skladbot_request_dry_run` - 72 tests OK;
  - `.venv/bin/python -m unittest discover -s tests` - 420 tests OK;
  - `.venv/bin/python -m compileall backend` - OK.

### Telegram KIZ menu recent limit

- Причина: в Telegram-меню выгрузки КИЗов копились старые даты и Excel-файлы, создавая визуальный шум.
- Изменено:
  - меню `Выгрузка КИЗов -> По датам отгрузки` показывает только 7 последних дат;
  - меню `Выгрузка КИЗов -> По загруженным Excel-файлам` показывает только 7 последних файлов по датам отгрузки;
  - старые данные из backend не удаляются, прямые команды `/kiz ДД.ММ.ГГГГ` и `/kiz ДД.ММ.ГГГГ ДД.ММ.ГГГГ` продолжают работать.
- Проверено:
  - `.venv/bin/python -m unittest tests.test_backend_telegram_import` - 52 tests OK;
  - `.venv/bin/python -m unittest discover -s tests` - 422 tests OK;
  - `.venv/bin/python -m compileall backend` - OK.

### Telegram manual order control

- Причина: нужен ручной ввод заказа через Telegram и ручное удаление только активных заказов без риска потерять уже начатую складом обработку.
- Изменено:
  - добавлено меню `Ручное управление`;
  - ручное создание заказа пошагово спрашивает дату отгрузки, тип оплаты, юрлицо, адрес/координаты, торгового представителя, SKU и блоки;
  - ручной заказ отправляется в обычный backend import pipeline с `source=telegram_manual`, чтобы дальше работали Google mirror и SkladBot;
  - добавлен backend endpoint `/api/v1/admin/orders/{order_id}/delete-active`;
  - удаление активного заказа разрешено только если по нему нет сканов КИЗов;
  - если склад уже начал обработку, Telegram блокирует удаление, а backend повторно проверяет это под транзакцией;
  - при удалении backend удаляет заказ из активной БД и ставит событие удаления строк из Google Sheets; SkladBot-заявку нужно удалить вручную.
- Проверено:
  - `.venv/bin/python -m unittest tests.test_backend_api_persistence.BackendApiPersistenceTests.test_delete_active_order_removes_unscanned_order_and_queues_google_delete tests.test_backend_api_persistence.BackendApiPersistenceTests.test_delete_active_order_rejects_order_with_scans tests.test_backend_telegram_import.BackendTelegramImportTests.test_telegram_worker_handles_main_menu_callbacks tests.test_backend_telegram_import.BackendTelegramImportTests.test_telegram_worker_manual_add_order_imports_through_backend tests.test_backend_telegram_import.BackendTelegramImportTests.test_telegram_worker_manual_delete_active_order_calls_safe_backend_endpoint tests.test_backend_telegram_import.BackendTelegramImportTests.test_telegram_worker_manual_delete_refuses_started_order_before_backend_call` - OK;
  - `.venv/bin/python -m unittest discover -s tests` - 427 tests OK;
  - `.venv/bin/python -m compileall backend src tests` - OK.

### Desktop main.py modular refactor

- Причина: `src/taksklad/main.py` снова вырос до нескольких тысяч строк после UI/складских доработок, хотя приложение уже дробили на owner-модули.
- Изменено:
  - `main.py` оставлен для сборки приложения, startup wiring и `ScanningApp.__init__`;
  - display, scan, finish, layout, refresh, runtime и returns workflow разнесены по `app_*`, `desktop_*` и `backend_flow.py`;
  - добавлен guard `tests/test_code_organization.py`, который ограничивает `main.py` 500 строками и запрещает возвращать workflow-методы прямо в `ScanningApp`;
  - финальная карта ownership записана в `docs/main-refactor-inventory.md`.
- Правило на будущее: существенную новую workflow-логику не добавлять в `main.py` без отдельного documented extraction rationale.

### Telegram system menu button

- Причина: в Telegram нужна штатная кнопка меню рядом с полем ввода, а не отдельная текстовая команда `Призвать кнопки`.
- Изменено:
  - команда `/buttons` удалена;
  - публичные команды Telegram очищаются через `deleteMyCommands`, чтобы синяя кнопка `Меню` не занимала место рядом с полем ввода;
  - `setChatMenuButton` оставлен в `type=default`;
  - `/menu` и `/start` отправляют обычный reply-keyboard с основными действиями TakSklad без `is_persistent`, чтобы Telegram показывал кнопку скрытия клавиатуры;
  - live verifier Telegram menu проверяет, что публичных команд нет.

### Failed import incident control

- Причина: кривой Telegram/Excel import должен быть виден оператору, закрываться с причиной и не держать `/ready` в degraded после ручной проверки.
- Изменено:
  - backend import со статусом `failed` или `completed_with_errors` автоматически создает linked incident;
  - Telegram import передает `telegram_event_id` в backend, чтобы incident связывался с исходным pending event;
  - failed `telegram_excel_import` без созданного import тоже получает incident по pending event;
  - `/ready` игнорирует failed import/event, если по нему есть terminal incident `resolved`, `ignored` или `cancelled`;
  - retry `telegram_excel_import` запрещен, если в событии нет исходного `document.file_id`;
  - Telegram failure message показывает файл, причину и следующее действие, с редактированием секретов.

### Admin table pagination for large order volume

- Причина: web/admin UI запрашивал `admin/table?limit=5000` и не показывал оператору, что список может быть больше загруженных строк.
- Изменено:
  - backend `/api/v1/admin/table` принимает `offset` вместе с `limit`;
  - response содержит `limit`, `offset`, `row_count`, `total_rows`, `has_more`;
  - `totals` по-прежнему считаются по всей таблице, а `rows` возвращаются страницей;
  - frontend показывает `Показано X из Y загруженных · всего Z` и кнопку `Загрузить еще`;
  - пока все страницы не загружены, UI явно сообщает, что фильтры применяются к загруженным строкам.
- Проверено:
  - `.venv/bin/python -m unittest tests.test_backend_api_persistence.BackendApiPersistenceTests.test_admin_table_totals_are_not_limited_by_row_limit tests.test_backend_api_persistence.BackendApiPersistenceTests.test_admin_table_supports_offset_pagination_metadata` - 2 tests OK;
  - `.venv/bin/python -m py_compile backend/app/admin_service.py backend/app/main.py backend/app/schemas.py` - OK;
  - `npm run build` в `frontend` - OK.

### SkladBot create failure incidents

- Причина: отказ SkladBot при создании заявки, особенно из-за недостатка остатка, должен быть виден в админке и не попадать на склад как обычный активный заказ.
- Изменено:
  - failures `skladbot_request_create` создают linked incident с order/import/event/source file/SKU;
  - shortage без сканов продолжает удалять заказ из активной БД, ставит Google cleanup и Telegram notification;
  - shortage со сканами не удаляется автоматически, получает incident `manual_review` и остается для ручного решения;
  - логистический отчет явно исключает shortage/create_failed заказы, даже если заказ остался в БД;
  - admin retry для failed `skladbot_request_create` остается через event queue и не требует Telegram source file.

### Return flow hardening

- Причина: возврат должен быть проверяемым путем повторного использования КИЗа, а оператору нужно видеть исходную отгрузку и заявку возврата SkladBot в админке.
- Изменено:
  - admin table API теперь отдает `skladbot_return_request_number`, `skladbot_return_request_id`, `skladbot_return_status`;
  - web-panel в колонке SkladBot для возвратов показывает исходную WH-R и отдельную строку по заявке возврата;
  - return smoke test закрепляет lookup по WH-R/request id, duplicate-return rejection, отчетные поля `/returns` и очередь заявки возврата;
  - отдельный admin test проверяет, что returned-order виден с linked order, количеством КИЗов и статусом return-заявки SkladBot.
- Не изменено:
  - старые исторические возвраты не backfill-ятся автоматически;
  - исходные `scan_codes` и audit history отгрузки остаются историей, а повторная отгрузка разрешается только через `return` movement.

### Daily reconciliation and deduped alerts

- Причина: расхождения DB, Google mirror и SkladBot должны находиться системой до того, как склад или оператор заметит их вручную.
- Изменено:
  - добавлен DB-first reconciliation service для ежедневной сверки по дате отгрузки;
  - endpoint `/api/v1/reports/reconciliation/day` возвращает сверку из Postgres как source of truth;
  - Google-only rows, DB-only active items, status mismatch и WH-R mismatch считаются отдельными счетчиками;
  - SkladBot gaps агрегируются по заказам без usable WH-R/status, без per-row Telegram spam;
  - critical incidents ставят deduped Telegram notification events с idempotency по incident/date/source/chat;
  - Google failure записывается как mirror issue и не переводит DB workflow в failed;
  - scheduled 22:00 SkladBot daily report запускает reconciliation для того же конфигурируемого чата.
- Проверено:
  - `.venv/bin/python -m unittest tests.test_reconciliation_service tests.test_backend_api_persistence.BackendApiPersistenceTests.test_reconciliation_report_endpoint_is_db_first_and_does_not_alert tests.test_backend_api_persistence.BackendApiPersistenceTests.test_reconciliation_report_endpoint_records_google_down_as_mirror_issue tests.test_skladbot_daily_report.SkladBotDailyReportTests.test_scheduled_report_runs_reconciliation_for_configured_chat` - 6 tests OK.

### Release dependency hardening

- Причина: `npm audit --audit-level=high` показывал high vulnerability в `esbuild 0.27.x`, который попадал через frontend build chain.
- Изменено:
  - frontend обновлен до `vite 8.0.16`;
  - `esbuild 0.28.1` закреплен как dev dependency, совместимый с optional peer range Vite 8;
  - production build остается через `node:22-alpine` в Dockerfile.
- Проверено:
  - `npm --prefix frontend audit --audit-level=high` - 0 vulnerabilities;
  - `npm --prefix frontend run build` - OK.

### Release GO/NO-GO status

- Automated release gates: GO.
- Production deploy status: NO-GO до заполнения ручного `outputs/taksklad_acceptance/ACCEPTANCE_RESULTS.md`.
- Версионность:
  - `TakSklad 2.0.16 Operations Control` - имя текущего roadmap/update cycle;
  - `version.json` пока остается на `2.0.15`, потому новый Windows rollout artifact не выпускался в этом проходе;
  - переключать manifest на новую desktop-версию можно только после отдельной сборки, SHA-проверки и ручной acceptance.
- Причина NO-GO:
  - `tools/release_go_no_go.py` требует подтвержденные чекбоксы Telegram Import, SkladBot Matching и Windows Desktop Acceptance;
  - файл acceptance results сейчас оставлен в состоянии NO-GO, чтобы случайно не выдать production deploy без ручной приемки.
- Это не блокирует кодовый release candidate, но блокирует честный production deploy без отдельного acceptance прохода.

### Returned orders import and logistics isolation

- Причина: если ошибочный заказ уже собрали, затем провели через `Возврат`, повторная загрузка исправленного Excel должна создать новую активную сборку, а не приклеиваться к старому returned-заказу и не считаться дублем.
- Изменено:
  - backend import больше не использует `returned` orders/items/source_import_id как существующие ключи дедупликации;
  - повторный import после возврата создает новый активный заказ и новую позицию при том же бизнес-ключе;
  - логистический отчет и список дат логистики явно исключают `returned`-заказы, включая случаи до выгрузки отчета;
  - старый returned-заказ остается в БД/архиве как история возврата и не участвует в новой операционной реальности склада.
- Проверено:
  - `.venv/bin/python -m unittest tests.test_backend_api_persistence.BackendApiPersistenceTests.test_import_after_return_creates_new_active_order_instead_of_duplicate tests.test_backend_api_persistence.BackendApiPersistenceTests.test_duplicate_backend_import_still_can_backfill_google_sheets tests.test_backend_api_persistence.BackendApiPersistenceTests.test_logistics_report_excludes_returned_orders tests.test_backend_api_persistence.BackendApiPersistenceTests.test_logistics_report_uses_shipment_date_coordinates_and_prices` - 4 tests OK;
  - `.venv/bin/python -m unittest tests.test_backend_api_persistence` - 99 tests OK;
  - `.venv/bin/python -m unittest tests.test_backend_skladbot_request_dry_run` - 27 tests OK;
  - `.venv/bin/python -m py_compile backend/app/imports_service.py backend/app/logistics_service.py tests/test_backend_api_persistence.py` - OK.
- VDS deploy:
  - restore point: `/opt/taksklad/restore_points/pre-returned-import-isolation-20260625T092756Z`;
  - Postgres backup: `/opt/taksklad/backups/postgres/taksklad-postgres-20260625T092756Z.sql.gz`;
  - на VDS синхронизированы только `backend/app/imports_service.py` и `backend/app/logistics_service.py`;
  - выполнен `docker compose --env-file deploy/vds/.env -f deploy/vds/docker-compose.yml up -d --build backend-api`;
  - VDS `backend-api` compileall - OK;
  - `https://api.taksklad.uz/health` - OK, backend `2.0.23`;
  - `https://api.taksklad.uz/ready` - DB/migrations OK, общий `degraded` из-за старых queue events;
  - production preview того же кейса вернул `rows_importable=6`, `orders_new=2`, `items_new=6`, `duplicate_rows=0`;
  - `acceptance_status.sh` остался `failed` из-за старого Google/backend sync mismatch по `ASADBEK GOLD BIZNES / Chapman RED OP 20` и незакрытых ручных GO/NO-GO чекбоксов, не из-за deploy health.

### Erroneous returned-order import cleanup

- Причина: до фикса `returned`-заказы участвовали в дедупликации, поэтому Telegram import `afc07b59-d2e1-47d6-9a3e-e9c692c2cab3` добавил 5 позиций в 2 старых `returned`-заказа вместо создания новой активной сборки.
- Перед удалением:
  - restore point: `/opt/taksklad/restore_points/pre-delete-second-import-20260625T093332Z`;
  - Postgres backup: `/opt/taksklad/backups/postgres/taksklad-postgres-20260625T093332Z.sql.gz`;
  - проверено, что по этим 5 позициям нет `scan_codes`, `kiz_movements` и incidents.
- Удалено:
  - 5 `order_items`, созданных import `afc07b59-d2e1-47d6-9a3e-e9c692c2cab3`;
  - import row, linked `import_files`, 3 linked `pending_events`, related `audit_log`;
  - 6 строк Google Sheets: 1 строка из `data` и 5 строк из `Архив`.
- Проверено после удаления:
  - `order_items`, `imports`, `import_files`, linked `pending_events` по второму import = 0;
  - в Google Sheets совпадений по source file/import id = 0;
  - старые returned-заказы остались как архив возврата: `WH-R-200667` и `WH-R-200666`, по 3 исходные позиции в каждом;
  - прямой Google/backend sync по активным позициям после удаления: `status=ok`, `backend_active_items=96`, `backend_missing_sheet=[]`, `sheet_missing_backend=[]`.

### Web dashboard loaded-day metrics

- Причина: верхние карточки `Информация за день` брали данные из `GET /api/v1/reports/day`, а этот endpoint считает отчет по дате отгрузки плюс заказы, сканированные в выбранный день. Для оператора web-панели это смешивало загруженные сегодня заказы с заказами другой даты.
- Изменено:
  - добавлен read-only endpoint `GET /api/v1/admin/dashboard/day-summary`;
  - верхние карточки web-панели теперь берут summary из нового endpoint;
  - `Всего заказов` считает уникальные операционные заказы, у которых есть позиции, загруженные в выбранный день;
  - `Всего блоков` считает все блоки в загруженных в этот день позициях, включая готовые и активные;
  - `Отскан. блоков` считает текущий прогресс сканирования по этим же позициям;
  - возвраты, отмены, archive-without-KIZ и `removed_from_google_sheet` строки не попадают в dashboard summary.
- Не изменено:
  - `GET /api/v1/reports/day` оставлен в старой семантике для отчетов и Telegram status;
  - вкладка `Отчет` продолжает использовать старый day report.
- Проверено:
  - `.venv/bin/python -m unittest tests.test_backend_api_persistence.BackendApiPersistenceTests.test_dashboard_day_summary_counts_loaded_items_not_shipment_or_scan_date tests.test_backend_api_persistence.BackendApiPersistenceTests.test_day_report_counts_scan_by_business_timezone tests.test_backend_api_persistence.BackendApiPersistenceTests.test_complete_order_requires_required_blocks_and_closes_order` - 3 tests OK;
  - `.venv/bin/python -m unittest tests.test_backend_api_persistence` - 100 tests OK;
  - `.venv/bin/python -m unittest tests.test_backend_telegram_import` - 65 tests OK;
  - `.venv/bin/python -m py_compile backend/app/reports_service.py backend/app/main.py backend/app/schemas.py` - OK;
  - `npm --prefix frontend run build` - OK.
- VDS deploy:
  - restore point: `/opt/taksklad/restore_points/pre-dashboard-loaded-day-metrics-20260625T100959Z`;
  - Postgres backup: `/opt/taksklad/backups/postgres/taksklad-postgres-20260625T100959Z.sql.gz`;
  - синхронизированы `backend/app/main.py`, `backend/app/reports_service.py`, `backend/app/schemas.py`, `frontend/src/App.tsx`, `frontend/src/api.ts` и docs;
  - выполнен `docker compose --env-file deploy/vds/.env -f deploy/vds/docker-compose.yml up -d --build backend-api frontend`;
  - VDS `backend-api` compileall - OK;
  - `https://api.taksklad.uz/health` - OK, backend `2.0.23`;
  - `https://api.taksklad.uz/ready` - DB/migrations OK, общий `degraded` из-за старых queue events;
  - live `GET /api/v1/admin/dashboard/day-summary?report_date=2026-06-25` вернул `source=postgres_loaded_items`, `orders=69`, `active_orders=1`, `planned_blocks=668`, `scanned_blocks=613`;
  - live `GET /api/v1/orders/active` вернул 1 активный заказ `POYTAXT SPECIAL TRADE MCHJ Ozbekfilm`, 55 блоков, 0 отсканировано;
  - новый frontend asset: `/assets/index-yntfbHmQ.js`;
  - `acceptance_status.sh` остался `failed` из-за старого readiness `degraded` и незакрытых ручных GO/NO-GO чекбоксов, не из-за dashboard deploy.

### Desktop Telegram UI theme import fix

- Причина: Telegram import UI в desktop-клиенте использовал `BG_MAIN` и `FG_MUTED` в `src/taksklad/app_telegram.py`, но модуль не импортировал эти константы из `config.py`. Из-за этого Windows-клиент отправлял Telegram alert `name 'BG_MAIN' is not defined`.
- Изменено:
  - в `src/taksklad/app_telegram.py` добавлены недостающие импорты `BG_MAIN` и `FG_MUTED`.
- Проверено:
  - `.venv/bin/python -m py_compile src/taksklad/app_telegram.py` - OK;
  - `.venv/bin/python -m unittest tests.test_desktop_ui_contract` - 46 tests OK.

### Desktop Telegram polling disabled in backend mode

- Причина: после перехода Telegram Excel import на серверный `telegram-worker` legacy polling внутри desktop-клиента мог прочитать тот же Telegram-документ. В результате backend уже создавал заказ, web-panel и SkladBot показывали созданную заявку, а desktop после сетевого timeout отправлял ложное сообщение `Не удалось импортировать Excel-файл`.
- Изменено:
  - добавлен флаг `TELEGRAM_DESKTOP_POLLING_ENABLED`;
  - default: desktop Telegram polling выключен, если включен `TAKSKLAD_BACKEND_ENABLED`;
  - `src/taksklad/app_telegram.py` больше не запускает `getUpdates`, когда desktop polling отключен;
  - legacy fallback можно вернуть явно через `TELEGRAM_DESKTOP_POLLING_ENABLED=true`.
- Инвариант: импорт заказов, backend `/api/v1/imports`, SkladBot create и данные заказов не менялись.

### Telegram worker import timeout confirmation

- Причина: серверный `telegram-worker` мог получить read timeout после `POST /api/v1/imports`, хотя backend уже успел закоммитить импорт, создать заказы и поставить SkladBot create event. Старый текст ошибки в таком случае ложно писал, что заказы и заявки SkladBot не созданы.
- Изменено:
  - добавлен `TELEGRAM_WORKER_IMPORT_TIMEOUT_SECONDS` с default 120 секунд для `/api/v1/imports`;
  - при timeout после backend import worker делает read-back через `/api/v1/imports` и ищет импорт по `telegram_event_id`;
  - если импорт найден, Telegram получает обычное успешное сообщение с пометкой, что результат подтверждён через историю импортов;
  - если импорт не найден или read-back недоступен, Telegram получает сообщение `Не удалось подтвердить импорт`, без утверждения, что заказы не созданы, и с указанием не отправлять файл повторно до проверки web-панели.
- Проверено:
  - `.venv/bin/python -m unittest tests.test_backend_telegram_import` - 67 tests OK;
  - `.venv/bin/python -m unittest tests.test_desktop_ui_contract` - 48 tests OK;
  - `.venv/bin/python -m py_compile backend/app/telegram_worker.py tests/test_backend_telegram_import.py` - OK;
  - `git diff --check` - OK.
- VDS deploy:
  - commit: `c937b9b Fix Telegram import timeout false failures`;
  - Postgres backup: `/opt/taksklad/backups/postgres/taksklad-postgres-20260625T111244Z.sql.gz`;
  - restore point: `/opt/taksklad/restore_points/pre-telegram-import-timeout-fix-20260625T111304Z`;
  - synced runtime file: `backend/app/telegram_worker.py`, SHA256 `4f2818d55c72371fc5fabebcbf12affe7119d90953d42291b261ac3ac70ab2a7`;
  - `docker compose up -d --build telegram-worker` пересобрал и пересоздал `telegram-worker`; `backend-api` также был пересоздан compose-зависимостью;
  - VDS containers: `backend-api`, `telegram-worker`, `postgres`, `frontend`, `google-sheets-sync-worker`, `skladbot-worker` running;
  - `https://api.taksklad.uz/health` - OK, backend `2.0.23`;
  - `https://api.taksklad.uz/ready` - DB/migrations OK, общий `degraded` из-за старых failed queue events;
  - in-container `python -m py_compile /app/app/telegram_worker.py` - OK;
  - fresh `backend-api`/`telegram-worker` logs since deploy - no `error|traceback|exception|critical|failed`;
  - `./deploy/vds/acceptance_status.sh` остался `failed` из-за старого readiness `degraded` и незакрытых ручных GO/NO-GO чекбоксов, не из-за этого deploy.

### Smartup terminal auto import worker

- Причина: ручной процесс Smartup должен стать серверной автоматизацией по слотам `12:00`, `15:00`, `17:50`: выгрузка `Новые + Терминал`, создание заказов TakSklad по `delivery_date`, перевод заказов в `В ожидании`, постановка SkladBot-заявок и финальный логистический отчёт.
- Изменено:
  - добавлен `backend/app/smartup_auto_import.py` с Smartup client, локальным XLSX/audit export, backend preview, safety-gates, status change, import grouping by `delivery_date`, SkladBot queue hook и отправкой logistics report;
  - добавлен `backend/app/smartup_auto_import_worker.py` с расписанием и idempotency через `pending_events`;
  - добавлен compose service `smartup-auto-import-worker`, выключенный по умолчанию;
  - добавлены env-флаги `SMARTUP_AUTO_IMPORT_*`;
  - добавлены unit-тесты `tests/test_smartup_auto_import.py`.
- Инварианты:
  - по умолчанию worker не запускает автоматизацию;
  - backend import невозможен без включённого Smartup status change gate;
  - дата создания заказов TakSklad берётся из Smartup `delivery_date`;
  - имя файла выгрузки использует сегодняшнюю дату заказа/выгрузки: `Терминал ДД.ММ.ГГГГ Часть N.xlsx`;
  - реальные SkladBot-заявки по-прежнему зависят от `SKLADBOT_CREATE_REQUESTS_MODE=enabled`.

### Smartup auto import hardening

- Причина: после первого боевого теста нужен безопасный повторяемый контур эксплуатации: защита от параллельных workers, ручной запуск конкретного слота, Telegram alert при падении и видимость истории в web-admin.
- Изменено:
  - `run_scheduled_smartup_auto_import_slot` теперь берёт Postgres advisory lock на пару `export_date + slot` через отдельное DB connection и сохраняет существующий idempotency через `pending_events`;
  - добавлен `python -m app.smartup_auto_import_worker run-once --date YYYY-MM-DD --slot HH:MM`;
  - при ошибке слота событие помечается `failed`, пишется audit и отправляется Telegram alert в `SMARTUP_AUTO_IMPORT_ALERT_CHAT_ID` или logistics chat;
  - добавлен read-only endpoint `/api/v1/admin/smartup-auto-imports/history`;
  - в web-admin добавлена вкладка `Smartup` с последними запусками, файлами, ошибками, количеством созданных заказов, SkladBot/logistics статусами.
- Инварианты:
  - `run-once` использует тот же claim/lock путь, что и worker;
  - history endpoint не показывает Smartup/Telegram секреты;
  - сама обработка заказов осталась в прежнем `run_smartup_auto_import_once`.

### Smartup auto import weekend guard

- Причина: Smartup/складской процесс не работает в субботу и воскресенье, автоматические слоты не должны выгружать заказы в эти дни.
- Изменено:
  - добавлен `SMARTUP_AUTO_IMPORT_DISABLED_WEEKDAYS`, default `5,6`;
  - `run_due_smartup_auto_imports` возвращает `idle / weekday_disabled` до проверки слотов и не создаёт `PendingEvent`;
  - `run-once` оставлен доступным как ручной override.

### Backend-only hot path Phase 1 baseline

- Цель этапа: зафиксировать текущее поведение перед переводом складского hot path с Google Sheets на backend/Postgres как основной источник.
- Изменения этапа: только тесты, документация и Supergoal-артефакты; runtime-логика приложения не менялась.
- Инцидент 2026-06-25 вечером:
  - `ERROR`: 25;
  - `WARNING`: 38;
  - traceback-записи: 35;
  - основные сигнатуры: Google Sheets `429 Quota exceeded`, backend SSL/handshake/read/write timeout, Telegram document send timeout;
  - 11 предупреждений по КИЗам являются бизнес-валидацией несоответствий, а не системной аварией;
  - отдельный desktop-баг установленного клиента `2.0.22`: `NameError: name 'WARNING' is not defined`; локальный код `2.0.23` уже содержит исправленный импорт.
- Dirty-tree на старте Phase 1:
  - in-scope: `docs/implementation-log.md`, `tests/test_backend_events.py`, `.supergoal/taksklad-backend-only-hot-path-productio-gyoopS/*`;
  - existing out-of-scope: `AGENTS.md`, `backend/app/main.py`, `backend/app/models.py`, `backend/app/schemas.py`, `backend/sql/001_initial_schema.sql`, `deploy/vds/.env.example`, `deploy/vds/docker-compose.yml`, `docs/changelog.md`, `docs/taksklad-system-stack-overview.md`, `docs/user-business-process-guide.md`, `frontend/src/App.tsx`, `frontend/src/api.ts`, `frontend/src/styles.css`, `tests/test_backend_api_persistence.py`, `tests/test_backend_skeleton.py`, `backend/app/logistics_calendar_service.py`, `backend/app/smartup_auto_import.py`, `backend/app/smartup_auto_import_history_service.py`, `backend/app/smartup_auto_import_worker.py`, `backend/migrations/versions/20260626_0005_logistics_calendar.py`, `tests/test_smartup_auto_import.py`.
- Инварианты, которые нельзя ломать следующими этапами:
  - backend/Postgres является source of truth для backend-mode заказов, сканов, завершений и очередей;
  - Google Sheets в backend-mode должен быть зеркалом/экспортом, а не скрытым fallback для складского сканирования;
  - Google `429` не должен блокировать сканирование, завершение заказа, Telegram import или backend read, но должен давать видимый mirror-lag/error;
  - Telegram import в штатном backend-mode должен иметь одного владельца: server worker; desktop polling допустим только как явный emergency fallback;
  - retryable backend timeout по scan/order_complete остается в pending-очереди и повторяется;
  - duplicate scan ack от backend считается идемпотентным успехом;
  - wrong-SKU, duplicate-other-order и переполнение агрегата остаются blocked/visible, не автопрячутся;
  - при refresh error desktop сохраняет текущую загруженную позицию, если она уже была безопасно загружена;
  - секреты, chat IDs, полные КИЗ-списки и клиентские payload не должны попадать в diagnostics/docs/log summaries.

### Smartup reverse geocode and Google mirror address guard

- Причина: Smartup orders без текстового адреса сначала импортировались как `GPS: lat,lng`; при создании SkladBot-заявок это попадало в поле адреса. После backfill Google mirror мог вернуть `GPS:` обратно в Postgres.
- Изменено:
  - Smartup import делает reverse geocode координат через существующий Yandex geocoder и использует `GPS:` только как fallback;
  - ошибка reverse geocode не роняет весь Smartup import, а оставляет fallback;
  - `GPS:` теперь считается missing-address marker в import/backfill и Google export;
  - Google-to-backend sync больше не перетирает реальный адрес в Postgres заглушкой `GPS:`/`Координаты`;
  - Smartup Telegram routing разделен: client chat получает только Smartup export files, logistics chat получает только финальный logistics report после слота `17:50`.
- Production recovery:
  - DB backup перед работами: `/opt/taksklad/backups/postgres/taksklad-postgres-pre-smartup-address-skladbot-20260630T114407Z.sql.gz`;
  - Smartup orders backfilled: 45/45, `orders.address like 'GPS:%'` = 0 после полного цикла sync;
  - SkladBot create events completed: 45/45, TakSklad orders linked: 45/45;
  - Smartup export file `Терминал 30.06.2026 Часть 1.xlsx` отправлен в client chat вручную после инцидента;
  - read-only SkladBot check после инцидента: 44 из 45 созданных заявок в самой WMS уже имеют `GPS:` в поле адреса; публичный update endpoint для правки созданных заявок не подтвержден.
- Проверено:
  - `PYTHONPATH=. ./.venv/bin/python -m unittest tests.test_smartup_auto_import tests.test_google_sheets_sync_worker ...` - 38 tests OK после Telegram routing split;
  - `py_compile` измененных модулей - OK;
  - `git diff --check` по измененным файлам - OK.

### SkladBot daily report completeness by movement date

- Причина: daily-отчет пропускал SkladBot-заявки, созданные раньше даты отчета, хотя по ним была выгрузка, завершение, архивирование или складское движение в отчетный день. Пример расследования: заявка `WH-R-200655` по Alcaber/Alkabir.
- Изменено:
  - daily сначала собирает `warehouse/transactions` за дату отчета и использует WH-R из движений как причину включения `движение склада`;
  - убран ранний отбор по `created_at` из списка заявок до загрузки detail;
  - заявки с движением склада проверяются первыми, чтобы старые записи в списке не съедали detail-limit;
  - выполненные архивные заявки включаются по `created_at`, `updated_at`, `unloading_date`, `completed_at`, `archived_at`;
  - старые выполненные архивные заявки без дат перехода включаются один раз как `впервые найдена выполненной`, если их еще нет в `pending_events` registry;
  - новое движение склада за дату отчета включает WH-R даже тогда, когда эта заявка уже попадала в старый daily.
- Инварианты:
  - SkladBot API используется read-only;
  - SkladBot, складские остатки и Google Sheets не изменяются;
  - `pending_events` продолжает защищать плановые отправки от повторов старых заявок без нового движения или новой отчетной даты события.
- Проверено:
  - `PYTHONPATH=. ./.venv/bin/python -m unittest tests.test_skladbot_daily_report` - 23 tests OK;
  - `./.venv/bin/python -m py_compile backend/app/skladbot_daily_report.py tests/test_skladbot_daily_report.py` - OK;
  - `PYTHONPATH=. ./.venv/bin/python -m unittest tests.test_daily_report tests.test_skladbot_daily_report` остановился на импорте `tkinter`: текущий Python без модуля `_tkinter`.

### Smartup import dedupe and repriced logistics totals

- Дата: 2026-06-30.
- Причина:
  - две разные строки Smartup с разными `ID импорта`, но одинаковыми клиентом, датой, координатами, товаром и количеством, схлопывались по fallback `item_key`;
  - Google Sheets sync перезаписывал импортированную `Сумма с переоценкой` расчетом `blocks * block_price`;
  - логистический отчет для переоцененной строки показывал цену блока из `block_price`, хотя итоговая сумма строки была ниже.
- Изменено:
  - `backend/app/imports_service.py` дедуплицирует строки по `source_import_id`, если он есть, и использует бизнес-ключ только для строк без source id;
  - импорт сохраняет `Сумма с переоценкой` как `imported_line_total` и ставит ее в `line_total` приоритетнее расчетной суммы;
  - `backend/app/google_sheets_sync_worker.py` сохраняет импортированную/табличную итоговую сумму и не откатывает ее к расчетной;
  - `backend/app/logistics_service.py` для отчета выводит цену блока из `line_total / quantity_blocks`, если итоговая сумма отличается от `block_price * quantity_blocks`.
- Production repair:
  - Postgres backup перед ручным ремонтом: `/opt/taksklad/backups/postgres/taksklad-postgres-pre-import-dedupe-repair-20260630T140832Z.sql.gz`, SHA256 `2313ef260d686db1e71091aae075cedee3692c48716f6daaa9ad55b501ecf7f9`;
  - добавлена пропущенная строка YASMINA `smartup:257984858:1541071310:1` через dry-run SkladBot mode, без создания новой заявки в SkladBot;
  - позиция KOMUNA `89581c37-16b9-45fe-afb5-a5eee222760d` проверена с `line_total=11675000`, `calculated_line_total=12000000`.
- Проверено:
  - `.venv/bin/python -m unittest tests.test_smartup_auto_import tests.test_google_sheets_sync_worker` - 41 tests OK;
  - targeted `tests.test_backend_api_persistence` import/logistics checks - OK;
  - production deploy commits: `bb1c083` и `e4bb370`;
  - backend container health внутри `vds-backend-api-1`: `{"status":"ok","service":"taksklad-backend","version":"2.0.24","environment":"staging"}`;
  - runtime SHA256 для `google_sheets_sync_worker.py` и `logistics_service.py` совпадает с локальными файлами;
  - финальный отчет `/Users/anton/Documents/Telegram/TakSklad_логистика_01.07.2026_FIXED.xlsx`;
  - финальная сверка `/Users/anton/Documents/Telegram/Сверка_логистика_01.07.2026_FIXED.xlsx`: source rows 223, logistics rows 223, missing 0, extra 0, conflicts 0, blocks 511, amount 122315000.

### Manual KIZ undo for completed terminal order

- Дата: 2026-07-01.
- Причина: Антон попросил удалить из production DB один ошибочный КИЗ `0104006396053978217MQP?9:93ZNVLLeYm`.
- Scope:
  - runtime host: `api.taksklad.uz`, app path `/opt/stacks/taksklad/app`;
  - order `69887b09-cd63-4508-ad36-74663d007d90`, item `b27b7528-7976-4b18-b366-ee776fd878be`;
  - source file `Терминал 30.06.2026 Часть 1.xlsx`, product `Chapman Brown OP 20`, SkladBot request ID `202354`.
- Backup:
  - affected rows snapshot: `/opt/stacks/taksklad/repair_evidence/kiz-0104006396053978217MQP9-undo-before-20260701T062050Z.json`;
  - SHA256 `94b2837b9e44983659b4855336563fd32b6c9418b0aacff87ed7a9a90cd78961`.
- Изменено:
  - удален ровно один `scan_codes` row `bded7360-5f63-4f88-a0e9-17bffe53cc6e`;
  - `kiz_codes` сохранен как реестр КИЗа;
  - записан `kiz_movements` movement `undo` `8cb3f422-0eeb-43df-aa05-9b91a8b0c79f`;
  - позиция пересчитана с `2/2 completed` на `1/2 not_completed`;
  - заказ переведен с `completed` на `not_completed`;
  - поставлены Google export events `google_sheets_restore_order_export` и `google_sheets_scan_export`.
- Проверено:
  - точный `scan_codes` count по КИЗу = 0;
  - последний movement по КИЗу = `undo`, `available_for_outbound=true`;
  - позиция: `scanned_blocks=1`, `quantity_blocks=2`, `status=not_completed`, расчет по оставшимся scan rows = 1;
  - заказ: `status=not_completed`;
  - оба Google export events завершились `completed`;
  - `https://api.taksklad.uz/health` - OK, backend `2.0.25`;
  - `backend/sql/preflight_phase3_invariants.sql` показал 0 дублей внутри одного order item и 0 дублей в `kiz_codes`; старые межпозиционные повторы в `scan_codes` остаются вне scope этой ручной операции.
