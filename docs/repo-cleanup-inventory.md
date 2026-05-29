# Инвентаризация Репозитория Перед Уборкой

Дата: 2026-05-29.

Цель: аккуратно отделить код от рабочих данных, секретов, логов, backup и release-артефактов. Ничего не удалять слепо. Всё сомнительное сначала переносить в локальный `archive/`, который не попадает в Git.

## Правило

- Код и документация остаются в Git.
- Секреты, рабочие JSON, логи, backup, отчёты и собранные архивы не попадают в Git.
- Старые файлы не удаляются сразу: сначала перенос в `archive/repo-cleanup-YYYYMMDD/`.
- Текущие рабочие файлы (`credentials.json`, `TakSklad_data.json`) не переносить без подтверждения, потому что локальный запуск может зависеть от них.
- Перед публикацией в GitHub отдельно проверить, что в документации нет реальных ключей, токенов, private key id, chat id и service account деталей.

## Код

Кодовая часть проекта после локальной структуризации:

- `src/taksklad/main.py`
- `src/taksklad/config.py`
- `src/taksklad/storage.py`
- `src/taksklad/sheets.py`
- `src/taksklad/excel_import.py`
- `src/taksklad/excel_normalizer.py`
- `src/taksklad/orders.py`
- `src/taksklad/catalog.py`
- `src/taksklad/app_telegram.py`
- `src/taksklad/app_updates.py`
- `src/taksklad/app_imports.py`
- `src/taksklad/app_catalog.py`
- `src/taksklad/app_control_panel.py`
- `src/taksklad/app_skladbot.py`
- `src/taksklad/app_printing.py`
- `src/taksklad/app_day_end.py`
- `src/taksklad/duplicate_codes.py`
- `src/taksklad/skladbot.py`
- `src/taksklad/skladbot_sync.py`
- `src/taksklad/geocoding.py`
- `src/taksklad/http_client.py`
- `src/taksklad/printing.py`
- `src/taksklad/pending_store.py`
- `src/taksklad/reports.py`
- `src/taksklad/ui_widgets.py`
- `src/taksklad/telegram_service.py`
- `src/taksklad/update_service.py`
- `src/taksklad/utils.py`
- `src/taksklad/__init__.py`
- `main.py` - тонкая точка запуска для разработки и PyInstaller.
- `taksklad/__init__.py` - bridge для импорта пакета из `src/` без установки.
- `sitecustomize.py` - добавляет `src/` в `sys.path` при локальных проверках.
- `telegram_settings.example.json`
- `requirements.txt`
- `.github/workflows/`
- `assets/`
- `tests/`

Эти файлы должны оставаться в Git, если в них нет секретов.

## Документация

Документация проекта:

- `README.md`
- `README.txt`
- `docs/changelog.md`
- `docs/implementation-log.md`
- `docs/project-architecture.md`
- `docs/project-knowledge-base.md`
- `docs/project-overview.md`
- `docs/roadmap.md`
- `docs/skladbot-api-key-functionality.md`
- `docs/taksklad-full-functionality.md`
- `docs/warehouse-ecosystem-roadmap.md`

Перед коммитом документацию нужно проверить на случайно записанные секреты или слишком конкретные идентификаторы ключей.

## Секреты И Локальные Настройки

Держать вне Git:

- `credentials.json`
- `credentials_*.json`
- `telegram_settings.json`
- `yandex_geocoder_key.txt`
- любые реальные API tokens, bot tokens, service account private keys, chat ids.

Нужны только example-файлы без реальных значений.

## Рабочие Данные

Держать вне Git:

- `TakSklad_data.json`
- `TakSklad_data_*.json`
- `pending_saves.json`
- `pending_prints.json`
- `pending_telegram.json`
- `telegram_state.json`
- `product_catalog.json`
- `import_history.json`
- `scan_backups/`

Это локальное состояние приложения, а не исходный код.

## Логи

Держать вне Git:

- `*.log`
- `TakSklad.log`
- `docs/*.log`
- `reports/TakSklad_log_*.txt`

Логи можно переносить в `archive/`, если они нужны для разбора ошибок. Перед отправкой наружу их нужно чистить от персональных данных и секретов.

## Backup И Старые Снимки

Кандидаты на перенос в `archive/repo-cleanup-YYYYMMDD/`:

- `google_sheet_backup_*.json`
- `TakSklad_data_before_*.json`
- старые `credentials_*_YYYYMMDD_*.json`
- старые one-off JSON-снимки в `exports/`

Не удалять до проверки, что нужные данные уже перенесены в текущую таблицу/БД или больше не нужны.

## Отчёты И Release-Артефакты

Держать вне Git:

- `reports/`
- `exports/`
- `*.zip`
- собранные папки `TakSklad/` внутри `exports/`
- `.xlsx` отчёты, если это не тестовые fixtures.

Release-архивы лучше хранить в GitHub Releases или отдельном файловом хранилище, а не в репозитории.

## Уже Настроено В `.gitignore`

Игнорируются:

- credentials и локальные JSON-данные;
- логи;
- pending/state/settings-файлы;
- `scan_backups/`;
- `reports/`;
- `exports/`;
- `.venv/`, `venv/`;
- `__pycache__/`, `*.pyc`;
- `build/`, `dist/`, `*.spec`;
- `archive/`.

## Что Делать Следующим Шагом

1. Оставить текущие `credentials.json` и `TakSklad_data.json` на месте до перехода на понятный local data dir.
2. Проверить `git status --ignored`, чтобы убедиться, что секреты и данные не попадают в Git.
3. Проверить документацию на реальные секреты и идентификаторы ключей перед публикацией.

## Что Уже Перенесено В Архив

Локальный архив: `archive/repo-cleanup-20260529/`.

Перенесены:

- старые логи из корня и `docs/`;
- старые backup JSON и старые credentials-снимки;
- `reports/`;
- `exports/`;
- `scan_backups/`;
- legacy runtime JSON (`pending_telegram.json`, `telegram_state.json`, `telegram_settings.json`, `product_catalog.json`, `import_history.json`);
- Python cache и `.DS_Store`.

Не переносились:

- `credentials.json`;
- `TakSklad_data.json`.
