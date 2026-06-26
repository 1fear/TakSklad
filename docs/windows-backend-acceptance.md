# Windows Backend Acceptance

Дата создания: 2026-05-30.

Цель документа - пройти ручную приёмку TakSklad на Windows с включённым backend bridge до релиза 2.0. Это не релизный чеклист для рабочих компьютеров и не команда на обновление `version.json`.

## 1. Что Проверяем

- Desktop запускается на Windows как раньше.
- При выключенных backend flags приложение работает в старом локальном/Google режиме.
- При включённых backend flags приложение не блокирует склад, даже если backend временно недоступен.
- Скан КИЗ сначала сохраняется локально, затем отправляется в backend.
- Активные заказы можно читать из backend отдельным флагом.
- Excel-импорт через desktop отправляет строки в backend.
- Telegram Excel import на VDS отправляет строки в тот же backend import.
- Два ПК не конфликтуют по Telegram polling, потому что Telegram слушает серверный worker.

## 2. Тестовая Windows-Сборка

Для приёмки нужна свежая тестовая сборка, а не рабочий ярлык `1.1.7`.

На Windows из корня репозитория:

```powershell
.\tools\build_windows_test_archive.ps1 -InstallDependencies
```

Если зависимости уже установлены:

```powershell
.\tools\build_windows_test_archive.ps1
```

Что делает helper:

- проверяет, что `APP_VERSION` не ниже `2.0.0`;
- проверяет, что `APP_BUILD_LABEL = MVP 2.0`;
- проверяет, что публичный `version.json` находится в безопасном состоянии: либо старая стабильная линия `1.1.7`, либо non-mandatory rollout `2.0.0`;
- запускает автотесты, если не передан `-SkipTests`;
- собирает PyInstaller `--onedir`;
- добавляет acceptance helper и acceptance kit;
- создаёт ZIP и SHA256 в `outputs\windows_test_build`;
- не создаёт GitHub Release;
- не включает `mandatory=true`;
- не публикует новый Windows release поверх `v2.0.0`.

После сборки запускать приложение из распакованного архива через:

```powershell
.\tools\windows_backend_acceptance.ps1 -Token "<service-token>" -AppPath ".\TakSklad\TakSklad.exe"
```

## 3. Backend Flags Для Теста

Включать только на тестовой Windows-копии, не на рабочих ПК склада.

Рекомендуемый способ - использовать helper:

```powershell
.\tools\windows_backend_acceptance.ps1 -CheckOnly -Token "<service-token>"
.\tools\windows_backend_acceptance.ps1 -Token "<service-token>" -AppPath ".\TakSklad\TakSklad.exe"
```

Если запуск идёт из исходников:

```powershell
.\tools\windows_backend_acceptance.ps1 -Token "<service-token>" -AppPath ".\main.py"
```

Короткий вариант для исходников, если команда запускается из корня проекта:

```powershell
.\tools\windows_backend_acceptance.ps1 -Token "<service-token>" -UsePython
```

Что делает helper:

- проверяет `GET /health`;
- проверяет `GET /api/v1/orders/active` с service token;
- включает backend flags только для текущего PowerShell-процесса и дочернего запуска приложения;
- при запуске из исходников проверяет, что `APP_VERSION` не ниже `2.0.0`, чтобы случайно не тестировать старую рабочую линию;
- при запуске из исходников дополнительно сверяет `APP_BUILD_LABEL = MVP 2.0`;
- при запуске `.exe` требует `build_manifest.json` из тестового архива и сверяет, что `app_version` не ниже `2.0.0`, а `app_build_label` равен `MVP 2.0`;
- предпочитает проектный `.venv\Scripts\python.exe`, если он есть;
- не сохраняет token в файл, реестр или git.

Важно: если передать старый `TakSklad.exe` без `build_manifest.json` или архив без `app_build_label = MVP 2.0`, helper остановит запуск. Для Windows-приёмки использовать только свежий test archive или запуск из текущих исходников через `main.py`.

Ручной вариант, если helper недоступен:

```powershell
$env:TAKSKLAD_BACKEND_ENABLED = "1"
$env:TAKSKLAD_BACKEND_READ_ORDERS_ENABLED = "1"
$env:TAKSKLAD_BACKEND_BASE_URL = "https://api.taksklad.uz"
$env:TAKSKLAD_BACKEND_API_TOKEN = "<service-token-from-local-secret-storage>"
$env:TAKSKLAD_BACKEND_TIMEOUT_SECONDS = "8"
$env:TAKSKLAD_BACKEND_ONLY_REFRESH = "0"
$env:TAKSKLAD_BACKEND_EMERGENCY_GOOGLE_FALLBACK_ENABLED = "0"
$env:TELEGRAM_DESKTOP_POLLING_ENABLED = "0"
```

Сервисный токен не хранить в документации, чате, скриншотах и Git.

### 3.1 Shadow Backend-only На Одном ПК

Для Phase 7 backend-only включается только на одном тестовом профиле/ПК. Не раскатывать эти значения на все рабочие места:

```powershell
$env:TAKSKLAD_BACKEND_ENABLED = "1"
$env:TAKSKLAD_BACKEND_READ_ORDERS_ENABLED = "1"
$env:TAKSKLAD_BACKEND_BASE_URL = "https://api.taksklad.uz"
$env:TAKSKLAD_BACKEND_API_TOKEN = "<service-token-from-local-secret-storage>"
$env:TAKSKLAD_BACKEND_TIMEOUT_SECONDS = "8"
$env:TAKSKLAD_BACKEND_ONLY_REFRESH = "1"
$env:TAKSKLAD_BACKEND_EMERGENCY_GOOGLE_FALLBACK_ENABLED = "0"
$env:TELEGRAM_DESKTOP_POLLING_ENABLED = "0"
```

Ожидаемые строки в логах:

- `Startup self-check: ... telegram_desktop_polling=no ... backend_only_refresh=yes ... backend_emergency_google_fallback=no ...`;
- `Refresh diagnostic summary: source=backend primary_source=backend backend_only_refresh=True emergency_google_fallback=False ...`;
- при backend timeout без кэша: понятная ошибка `Backend refresh недоступен`, без чтения Google;
- при backend timeout с уже загруженным списком: текущая позиция сохраняется, автоматического Google fallback нет.

Backend shadow-срез:

```powershell
Invoke-RestMethod `
  -Headers @{ Authorization = "Bearer <service-token-from-local-secret-storage>" } `
  -Uri "https://api.taksklad.uz/api/v1/admin/operations" |
  Select-Object -ExpandProperty shadow_diagnostics
```

Ожидаемые поля: `backend_active_orders_source=postgres_backend`, `google_mirror_status`, `google_mirror_lag_seconds`, `google_mirror_pending_exports`, `google_mirror_failed_exports`, `queue_stale_processing`, `hot_path_stale_processing`, `telegram_worker_state`, `telegram_pending_events`.

Аварийный Google fallback разрешён только вручную и временно:

```powershell
$env:TAKSKLAD_BACKEND_EMERGENCY_GOOGLE_FALLBACK_ENABLED = "1"
```

После проверки вернуть:

```powershell
$env:TAKSKLAD_BACKEND_EMERGENCY_GOOGLE_FALLBACK_ENABLED = "0"
```

## 4. Быстрый Rollback

Если в тесте появляется блокирующая ошибка, закрыть приложение и запустить без backend flags:

```powershell
.\tools\windows_backend_acceptance.ps1 -Clear
```

Ручной вариант:

```powershell
Remove-Item Env:\TAKSKLAD_BACKEND_ENABLED -ErrorAction SilentlyContinue
Remove-Item Env:\TAKSKLAD_BACKEND_READ_ORDERS_ENABLED -ErrorAction SilentlyContinue
Remove-Item Env:\TAKSKLAD_BACKEND_BASE_URL -ErrorAction SilentlyContinue
Remove-Item Env:\TAKSKLAD_BACKEND_API_TOKEN -ErrorAction SilentlyContinue
Remove-Item Env:\TAKSKLAD_BACKEND_TIMEOUT_SECONDS -ErrorAction SilentlyContinue
Remove-Item Env:\TAKSKLAD_BACKEND_ONLY_REFRESH -ErrorAction SilentlyContinue
Remove-Item Env:\TAKSKLAD_BACKEND_EMERGENCY_GOOGLE_FALLBACK_ENABLED -ErrorAction SilentlyContinue
Remove-Item Env:\TELEGRAM_DESKTOP_POLLING_ENABLED -ErrorAction SilentlyContinue
```

Rollback не должен очищать локальные очереди. Перед rollback записать counts из последней строки `Startup self-check` или `Refresh diagnostic summary`:

- `pending_backend_events`;
- `pending_saves`;
- `pending_prints`;
- `pending_telegram`.

После rollback снова запустить приложение, дождаться новой строки self-check и сверить counts. Допустимо только уменьшение count после успешной синхронизации; недопустимо внезапное обнуление без фактического sync/audit. На backend дополнительно проверить:

```powershell
Invoke-RestMethod `
  -Headers @{ Authorization = "Bearer <service-token-from-local-secret-storage>" } `
  -Uri "https://api.taksklad.uz/api/v1/admin/events"
```

Критерий rollback: приложение снова работает как текущая стабильная desktop-линия, pending events не потеряны, `/api/v1/admin/events` и `/api/v1/admin/operations` не показывают новый hot-path blocker.

## 5. Сценарии Приёмки

### 5.1 Запуск

Шаги:

1. Запустить приложение без backend flags.
2. Проверить, что список заказов, выбор заказа и сканирование работают как раньше.
3. Закрыть приложение.
4. Запустить приложение с backend flags.

Ожидаемый результат:

- приложение открывается без ошибки;
- нет зависания на старте;
- в логах нет traceback по backend config.

### 5.2 Desktop Excel Import

Шаги:

1. Импортировать копию реального Excel-файла.
2. Проверить, что заказы видны в desktop.
3. Проверить `GET /api/v1/imports` на backend.
4. Проверить `GET /api/v1/orders/active`.

Ожидаемый результат:

- импорт не создает дубли при повторном запуске того же файла;
- позиции, клиент, адрес, оплата, количество и номера SkladBot не ломаются;
- невалидные строки попадают в ошибки импорта, но не валят весь файл.

### 5.3 Telegram Excel Import

Шаги:

1. Отправить `.xlsx` или `.xlsm` в разрешённый Telegram chat.
2. Дождаться ответа Telegram worker.
3. Проверить импорт в backend history.
4. Проверить, что активные заказы появились в backend.

Ожидаемый результат:

- Telegram отвечает, что Excel импортирован;
- строки появились в backend;
- повторная отправка того же файла не создает дубль позиций;
- Telegram token не появляется в логах.

### 5.4 Сканирование С Backend

Шаги:

1. После обновления списка проверить статус `Backend: online, список из VDS` в блоке статистики.
2. Выбрать заказ.
3. Отсканировать валидный КИЗ.
4. Проверить локальный backup скана.
5. Проверить `POST /api/v1/scans` эффект через backend active orders.

Ожидаемый результат:

- backend status виден оператору без служебных окон;
- КИЗ принят без задержки, мешающей оператору;
- локальный backup создан до внешней отправки;
- backend получил скан;
- повтор того же КИЗа в той же позиции не увеличивает счётчик второй раз;
- дубль того же КИЗа в другой позиции остаётся конфликтом backend queue, а не исчезает.

### 5.5 Обновление Во Время Сканирования

Шаги:

1. Начать сканирование позиции.
2. На втором окне или втором ПК обновить список заказов.
3. Во время обновления продолжить сканировать.

Ожидаемый результат:

- нет ложного постоянного сообщения `Дождитесь завершения текущей операции`;
- сканирование не блокируется долгим обновлением;
- текущая позиция не сбрасывается.

### 5.6 Два ПК

Шаги:

1. Запустить тестовую копию на двух Windows ПК с backend flags.
2. На первом ПК выбрать одну позицию.
3. На втором ПК выбрать другую позицию.
4. Отсканировать разные КИЗы.
5. Попробовать отсканировать один и тот же КИЗ на обоих ПК.

Ожидаемый результат:

- разные КИЗы сохраняются;
- дубль одного КИЗ не проходит;
- Telegram `HTTP 409 Conflict` не появляется из desktop, потому что Telegram polling не должен запускаться на ПК.

### 5.7 Backend Недоступен

Шаги:

1. Временно указать неправильный `TAKSKLAD_BACKEND_BASE_URL` или отключить сеть.
2. Отсканировать КИЗ.
3. Вернуть правильный backend.
4. Проверить повторную отправку pending backend events.

Ожидаемый результат:

- оператор может сканировать;
- КИЗ не теряется;
- событие появляется в локальной очереди;
- после восстановления backend событие синхронизируется.

### 5.8 Завершение Заказа, Печать, День

Шаги:

1. Досканировать тестовый заказ.
2. Завершить заказ.
3. Проверить печать.
4. Завершить день.
5. Проверить дневной отчёт в backend и Telegram.

Ожидаемый результат:

- недосканированный заказ не закрывается;
- досканированный заказ закрывается;
- печать не зависит от backend;
- дневной отчёт совпадает с фактическими сканами.

## 6. Критерий Готовности К Релизу

Релиз 2.0 можно готовить только если:

- все сценарии выше пройдены на тестовой Windows-копии;
- найденные дефекты закрыты или записаны как accepted known issues;
- rollback без backend flags проверен;
- нет потери КИЗов при offline/timeout;
- `version.json` проверен как staged rollout `2.0.0`, `mandatory=false`.

## 7. Что Уже Покрыто Автотестами

Автотесты не заменяют физическую Windows-приёмку, но закрывают часть логики desktop/backend bridge:

- backend-заказы преобразуются в desktop-строки с существующими КИЗами;
- pending backend scan не дублируется;
- pending backend code попадает в общий набор занятых КИЗов и блокирует повторный ввод;
- отмена последнего КИЗа убирает pending backend scan;
- retryable backend failure оставляет событие в очереди;
- повтор того же КИЗа в той же backend-позиции идемпотентен;
- дубль КИЗа в другой backend-позиции остаётся конфликтом в очереди, а не исчезает как успешная синхронизация;
- pending `order_complete` отправляется в backend;
- неизвестное событие не держит очередь.

Текущая команда:

```bash
.venv/bin/python -m unittest tests.test_backend_bridge
```

## 8. Что Остаётся После Приёмки

- Собрать Windows archive.
- Проверить archive на чистой Windows-машине.
- Подготовить release notes 2.0.
- Включить staged rollout: сначала один ПК, затем второй после смены без критичных ошибок.
