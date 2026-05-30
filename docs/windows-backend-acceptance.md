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

## 2. Backend Flags Для Теста

Включать только на тестовой Windows-копии, не на рабочих ПК склада.

Рекомендуемый способ - использовать helper:

```powershell
.\tools\windows_backend_acceptance.ps1 -CheckOnly -Token "<service-token>"
.\tools\windows_backend_acceptance.ps1 -Token "<service-token>" -AppPath ".\TakSklad.exe"
```

Если запуск идёт из исходников:

```powershell
.\tools\windows_backend_acceptance.ps1 -Token "<service-token>" -AppPath ".\main.py"
```

Что делает helper:

- проверяет `GET /health`;
- проверяет `GET /api/v1/orders/active` с service token;
- включает backend flags только для текущего PowerShell-процесса и дочернего запуска приложения;
- не сохраняет token в файл, реестр или git.

Ручной вариант, если helper недоступен:

```powershell
$env:TAKSKLAD_BACKEND_ENABLED = "1"
$env:TAKSKLAD_BACKEND_READ_ORDERS_ENABLED = "1"
$env:TAKSKLAD_BACKEND_BASE_URL = "https://api.135.181.245.84.sslip.io"
$env:TAKSKLAD_BACKEND_API_TOKEN = "<service-token-from-local-secret-storage>"
$env:TAKSKLAD_BACKEND_TIMEOUT_SECONDS = "8"
```

После перехода DNS заменить временный URL на:

```powershell
$env:TAKSKLAD_BACKEND_BASE_URL = "https://api.taksklad.uz"
```

Сервисный токен не хранить в документации, чате, скриншотах и Git.

## 3. Быстрый Rollback

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
```

Критерий rollback: приложение снова работает как текущая стабильная desktop-линия.

## 4. Сценарии Приёмки

### 4.1 Запуск

Шаги:

1. Запустить приложение без backend flags.
2. Проверить, что список заказов, выбор заказа и сканирование работают как раньше.
3. Закрыть приложение.
4. Запустить приложение с backend flags.

Ожидаемый результат:

- приложение открывается без ошибки;
- нет зависания на старте;
- в логах нет traceback по backend config.

### 4.2 Desktop Excel Import

Шаги:

1. Импортировать копию реального Excel-файла.
2. Проверить, что заказы видны в desktop.
3. Проверить `GET /api/v1/imports` на backend.
4. Проверить `GET /api/v1/orders/active`.

Ожидаемый результат:

- импорт не создает дубли при повторном запуске того же файла;
- позиции, клиент, адрес, оплата, количество и номера SkladBot не ломаются;
- невалидные строки попадают в ошибки импорта, но не валят весь файл.

### 4.3 Telegram Excel Import

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

### 4.4 Сканирование С Backend

Шаги:

1. Выбрать заказ.
2. Отсканировать валидный КИЗ.
3. Проверить локальный backup скана.
4. Проверить `POST /api/v1/scans` эффект через backend active orders.

Ожидаемый результат:

- КИЗ принят без задержки, мешающей оператору;
- локальный backup создан до внешней отправки;
- backend получил скан;
- повтор того же КИЗ возвращает понятный дубль, а не ломает приложение.

### 4.5 Обновление Во Время Сканирования

Шаги:

1. Начать сканирование позиции.
2. На втором окне или втором ПК обновить список заказов.
3. Во время обновления продолжить сканировать.

Ожидаемый результат:

- нет ложного постоянного сообщения `Дождитесь завершения текущей операции`;
- сканирование не блокируется долгим обновлением;
- текущая позиция не сбрасывается.

### 4.6 Два ПК

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

### 4.7 Backend Недоступен

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

### 4.8 Завершение Заказа, Печать, День

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

## 5. Критерий Готовности К Релизу

Релиз 2.0 можно готовить только если:

- все сценарии выше пройдены на тестовой Windows-копии;
- найденные дефекты закрыты или записаны как accepted known issues;
- rollback без backend flags проверен;
- нет потери КИЗов при offline/timeout;
- `version.json` всё ещё не менялся до финального решения о rollout.

## 6. Что Уже Покрыто Автотестами

Автотесты не заменяют физическую Windows-приёмку, но закрывают часть логики desktop/backend bridge:

- backend-заказы преобразуются в desktop-строки с существующими КИЗами;
- pending backend scan не дублируется;
- pending backend code попадает в общий набор занятых КИЗов и блокирует повторный ввод;
- отмена последнего КИЗа убирает pending backend scan;
- retryable backend failure оставляет событие в очереди;
- backend duplicate scan `409 Code already scanned` считается уже синхронизированным;
- pending `order_complete` отправляется в backend;
- неизвестное событие не держит очередь.

Текущая команда:

```bash
.venv/bin/python -m unittest tests.test_backend_bridge
```

## 7. Что Остаётся После Приёмки

- Собрать Windows archive.
- Проверить archive на чистой Windows-машине.
- Подготовить release notes 2.0.
- Включить staged rollout: сначала один ПК, затем второй после смены без критичных ошибок.
