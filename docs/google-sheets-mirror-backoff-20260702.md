# Google Sheets Mirror Backoff 2026-07-02

## Причина

Production `google-sheets-sync-worker` после `Pending Google Sheets export paused after rate limit: APIError: [429]` сразу запускал full read `data + Архив` через Google-to-backend sync. Это добивало Google Sheets read quota, писало ERROR traceback и держало mirror в `degraded/pending`.

## Изменено

- `run_google_sheets_worker_cycle()` пропускает Google-to-backend full read, если pending export batch вернул `paused`.
- Worker читает persistent cooldown из `pending_events.payload.next_attempt_at`, чтобы после restart не начинать full read раньше retry window.
- Google read `429/quota` в backend sync переводится в warning + cooldown, без `logging.exception`.
- No-op `google_sheets_skladbot_export` для уже пронумерованных заказов throttled через `SKLADBOT_GOOGLE_EXPORT_MIN_INTERVAL_SECONDS`, default 300 секунд.
- Реальные SkladBot updates и archive backfill продолжают ставить Google export без throttle через `force=True`.

## Инварианты

- Postgres остается source of truth.
- Pending Google exports не переводятся в completed/skipped при 429.
- Складские статусы, остатки, КИЗы и строки Google Sheets этой правкой не меняются.

## Проверено

- `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. ./.venv/bin/python -m unittest tests.test_google_sheets_sync_worker.GoogleSheetsSyncWorkerTests.test_worker_cycle_skips_backend_read_when_pending_exports_paused tests.test_google_sheets_sync_worker.GoogleSheetsSyncWorkerTests.test_worker_cycle_cools_down_backend_read_after_rate_limit tests.test_google_sheets_sync_worker.GoogleSheetsSyncWorkerTests.test_worker_cycle_skips_backend_read_during_persistent_export_cooldown tests.test_backend_google_sheets_pending.GoogleSheetsPendingLockTests.test_google_sheets_export_cooldown_until_uses_future_retry_events tests.test_backend_google_sheets_pending.GoogleSheetsPendingLockTests.test_rate_limit_keeps_event_pending_and_stops_batch tests.test_backend_skladbot_worker.BackendSkladBotWorkerTests.test_skladbot_google_export_skips_recent_noop_export` - 6 tests OK.
- `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. ./.venv/bin/python -m py_compile backend/app/google_sheets_sync_worker.py backend/app/google_sheets_pending.py backend/app/skladbot_worker.py tests/test_google_sheets_sync_worker.py tests/test_backend_google_sheets_pending.py tests/test_backend_skladbot_worker.py` - OK.
- `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. ./.venv/bin/python -m unittest tests.test_google_sheets_sync_worker tests.test_backend_google_sheets_pending` - 32 tests OK.
- `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. ./.venv/bin/python -m unittest tests.test_backend_skladbot_worker` - 50 tests OK.
- `git diff --check` - OK.
