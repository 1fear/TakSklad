# Windows Backend Acceptance

Дата создания: 2026-05-30.

Цель документа — безопасно проверить подписанный production Windows release. Это не команда на публикацию, deploy или изменение `version.json`.

## 1. Что Проверяем

- Подписанный desktop и соседний console helper соответствуют package manifest и immutable signer pin.
- Desktop работает только через backend; старого локального/Google режима нет.
- При включённых backend flags приложение не блокирует склад, даже если backend временно недоступен.
- Скан КИЗ сначала сохраняется локально, затем отправляется в backend.
- Активные заказы можно читать из backend отдельным флагом.
- Excel-импорт через desktop отправляет строки в backend.
- Telegram Excel import на VDS отправляет строки в тот же backend import.
- Два ПК не конфликтуют по Telegram polling, потому что Telegram слушает серверный worker.

## 2. Подписанный Production Package

Production credential migration разрешена только из проверенного release-каталога: `TakSklad.exe`, соседний `TakSkladAuth.exe` и `build_manifest.json`. Unsigned/local test archive является только GUI/synthetic артефактом и не содержит production-capable auth helper.

До extraction и до любого token prompt доверенный admin host должен находиться на exact tagged/main SHA и выполнить GitHub/Sigstore verification `release.json`, outer `version.json`, ZIP и OCI/Windows subjects. Verifier сверяет release → version → ZIP hashes, безопасно отклоняет absolute/traversal/backslash/duplicate/case-collision/symlink/special/extra/missing/oversize members, связывает inner manifests с app/helper/wrapper и извлекает только в новый отсутствующий каталог:

```bash
release_dir="/protected/new-download-v2.0.51"
extract_dir="/protected/new-extract-v2.0.51"
mkdir -m 700 "$release_dir"
gh release download v2.0.51 --dir "$release_dir"
TAKSKLAD_RELEASE_MANIFEST="$release_dir/release.json" \
TAKSKLAD_RELEASE_ARTIFACT_DIR="$release_dir" \
./tools/verify_release_attestations.sh --sha "<exact-tagged-main-sha>" \
  --extract-windows-to "$extract_dir"
```

`extract_dir` заранее не создавать. Если `gh` недоступен на складском ПК, verification выполняется на доверенном admin host, после чего весь уже проверенный каталог `$extract_dir/TakSklad` передаётся по контролируемому каналу с chain-of-custody; пропускать verification нельзя. Production запускает только packaged `$extract_dir/TakSklad/windows_backend_acceptance.ps1`, не checkout `tools`.

Перед первым запуском закрыть GUI. Оператор запускает trusted wrapper из подписанного release-каталога. Wrapper до запроса token проверяет exact production origin, package manifest, SHA helper, immutable pinned leaf certificate и централизованную Authenticode policy для обоих EXE. Разрешён `Valid`; на clean host также только `NotTrusted`/`UnknownError` с единственной chain error `PartialChain` или `UntrustedRoot`. Любой другой status/error блокирует helper до materialization/handoff token.

```powershell
& ".\TakSklad\windows_backend_acceptance.ps1" -InstallBackendToken -CheckOnly `
  -PrincipalIdentifier "desktop.pc-01" -AppPath ".\TakSklad\TakSklad.exe"
```

Установка и rotation используют одну команду. До изменения DPAPI команда проверяет
новый scoped credential через pinned `https://api.taksklad.uz/api/v1/returns/auth-canary/desktop`
с exact `204 No Content`, а после записи повторяет
canary со значением, прочитанным из DPAPI. При любой ошибке прежнее значение
восстанавливается и проверяется; rollback failure является блокирующей fatal-ошибкой.
В argv, updater, release asset, stdout и логах token не появляется. После установки запускать приложение из распакованного архива через:

```powershell
& ".\TakSklad\windows_backend_acceptance.ps1" -AppPath ".\TakSklad\TakSklad.exe"
```

Threat model честный: DPAPI защищает store границей Windows user/profile, а не конкретного binary. Весь same-user code и сам warehouse profile являются доверенной зоной. Подпись helper подтверждает provenance release artifact и защищает операторский handoff от случайной/подменённой сборки, но не превращает DPAPI ACL в per-binary ACL.

## 3. Packaged Acceptance

Использовать только wrapper и подписанный release package:

```powershell
& ".\TakSklad\windows_backend_acceptance.ps1" -CheckOnly -AppPath ".\TakSklad\TakSklad.exe"
& ".\TakSklad\windows_backend_acceptance.ps1" -AppPath ".\TakSklad\TakSklad.exe"
```

Source/unit auth tests отделены от операторского helper: они используют только injected synthetic store и explicit localhost test API; current-user production DPAPI из checkout не читается.

Что делает helper:

- читает backend credential из current-user DPAPI store production desktop;
- запускает соседний подписанный `TakSkladAuth.exe`, сверяет его hash/signer с package manifest и проверяет только data-free desktop endpoint с exact `204`;
- включает backend flags только для текущего PowerShell-процесса и дочернего запуска приложения;
- требует production `build_manifest.json` и сверяет version/build label, helper SHA, signature requirement и pinned signer;
- не принимает token через argv/env и не печатает его.

Важно: unsigned test archive является synthetic-only и не имеет права читать production DPAPI. Production credential устанавливается и проверяется только подписанным release helper рядом с подписанным `TakSklad.exe`; GUI должен быть закрыт.

### Недоступный DPAPI store

Если status store не `ok` (ACL, другой Windows user/profile, повреждение), остановить wrapper и не запускать складской GUI. Нельзя автоматически удалять/reset store: в нём также находятся Telegram и geocoder secrets. Сохранить зашифрованный artifact и redacted metadata без открытия содержимого; отдельно проверить current-user/profile/ACL и факт corruption. Восстановление допустимо только под тем же Windows user/profile либо контролируемым reprovision всех обязательных secrets. Plaintext, transcript и логи с credential запрещены. После восстановления: wrapper с `-CheckOnly`, затем ручной read-only UI lookup/list и только после отдельного operator gate write-проверка. Любая неоднозначность — stop и escalation администратору. Ручной source/env обход запрещён.

Сервисный токен не хранить в документации, чате, скриншотах и Git.

### 3.1 DB-only Проверка На Одном ПК

До общего cutover проверка выполняется на одном доверенном Windows user/profile. Desktop уже не поддерживает другой источник данных; backend flags задаёт wrapper, а credential берётся только из current-user DPAPI.

Ожидаемые строки в логах:

- `Startup self-check: ... telegram_desktop_polling=no ... backend_only_refresh=yes ...`;
- `Refresh diagnostic summary: source=backend primary_source=backend backend_only_refresh=True ...`;
- при backend timeout без кэша: понятная ошибка `Backend refresh недоступен`, без чтения Google;
- при backend timeout с уже загруженным списком: текущая позиция сохраняется, автоматического Google fallback нет.

Admin shadow-срез не входит в Windows returns canary и требует отдельного scoped доступа.

## 4. Быстрый Rollback

Если в тесте появляется блокирующая ошибка, закрыть приложение и зафиксировать
локальные очереди. Запуск без backend или возврат на Google запрещён.

Очистить только временные переменные acceptance helper:

```powershell
& ".\TakSklad\windows_backend_acceptance.ps1" -Clear
```

Ручной вариант:

```powershell
Remove-Item Env:\TAKSKLAD_BACKEND_ENABLED -ErrorAction SilentlyContinue
Remove-Item Env:\TAKSKLAD_BACKEND_READ_ORDERS_ENABLED -ErrorAction SilentlyContinue
Remove-Item Env:\TAKSKLAD_BACKEND_BASE_URL -ErrorAction SilentlyContinue
Remove-Item Env:\TAKSKLAD_BACKEND_TIMEOUT_SECONDS -ErrorAction SilentlyContinue
Remove-Item Env:\TAKSKLAD_BACKEND_ONLY_REFRESH -ErrorAction SilentlyContinue
Remove-Item Env:\TELEGRAM_DESKTOP_POLLING_ENABLED -ErrorAction SilentlyContinue
```

Rollback не должен очищать локальные очереди. Перед rollback записать counts из последней строки `Startup self-check` или `Refresh diagnostic summary`:

- `pending_backend_events`;
- `pending_saves`;
- `pending_prints`;
- `pending_telegram`.

После исправления или установки предыдущего DB-compatible DB-only release снова
запустить приложение, дождаться self-check и сверить counts. Допустимо только
уменьшение count после успешной синхронизации; недопустимо внезапное обнуление
без фактического sync/audit. Критерий rollback: приложение снова работает как текущая стабильная desktop-линия, pending events не потеряны. Backend admin-проверка требует отдельного scoped доступа и отдельного gate.

## 5. Сценарии Приёмки

### 5.1 Запуск

Шаги:

1. Закрыть все процессы TakSklad.
2. Выполнить wrapper `-CheckOnly` и получить data-free desktop canary `204`.
3. Запустить приложение тем же wrapper без `-CheckOnly`.
4. Проверить read-only список и lookup; write-сценарий — только по отдельному operator gate.

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

1. На отдельном тестовом контуре отключить сеть; менять pinned backend origin запрещено.
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
- Candidate `2.0.51` проверен локально; public channel считается переключённым только после final preflight exact `2.0.51`/`onefile_exe` и immutable release gate.

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
