# TakSklad Manual Acceptance Runbook

Дата: 2026-05-31.

Этот документ закрывает последние ручные проверки, которые нельзя честно выполнить из macOS/VDS без участия реального Telegram-пользователя и Windows-компьютера склада.

Статус 2026-07-02: основной production smoke принят по боевому подтверждению Антона. В бою прошли Smartup auto export, Telegram import в БД, сканирование КИЗов и создание заявок SkladBot; операторских ошибок не заявлено. Текущий релизный результат фиксируется в `outputs/taksklad_acceptance/ACCEPTANCE_RESULTS.md`, а synthetic marker `ACCEPTANCE TELEGRAM 20260531` остается только fallback-сценарием для повторной искусственной приемки.

## 1. Telegram Import От Пользовательского Аккаунта

### Цель

Проверить именно входящее пользовательское сообщение в Telegram-бот, а не только Bot API file smoke.

### Файл Для Проверки

Готовый acceptance kit:

`/Users/anton/Documents/work/TakSklad/outputs/taksklad_acceptance/README.md`

Внутри лежат:

`/Users/anton/Documents/work/TakSklad/outputs/taksklad_acceptance/TakSklad_Telegram_Acceptance_2026-05-31.xlsx`

`/Users/anton/Documents/work/TakSklad/outputs/taksklad_acceptance/acceptance_manifest.json`

`/Users/anton/Documents/work/TakSklad/outputs/taksklad_acceptance/ACCEPTANCE_RESULTS.md`

`/Users/anton/Documents/work/TakSklad/outputs/taksklad_acceptance/ACCEPTANCE_RESULTS_TEMPLATE.md`

Если kit нужно пересобрать:

```bash
cd /Users/anton/Documents/work/TakSklad
.venv/bin/python tools/prepare_acceptance_kit.py
```

Перед ручными проверками запустить локальный preflight:

```bash
cd /Users/anton/Documents/work/TakSklad
.venv/bin/python tools/release_preflight.py --phase candidate --skip-network
```

Перед Windows-приёмкой можно дополнительно проверить опубликованные артефакты:

```bash
.venv/bin/python tools/release_preflight.py --phase final --verify-downloads \
  --source-sha <exact-tagged-main-sha> --timeout 120
```

Candidate-mode проверяет локальный кандидат `2.0.51`, не утверждая, что public channel уже переключён. Final-mode разрешён только после immutable publication: он скачивает все Windows assets из release manifest, проверяет exact `2.0.51`, `onefile_exe`, SHA256 и attestations.

Он проверяет:

- публичный `https://api.taksklad.uz/health`;
- candidate contract `2.0.51` отдельно от ещё поддерживаемого public channel; final-mode требует опубликованный exact `2.0.51`, `mandatory=true`, `onefile_exe` и immutable ссылки/хеши;
- checksum acceptance Excel;
- наличие acceptance/runbook/helper-файлов;
- отсутствие tracked runtime/secret-файлов.

Содержимое:

- клиент: `ACCEPTANCE TELEGRAM 20260531`;
- дата отгрузки: `31.05.2026`;
- 2 позиции;
- 3 блока всего;
- координаты: `41.311081, 69.240562`;
- цена блока: `240000`.
- SHA-256 Excel зафиксирован в `acceptance_manifest.json` и `README.md`.

Файл проверен локальным backend parser:

- строк импорта: `2`;
- дата отгрузки: `31.05.2026`;
- блоки: `2` и `1`;
- суммы: `480000` и `240000`.

Результат ручной приёмки фиксировать не в шаблоне, а в `ACCEPTANCE_RESULTS.md`: заполнить фактический вывод Telegram/VDS/Windows, список дефектов и итог `GO/NO-GO`, затем запустить `tools/release_go_no_go.py`.
Текущий файл уже может быть создан со статусом `NO-GO`; его нужно обновлять по факту проверок, а не удалять.

Отдельный контроль полного реестра функций:

```bash
cd /Users/anton/Documents/work/TakSklad
.venv/bin/python tools/feature_acceptance_status.py
.venv/bin/python tools/feature_acceptance_status.py --require-manual-complete --require-no-open-errors
```

`feature_acceptance_status.py` проверяет только `docs/taksklad-feature-user-stories.xlsx`: наличие всех manual-строк, обязательные колонки, неизвестные статусы и открытые ошибки. Это не production release `GO/NO-GO`; релизный канон остается `tools/release_go_no_go.py` + `outputs/taksklad_acceptance/ACCEPTANCE_RESULTS.md`.

### Действия В Telegram

1. Открыть Telegram-бота `SkladKis_bot` от реального разрешённого пользовательского аккаунта.
2. Нажать кнопку `Дата отгрузки`.
3. Отправить текстом `31.05.2026`.
4. Отправить файл `TakSklad_Telegram_Acceptance_2026-05-31.xlsx` как документ.
5. Дождаться ответа бота.

### Ожидаемый Результат

Бот должен ответить примерно так:

- файл поставлен в очередь или импортирован;
- дата отгрузки `31.05.2026`;
- строк отправлено в backend: `2`;
- импортировано: `2`;
- ошибок: `0`.

После обработки в backend должен появиться активный заказ `ACCEPTANCE TELEGRAM 20260531`.

### Проверка На VDS

После ответа бота проверить backend по маркеру:

```bash
cd /opt/taksklad/app
./deploy/vds/verify_acceptance_marker.sh "ACCEPTANCE TELEGRAM 20260531" --expect-orders 1
```

Или дождаться появления заказа автоматически:

```bash
cd /opt/taksklad/app
./deploy/vds/wait_acceptance_marker.sh "ACCEPTANCE TELEGRAM 20260531" --expect-orders 1 --timeout 300 --interval 10
```

Ожидаемо:

- `status`: `ok`;
- `orders`: `1`;
- `items`: `2`;
- `planned_blocks`: `3`;
- `imports`: минимум `1`;
- `pending_events`: `0`.

### SkladBot Match Diagnostic

После того как менеджер создал живую заявку `3PL отгрузка` в SkladBot, проверить matching без записи в БД:

```bash
cd /opt/taksklad/app
./deploy/vds/diagnose_skladbot_match.sh --marker "ACCEPTANCE TELEGRAM 20260531" --limit 5 --request-limit 20
```

Что смотреть:

- `candidate_requests` больше `0`;
- у нужного заказа в `matched_requests` есть ровно одна заявка;
- если совпадения нет, в `nearest_requests[].failed_checks` видно причину: `date`, `client`, `payment` или `products`;
- `product_checks` показывает, какой товар/количество блоков не совпали.

Диагностика read-only: она не меняет заказы, КИЗы, SkladBot-номера и статусы.
Если активного backend-заказа по маркеру нет, диагностика не ходит в SkladBot API и сразу возвращает `active_orders: 0`.

### Очистка После Проверки

После ручной проверки тестовые данные нужно удалить по маркеру:

`ACCEPTANCE TELEGRAM 20260531`

Сначала dry-run:

```bash
cd /opt/taksklad/app
./deploy/vds/cleanup_acceptance_marker.sh "ACCEPTANCE TELEGRAM 20260531"
```

Если вывод показывает только ожидаемые тестовые строки, удалить:

```bash
cd /opt/taksklad/app
./deploy/vds/cleanup_acceptance_marker.sh "ACCEPTANCE TELEGRAM 20260531" --apply
```

Скрипт отказывается работать с обычными маркерами без слов `ACCEPTANCE`, `WEB_UI_SMOKE` или `SMOKE_MVP`.

## 2. Повторяемый VDS MVP Smoke

### Цель

Быстро проверить backend-процесс на VDS без реальных складских данных:

- импорт тестового заказа;
- логистический отчёт с координатами;
- запрет досрочного завершения заказа;
- сканирование 3 КИЗов;
- запрет дубля КИЗа;
- завершение заказа;
- КИЗ-отчёт по исходному файлу;
- автоматическая очистка smoke-данных.

### Команда На VDS

```bash
cd /opt/taksklad/app
./deploy/vds/smoke_mvp_chapman.sh
```

Опционально можно задать дату и маркер:

```bash
cd /opt/taksklad/app
SMOKE_SHIPMENT_DATE=2026-05-31 \
SMOKE_MARKER=SMOKE_MVP_CHAPMAN_manual_20260531 \
./deploy/vds/smoke_mvp_chapman.sh
```

Маркер обязан содержать `SMOKE_MVP`, иначе скрипт откажется запускаться. После проверки скрипт удаляет созданные тестовые строки через `cleanup_acceptance_marker.sh`.

## 3. Windows Desktop Acceptance

### Цель

Проверить desktop-приложение TakSklad на Windows, потому что web-frontend smoke на VDS не доказывает работу Tkinter/печати/локальной Windows-среды.

Основной чеклист: `docs/windows-backend-acceptance.md`.

### Unsigned Test Archive: Только GUI/Synthetic

Локальный test archive можно собрать для GUI/synthetic проверки без production credential:

```powershell
.\tools\build_windows_test_archive.ps1 -InstallDependencies
```

Если зависимости уже стоят:

```powershell
.\tools\build_windows_test_archive.ps1
```

Распаковать архив из `outputs\windows_test_build`. Он не содержит production-capable `TakSkladAuth.exe`: запрещены current-user production DPAPI, production origin, установка token и credentialed VDS acceptance. Допустимы только GUI smoke и source/unit сценарии с injected synthetic store и explicit localhost test API.

### Signed v2.0.51: Credentialed VDS/DPAPI Acceptance

Получить подписанный production ZIP `v2.0.51` только после release gate. На доверенном admin host запустить `tools/verify_release_attestations.sh --sha <exact-tagged-main-sha> --extract-windows-to <new-absolute-dir>` с `TAKSKLAD_RELEASE_MANIFEST` и `TAKSKLAD_RELEASE_ARTIFACT_DIR`, указывающими на скачанные release assets. Verifier сначала проверяет GitHub/Sigstore attestations и cross-manifest hashes, затем безопасно извлекает ZIP в новый каталог. Только весь проверенный каталог `TakSklad` передаётся на workstation. Checkout wrapper, обычная распаковка ZIP и unsigned archive не подходят.

Установить или rotate отдельный desktop principal и сразу выполнить data-free desktop canary:

```powershell
& ".\TakSklad\windows_backend_acceptance.ps1" -InstallBackendToken -CheckOnly `
  -PrincipalIdentifier "desktop.pc-01" -AppPath ".\TakSklad\TakSklad.exe"
```

Повторная read-only проверка связи с VDS:

```powershell
& ".\TakSklad\windows_backend_acceptance.ps1" -CheckOnly -AppPath ".\TakSklad\TakSklad.exe"
```

Запуск на одном доверенном тестовом Windows profile:

```powershell
& ".\TakSklad\windows_backend_acceptance.ps1" -AppPath ".\TakSklad\TakSklad.exe"
```

В startup diagnostics должны быть видны:

- `telegram_desktop_polling=no`;
- `backend_only_refresh=yes`;
- `backend_emergency_google_fallback=no`.

Production credential acceptance выполняется только для подписанного packaged release. Wrapper проверяет соседние EXE, immutable signer pin, package manifest и helper SHA до чтения или передачи нового token; при любой ошибке helper не запускается.

1. Запустить подписанный TakSklad v2.0.51 через wrapper выше.
2. Убедиться, что приложение открылось без обновления `version.json`.
3. Обновить список заказов.
4. Проверить в блоке статистики статус `Backend: online, список из VDS`.
5. Найти заказ `ACCEPTANCE TELEGRAM 20260531`.
6. Выбрать заказ.
7. Отсканировать 3 тестовых КИЗа:
   - `WIN-KIZ-ACCEPT-001`;
   - `WIN-KIZ-ACCEPT-002`;
   - `WIN-KIZ-ACCEPT-003`.
8. Завершить заказ.
9. Проверить, что после завершения юрлица появляется окно печати, а не браузер.
10. В окне печати проверить:
   - выбор доступного принтера;
   - размеры этикетки `100x100`, `100x150`, `75x50`, `58x40`;
   - клавиша `Enter` подтверждает печать;
   - клавиша `Esc` закрывает окно без печати.
11. Проверить, что заказ ушёл из активных.
12. Проверить завершение смены.
13. Отдельно проверить: обновление списка во время сканирования не блокирует ввод КИЗов.
14. Проверить backend refresh после временного network timeout: с загруженным списком текущая позиция сохраняется, без загруженного списка показывается backend connectivity error.
15. Проверить `retired Google worker absent`: compose и runtime не содержат
    Google worker, credentials mount или новых `google_sheets_export` событий.
16. Открыть `Возвраты`.
17. Найти завершённую заявку по ШК/номеру SkladBot.
18. Нажать `Принять возврат`.
19. Проверить, что заявка появилась в `Последние возвраты`.
20. Повторно найти эту же заявку и убедиться, что повторное принятие заблокировано.

### Ожидаемый Результат

- приложение не зависает;
- на экране статистики видно состояние backend;
- нет ложного `Дождитесь завершения текущей операции`;
- КИЗы сохраняются;
- заказ завершается;
- печать появляется после завершения заказа, даёт выбрать принтер и размер этикетки;

### Если Рабочий ПК Остался На Старой Версии

Если TakSklad пишет, что требуется обязательное обновление, или автообновление упало, этот ПК нельзя оставлять в обычном режиме сканирования.

1. Закрыть TakSklad.
2. Открыть лог `docs/TakSklad_update.log` рядом с приложением и сохранить текст ошибки для разбора.
3. Установить свежий Windows-архив TakSklad: распаковать новую папку и запускать только новый `TakSklad.exe`.
4. Проверить внизу окна версию приложения и `MVP 2.0`.
5. Нажать `Обновить` и убедиться, что список заказов читается из VDS.

Если старая версия снова открывается по ярлыку, удалить старый ярлык с рабочего стола и создать новый на свежий `TakSklad.exe`.
- возвраты видны отдельным списком, повторный возврат одной заявки запрещён;
- завершение смены формирует ожидаемый отчёт;
- тестовые данные можно удалить по маркеру.

### Проверка После Windows-Сканов

После сканирования 3 КИЗов и завершения заказа проверить VDS:

```bash
cd /opt/taksklad/app
./deploy/vds/verify_acceptance_marker.sh "ACCEPTANCE TELEGRAM 20260531" \
  --expect-orders 1 \
  --expect-scans 3 \
  --expect-completed
```

Или дождаться результата автоматически:

```bash
cd /opt/taksklad/app
./deploy/vds/wait_acceptance_marker.sh "ACCEPTANCE TELEGRAM 20260531" \
  --expect-orders 1 \
  --expect-scans 3 \
  --expect-completed \
  --timeout 300 \
  --interval 10
```

Ожидаемо:

- `status`: `ok`;
- `completed_orders`: `1`;
- `active_orders`: `0`;
- `planned_blocks`: `3`;
- `scanned_blocks`: `3`;
- `scan_codes`: `3`;
- `incomplete_items`: `[]`.

## 4. Что Не Делать Во Время Acceptance

- Не менять `version.json` вручную и не переключать rollout без release checklist.
- Не перемещать и не перезаписывать существующий tag/release; `v2.0.51` публикуется один раз после final gate.
- Не запускать Windows release workflow.
- Не проверять на реальных заказах без отдельного подтверждения.
- Не делать deploy из dirty tree широким `rsync`; только selective deploy проверенных файлов после restore point.

Для быстрого отката тестового запуска:

```powershell
& ".\TakSklad\windows_backend_acceptance.ps1" -Clear `
  -AppPath ".\TakSklad\TakSklad.exe"
```

## 5. Критерий Закрытия Goal

Goal можно закрывать только после трёх подтверждений:

1. Telegram import прошёл от реального пользовательского аккаунта.
2. SkladBot matching проверен на живой заявке `3PL отгрузка`.
3. Windows desktop acceptance прошёл на тестовой Windows-копии.

Статус 2026-07-02: эти подтверждения приняты через production smoke Антона и live readiness checks. Для нового релиза или подозрения на регрессию повторить сценарий по этому runbook.
