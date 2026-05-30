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

Если kit нужно пересобрать:

```bash
cd /Users/anton/Documents/work/TakSklad
.venv/bin/python tools/prepare_acceptance_kit.py
```

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

Ожидаемо:

- `status`: `ok`;
- `orders`: `1`;
- `items`: `2`;
- `planned_blocks`: `3`;
- `imports`: минимум `1`;
- `pending_events`: `0`.

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

Перед запуском проверить связь с VDS:

```powershell
.\tools\windows_backend_acceptance.ps1 -CheckOnly -Token "<service-token>"
```

Запустить тестовую копию:

```powershell
.\tools\windows_backend_acceptance.ps1 -Token "<service-token>" -AppPath ".\TakSklad.exe"
```

Если проверка идёт из исходников:

```powershell
.\tools\windows_backend_acceptance.ps1 -Token "<service-token>" -AppPath ".\main.py"
```

1. Запустить тестовую копию TakSklad на Windows через helper выше.
2. Убедиться, что приложение открылось без обновления `version.json`.
3. Обновить список заказов.
4. Найти заказ `ACCEPTANCE TELEGRAM 20260531`.
5. Выбрать заказ.
6. Отсканировать 3 тестовых КИЗа:
   - `WIN-KIZ-ACCEPT-001`;
   - `WIN-KIZ-ACCEPT-002`;
   - `WIN-KIZ-ACCEPT-003`.
7. Завершить заказ.
8. Проверить, что появляется печать/сводный лист после завершения юрлица.
9. Проверить, что заказ ушёл из активных.
10. Проверить завершение смены.
11. Отдельно проверить: обновление списка во время сканирования не блокирует ввод КИЗов.

### Ожидаемый Результат

- приложение не зависает;
- нет ложного `Дождитесь завершения текущей операции`;
- КИЗы сохраняются;
- заказ завершается;
- печать появляется после завершения заказа;
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

Ожидаемо:

- `status`: `ok`;
- `completed_orders`: `1`;
- `active_orders`: `0`;
- `planned_blocks`: `3`;
- `scanned_blocks`: `3`;
- `scan_codes`: `3`;
- `incomplete_items`: `[]`.

## 4. Что Не Делать Во Время Acceptance

- Не менять `version.json`.
- Не создавать GitHub Release.
- Не запускать Windows release workflow.
- Не отправлять push-уведомления рабочим ПК.
- Не проверять на реальных заказах без отдельного подтверждения.

Для быстрого отката тестового запуска:

```powershell
.\tools\windows_backend_acceptance.ps1 -Clear
```

## 5. Критерий Закрытия Goal

Goal можно закрывать только после двух подтверждений:

1. Telegram import прошёл от реального пользовательского аккаунта.
2. Windows desktop acceptance прошёл на тестовой Windows-копии.

До этого PR должен оставаться draft.
