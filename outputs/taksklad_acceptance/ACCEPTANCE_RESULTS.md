# TakSklad 2.0 Acceptance Results

Дата проверки: 2026-05-31

Проверяющий: Codex, локальная техническая проверка

Среда:

- VDS: `https://api.taksklad.uz`
- Desktop source/build: текущая ветка `feature/mvp-telegram-logistics-skladbot`, source-run
- Windows ПК: не проверялся
- Сканер: не проверялся
- Принтер: не проверялся

Маркер проверки: `ACCEPTANCE TELEGRAM 20260531`

Файл Telegram import: `TakSklad_Telegram_Acceptance_2026-05-31.xlsx`

SHA-256 Excel: `204b932a704b39294b513a95964844db1ed74d028e3daff13beef3ab09ec98fd`

## 1. Preflight

- [x] `.venv/bin/python tools/release_preflight.py` вернул `status=ok`.
- [x] `version.json` не менялся и остался на `1.1.7`.
- [x] В Git нет tracked runtime/secret-файлов.

Заметки:

```text
Локально проверено 2026-05-31. Public backend health отвечает status=ok.
```

## 2. Telegram Import

- [ ] В Telegram нажата кнопка `Дата отгрузки`.
- [ ] Отправлена дата `31.05.2026`.
- [ ] Отправлен Excel-файл как документ.
- [ ] Бот ответил без ошибки.
- [ ] `verify_acceptance_marker.sh` вернул `orders=1`.
- [ ] Логистический отчёт по дате выгружается.
- [ ] КИЗ по файлам не показывает незавершённые файлы.

Команда проверки:

```bash
cd /opt/taksklad/app
./deploy/vds/verify_acceptance_marker.sh "ACCEPTANCE TELEGRAM 20260531" --expect-orders 1
```

Фактический результат:

```text
Не выполнено в этом проходе. Нужен ручной входящий Telegram upload от пользовательского аккаунта.
```

## 3. SkladBot Matching

- [ ] Менеджер создал живую заявку `3PL отгрузка`.
- [ ] Диагностика нашла ровно одно совпадение.
- [ ] Дата отгрузки/выгрузки совпала.
- [ ] Клиент совпал после нормализации.
- [ ] Тип оплаты совпал.
- [ ] Товары совпали по цвету/формату.
- [ ] Количество совпало в блоках.
- [ ] Адрес использован только как мягкий признак.

Команда диагностики:

```bash
cd /opt/taksklad/app
./deploy/vds/diagnose_skladbot_match.sh --marker "ACCEPTANCE TELEGRAM 20260531" --limit 5 --request-limit 20
```

Фактический результат:

```text
Не выполнено в этом проходе. Нужна живая заявка SkladBot по acceptance-заказу.
```

## 4. Windows Desktop Acceptance

- [ ] Собран свежий test archive через `tools\build_windows_test_archive.ps1`.
- [ ] Запуск выполнен из test archive, не из старого ярлыка `1.1.7`.
- [ ] `windows_backend_acceptance.ps1 -CheckOnly` прошёл.
- [ ] Desktop открылся без зависания.
- [ ] Список заказов обновился из backend.
- [ ] На экране статистики видно `Backend: online, список из VDS`.
- [ ] Найден заказ `ACCEPTANCE TELEGRAM 20260531`.
- [ ] Во время сканирования обновление списка не блокирует ввод.
- [ ] Отсканированы тестовые КИЗы:

- [ ] `WIN-KIZ-ACCEPT-001`
- [ ] `WIN-KIZ-ACCEPT-002`
- [ ] `WIN-KIZ-ACCEPT-003`

- [ ] Дубль КИЗа не принят.
- [ ] Завершение недосканированного заказа запрещено.
- [ ] Завершение досканированного заказа прошло.
- [ ] После завершения заказа появилось окно печати.
- [ ] Печать не открывает браузер.
- [ ] Размеры этикеток доступны: `100x100`, `100x150`, `75x50`, `58x40`.
- [ ] `Enter` подтверждает печать, `Esc` отменяет.
- [ ] Завершение смены сформировало КИЗ-отчёт.
- [ ] Окно `Возвраты` открывается.
- [ ] По ШК/номеру завершённой заявки находится архивный заказ.
- [ ] `Принять возврат` переводит заказ в возврат и обновляет список `Последние возвраты`.
- [ ] Повторное принятие той же заявки запрещено.

Команда проверки backend после Windows:

```bash
cd /opt/taksklad/app
./deploy/vds/verify_acceptance_marker.sh "ACCEPTANCE TELEGRAM 20260531" --expect-orders 1 --expect-scans 3 --expect-completed
```

Фактический результат:

```text
Не выполнено в этом проходе. Нужна физическая Windows-приёмка.
```

## 5. Cleanup

- [ ] Dry-run cleanup показал только тестовые данные.
- [ ] Cleanup с `--apply` выполнен.
- [ ] Повторная проверка маркера не показывает активные тестовые заказы.

Команды:

```bash
cd /opt/taksklad/app
./deploy/vds/cleanup_acceptance_marker.sh "ACCEPTANCE TELEGRAM 20260531"
./deploy/vds/cleanup_acceptance_marker.sh "ACCEPTANCE TELEGRAM 20260531" --apply
```

Фактический результат:

```text
Не выполнялось: ручной acceptance import в этом проходе не создавал тестовые данные.
```

## 6. Defects / Known Issues

| ID | Сценарий | Симптом | Severity | Решение | Статус |
| --- | --- | --- | --- | --- | --- |
| KI-001 | Ручная приёмка | Нет фактического входящего Telegram upload от пользователя | manual-gate | Провести по acceptance kit | open |
| KI-002 | Windows desktop | Не проверены сканер, печать, закрытие смены на Windows | manual-gate | Провести Windows acceptance | open |

## 7. Go / No-Go

- [ ] Telegram import принят.
- [ ] SkladBot matching принят.
- [ ] Windows desktop acceptance принят.
- [x] Критичных дефектов нет.
- [x] Rollback понятен.
- [x] `version.json` всё ещё не менялся.

Итог:

- [ ] GO к подготовке release 2.0.
- [x] NO-GO, релиз откладывается.

Комментарий:

```text
Техническая подготовка продвинута, но release 2.0 нельзя готовить до живого Telegram upload, живого SkladBot match и Windows desktop acceptance.
```
