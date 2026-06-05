# TakSklad 2.0 Acceptance Results

Дата проверки:

Проверяющий:

Среда:

- VDS: `https://api.taksklad.uz`
- Desktop source/build:
- Windows ПК:
- Сканер:
- Принтер:

Маркер проверки: `ACCEPTANCE TELEGRAM 20260531`

Файл Telegram import: `TakSklad_Telegram_Acceptance_2026-05-31.xlsx`

SHA-256 Excel: `204b932a704b39294b513a95964844db1ed74d028e3daff13beef3ab09ec98fd`

## 1. Preflight

- [ ] `.venv/bin/python tools/release_preflight.py` вернул `status=ok`.
- [ ] `version.json` указывает на `2.0.8`, `mandatory=true`, ссылки и SHA заполнены.
- [ ] В Git нет tracked runtime/secret-файлов.

Заметки:

```text

```

## 2. Telegram Import

- [ ] В Telegram нажата кнопка `Дата отгрузки`.
- [ ] Отправлена дата `31.05.2026`.
- [ ] Отправлен Excel-файл как документ.
- [ ] Бот ответил без ошибки.
- [ ] `verify_acceptance_marker.sh` вернул `orders=1`.
- [ ] Логистический отчёт по дате выгружается.
- [ ] `Выгрузка КИЗов` не показывает незавершённые файлы.

Команда проверки:

```bash
cd /opt/taksklad/app
./deploy/vds/verify_acceptance_marker.sh "ACCEPTANCE TELEGRAM 20260531" --expect-orders 1
```

Фактический результат:

```text

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

```

## 6. Defects / Known Issues

| ID | Сценарий | Симптом | Severity | Решение | Статус |
| --- | --- | --- | --- | --- | --- |
| | | | | | |

## 7. Go / No-Go

- [ ] Telegram import принят.
- [ ] SkladBot matching принят.
- [ ] Windows desktop acceptance принят.
- [ ] Критичных дефектов нет.
- [ ] Rollback понятен.
- [ ] `version.json` проверен и `mandatory=true`.

Итог:

- [ ] GO к подготовке release 2.0.
- [ ] NO-GO, релиз откладывается.

Комментарий:

```text

```

Машинная проверка заполненного результата:

```bash
cd /Users/anton/Documents/work/TakSklad
# Заполнить существующий ACCEPTANCE_RESULTS.md фактическими результатами.
.venv/bin/python tools/release_go_no_go.py --results outputs/taksklad_acceptance/ACCEPTANCE_RESULTS.md
```
