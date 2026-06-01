# TakSklad Manual Acceptance Runbook

Дата: 2026-05-31.

Этот документ закрывает последние ручные проверки, которые нельзя честно выполнить из macOS/VDS без участия реального Telegram-пользователя и Windows-компьютера склада.

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
.venv/bin/python tools/release_preflight.py
```

Перед Windows-приёмкой можно дополнительно проверить опубликованные артефакты:

```bash
.venv/bin/python tools/release_preflight.py --verify-downloads --timeout 120
```

Эта команда скачивает `TakSklad.exe` и `TakSklad-windows-x64.zip` из `version.json` и сверяет SHA256. Она дольше обычного preflight.

Он проверяет:

- публичный `https://api.taksklad.uz/health`;
- что `version.json` указывает на `2.0.0`, `mandatory=false`, ссылки и SHA заполнены;
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

### Минимальный Набор Проверок

На Windows сначала собрать свежий test archive, не меняя `version.json` вручную:

```powershell
.\tools\build_windows_test_archive.ps1 -InstallDependencies
```

Если зависимости уже стоят:

```powershell
.\tools\build_windows_test_archive.ps1
```

Распаковать архив из `outputs\windows_test_build`. Дальше команды выполнять из корня распакованного test archive.

На macOS или локально из исходников новый desktop-интерфейс можно открыть без старого ярлыка `1.1.7`:

```bash
cd /Users/anton/Documents/work/TakSklad
./tools/run_desktop_local.sh
```

Это не релизная сборка и не автообновление. Скрипт запускает текущий код из `src/taksklad`, поэтому внизу окна должна быть версия из `src/taksklad/config.py`, а не старая рабочая сборка.

Проверить связь с VDS:

```powershell
.\tools\windows_backend_acceptance.ps1 -CheckOnly -Token "<service-token>"
```

Запустить тестовую копию:

```powershell
.\tools\windows_backend_acceptance.ps1 -Token "<service-token>" -AppPath ".\TakSklad\TakSklad.exe"
```

Если проверка идёт из исходников:

```powershell
.\tools\windows_backend_acceptance.ps1 -Token "<service-token>" -AppPath ".\main.py"
```

Если команда запускается из корня проекта и нужно принудительно взять исходники, а не лежащий рядом exe:

```powershell
.\tools\windows_backend_acceptance.ps1 -Token "<service-token>" -UsePython
```

Helper по умолчанию смотрит на `https://api.taksklad.uz`, проверяет, что `APP_VERSION` не ниже `2.0.0` и `APP_BUILD_LABEL = MVP 2.0`, и предпочитает `.venv\Scripts\python.exe`. Для `TakSklad.exe` helper требует `build_manifest.json` из свежего test archive и сверяет `app_version` + `app_build_label`; старый рабочий ярлык `1.1.7` без manifest будет остановлен до запуска.

1. Запустить тестовую копию TakSklad на Windows через helper выше.
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
14. Открыть `Возвраты`.
15. Найти завершённую заявку по ШК/номеру SkladBot.
16. Нажать `Принять возврат`.
17. Проверить, что заявка появилась в `Последние возвраты`.
18. Повторно найти эту же заявку и убедиться, что повторное принятие заблокировано.

### Ожидаемый Результат

- приложение не зависает;
- на экране статистики видно состояние backend;
- нет ложного `Дождитесь завершения текущей операции`;
- КИЗы сохраняются;
- заказ завершается;
- печать появляется после завершения заказа, даёт выбрать принтер и размер этикетки;
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

- Не включать `mandatory=true`.
- Не публиковать новый GitHub Release поверх `v2.0.0` без повторной проверки.
- Не запускать Windows release workflow.
- Не проверять на реальных заказах без отдельного подтверждения.

Для быстрого отката тестового запуска:

```powershell
.\tools\windows_backend_acceptance.ps1 -Clear
```

## 5. Критерий Закрытия Goal

Goal можно закрывать только после трёх подтверждений:

1. Telegram import прошёл от реального пользовательского аккаунта.
2. SkladBot matching проверен на живой заявке `3PL отгрузка`.
3. Windows desktop acceptance прошёл на тестовой Windows-копии.

До этого PR должен оставаться draft.
