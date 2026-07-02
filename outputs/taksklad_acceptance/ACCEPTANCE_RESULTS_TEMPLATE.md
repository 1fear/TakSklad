# TakSklad 2.0 Acceptance Results

Дата проверки:

Проверяющий:

Среда:

- VDS: `https://api.taksklad.uz`
- Backend:
- Desktop source/build:
- Windows ПК:
- Сканер:
- Принтер:

Маркер проверки:

Файл Telegram import:

SHA-256 Excel:

## 1. Preflight

- [ ] Public `https://api.taksklad.uz/health` вернул `status=ok`, `version=2.0.25`, `environment=production`.
- [ ] Public `https://api.taksklad.uz/ready` вернул `status=ok`, DB OK, migrations head `20260701_0007`.
- [ ] GitHub Actions `CI` и `Deploy Production` на `main` по текущему head зеленые.
- [ ] В Git нет tracked runtime/secret-файлов в текущей проверке.

Заметки:

```text

```

## 2. Telegram Import

- [ ] Боевые импорты из Telegram прошли в backend/Postgres.
- [ ] Telegram worker обработал документы без заявленных оператором ошибок.
- [ ] Импорты создали/обновили заказы в БД.
- [ ] Live `/ready` не показывает recent import errors.

Команда проверки:

```bash
curl -fsS --max-time 15 https://api.taksklad.uz/ready
```

Фактический результат:

```text

```

## 3. SkladBot Matching

- [ ] Боевой контур создавал заявки SkladBot.
- [ ] SkladBot create path отработал без заявленных оператором ошибок.
- [ ] Production queue содержит завершенные `skladbot_request_create` events.
- [ ] Адрес/клиент/товары/количество проверялись фактическим боевым процессом.

Команда диагностики:

```bash
curl -fsS --max-time 15 https://api.taksklad.uz/ready
```

Фактический результат:

```text

```

## 4. Windows Desktop Acceptance

- [ ] Боевой Windows/операторский сценарий прошел на текущем контуре.
- [ ] Сканирование КИЗов прошло в бою.
- [ ] Ошибки сканирования/дедупликации не заявлены.
- [ ] Завершение основного складского потока не заблокировано.
- [ ] Smartup auto export, Telegram import, DB import, KIZ scan и SkladBot create проверены одним live workflow.

Команда проверки backend после Windows:

```bash
curl -fsS --max-time 15 https://api.taksklad.uz/ready
```

Фактический результат:

```text

```

## 5. Cleanup

- [ ] Синтетические acceptance test data не создавались.
- [ ] Cleanup marker не требуется для production smoke.
- [ ] Live queue после smoke без stale processing.

Команды:

```bash
curl -fsS --max-time 15 https://api.taksklad.uz/ready
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
.venv/bin/python tools/release_go_no_go.py --results outputs/taksklad_acceptance/ACCEPTANCE_RESULTS.md
```
