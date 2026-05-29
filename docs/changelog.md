# Журнал изменений

Здесь фиксируются все правки в коде TakSklad: что менялось, в каком файле, зачем, и какие тесты это покрывают. Записи идут от новых к старым.

## 2026-05-30

### Реализованы backend endpoint'ы активных заказов, сканов и завершения заказа

**Файлы:** `backend/app/main.py`, `backend/app/orders_service.py`, `backend/app/models.py`, `backend/app/schemas.py`, `backend/requirements.txt`, `tests/test_backend_api_persistence.py`, `docs/*`.

**Что стало:**

- `GET /api/v1/orders/active` теперь возвращает реальные невыполненные заказы из БД с позициями.
- `POST /api/v1/scans` теперь пишет КИЗ в `scan_codes`, обновляет `scanned_blocks`, закрывает позицию при достижении плана и защищает от дублей.
- `POST /api/v1/orders/{order_id}/complete` теперь проверяет недосканированные обязательные позиции, закрывает заказ и пишет аудит.
- SQLAlchemy-модели можно поднимать в SQLite для быстрых тестов, при этом Postgres остаётся основной БД.
- Добавлена зависимость `httpx` для `FastAPI TestClient`.

**Проверки:**

- `.venv/bin/python -m unittest tests/test_backend_api_persistence.py` - 3 теста пройдены.
- `.venv/bin/python -m unittest discover -s tests` - 51 тест пройден.
- `.venv/bin/python -m py_compile main.py sitecustomize.py taksklad/__init__.py src/taksklad/*.py tests/*.py backend/app/*.py` - успешно.
- Локальный Docker/Postgres smoke прошёл полный сценарий: активный заказ, ранний отказ закрытия, два скана, дубль КИЗ, успешное закрытие заказа.
- Staging VDS обновлен и проверен через внешний HTTPS API тем же сценарием; временные smoke-данные удалены.

### Добавлен воспроизводимый Traefik-шаблон и зафиксирован VDS smoke-deploy

**Файлы:** `deploy/traefik/*`, `docs/implementation-log.md`.

**Что стало:**

- Добавлен `deploy/traefik/docker-compose.yml` для серверного Traefik с HTTPS, Docker provider и Let's Encrypt.
- Добавлен `deploy/traefik/.env.example` без секретов.
- Зафиксирован фактический VDS smoke-deploy: Docker/Compose, UFW, Traefik, `postgres`, `backend-api`, временный `sslip.io`-домен.
- Отдельно записано решение по Traefik: образ `v3.3` не работал с новым Docker API, сервер переведен на `traefik:v3.6`.

**Проверки:**

- На VDS `postgres` поднят и healthy.
- На VDS `backend-api` поднят.
- Внешний `GET /health` через HTTPS вернул `200`.
- Без Bearer-токена защищенный endpoint вернул `401`.
- С Bearer-токеном защищенный endpoint дошел до приложения и вернул ожидаемый MVP-ответ `501`.

### Зафиксирована локальная среда разработки ноутбука

**Файлы:** `docs/local-development-setup.md`, `docs/implementation-log.md`.

**Что стало:**

- Описана локальная настройка ноутбука для TakSklad: `.venv`, Python-зависимости, Docker CLI, Compose, Buildx, Colima, GitHub CLI.
- Зафиксированы команды для проверки тестов, backend compose config, локального запуска `postgres + backend-api` и остановки тестового стека.
- Уточнено, что рабочий `deploy/vds/.env` создаётся из `.env.example`, хранится локально и не попадает в Git.

**Проверки:**

- `.venv/bin/python -m unittest discover -s tests` - 47 тестов пройдены.
- `.venv/bin/python -m py_compile main.py sitecustomize.py taksklad/__init__.py src/taksklad/*.py tests/*.py backend/app/*.py` - успешно.
- Docker smoke `hello-world` - успешно.
- Локальный VDS compose smoke: `postgres + backend-api` собраны и подняты, `/health` отвечает, стартовые таблицы Postgres созданы.

### Добавлен VDS/backend MVP-каркас без Windows-релиза

**Файлы:** `.gitignore`, `backend/*`, `deploy/vds/*`, `tests/test_backend_skeleton.py`, `docs/*`.

**Что стало:**

- Добавлен FastAPI backend shell для будущего серверного TakSklad.
- Реализован `GET /health`.
- Зафиксированы контрактные endpoint'ы для активных заказов, сканов, завершения заказа, импортов и дневного отчёта. Реальной бизнес-логики в них пока нет, они возвращают `501 Not Implemented`.
- Добавлены настройки backend через env и опциональная проверка сервисного Bearer-токена.
- Добавлены SQLAlchemy-модели и стартовая PostgreSQL-схема для заказов, позиций, КИЗов, импортов, очередей, пользователей и аудита.
- Добавлен Dockerfile и VDS Docker Compose под `postgres`, `backend-api`, `adminer` и Traefik routing.
- Добавлен `.env.example` без реальных секретов.
- `.gitignore` теперь игнорирует реальные `.env`-файлы.
- Добавлены тесты backend-скелета, которые проверяют структуру, настройки, SQL-схему и compose без Docker.

**Что специально не менялось:**

- `version.json` не обновлялся и остаётся на `1.1.7`.
- Windows-архив, GitHub Release, tag и push-уведомления не создавались.
- Desktop пока не подключён к backend.

**Проверки:**

- `.venv/bin/python -m unittest tests/test_backend_skeleton.py` - 5 тестов пройдены.
- `.venv/bin/python -m unittest discover -s tests` - 47 тестов пройдены.
- `.venv/bin/python -m py_compile main.py sitecustomize.py taksklad/__init__.py src/taksklad/*.py tests/*.py backend/app/*.py` - успешно.
- `git diff --check -- . ':!archive/**'` - успешно.
- FastAPI smoke: `/health` вернул `200`, контрактный endpoint активных заказов вернул ожидаемый `501`.
- Docker Compose runtime не проверен локально: Docker CLI отсутствует в текущем окружении.

## 2026-05-29

### Начато разбиение `main.py`

**Файлы:** `src/taksklad/main.py`, `src/taksklad/http_client.py`, `src/taksklad/update_service.py`, `src/taksklad/printing.py`, `src/taksklad/pending_store.py`, `src/taksklad/reports.py`, `src/taksklad/ui_widgets.py`, `src/taksklad/telegram_service.py`, `src/taksklad/app_telegram.py`, `src/taksklad/app_updates.py`, `src/taksklad/app_imports.py`, `src/taksklad/app_catalog.py`, `src/taksklad/app_control_panel.py`, `src/taksklad/app_skladbot.py`, `src/taksklad/app_printing.py`, `src/taksklad/app_day_end.py`, `src/taksklad/duplicate_codes.py`, `tests/test_daily_report.py`, `docs/*`.

**Что стало:**

- HTTPS-запросы, автообновление, печать, локальные очереди/backup, отчеты и кнопка UI вынесены из `main.py` в отдельные модули.
- Telegram-сервис, Telegram UI/polling, UI-логика автообновления, ручной Excel-импорт, справочник товаров, контрольная панель, SkladBot-оркестрация, настройки/очередь печати, завершение дня и форматирование дублей КИЗ вынесены в отдельные модули.
- `main.py` уменьшен с 4190 до 1172 строк и остается главным образом Tkinter-оркестратором сканирования и основного UI.
- Тест дневного отчета обновлен под новый модуль `taksklad.reports`.

**Проверки:**

- `.venv/bin/python -m py_compile main.py sitecustomize.py taksklad/__init__.py src/taksklad/*.py tests/*.py` - успешно.
- `.venv/bin/python -m unittest discover -s tests` - 35 тестов пройдены.

### Локально структурирован репозиторий и закреплено название TakSklad

**Файлы:** `src/taksklad/*`, `main.py`, `taksklad/__init__.py`, `sitecustomize.py`, `.github/workflows/build-windows-release.yml`, `.gitignore`, `docs/*`, `README.md`, `tests/*`.

**Что стало:**

- Кодовые модули перенесены из корня в пакет `src/taksklad/`.
- Корневой `main.py` оставлен как тонкий запускатель для разработки и PyInstaller.
- Добавлен bridge-пакет `taksklad/`, чтобы тесты и локальные команды импортировали код из `src/`.
- Старые локальные артефакты перенесены в `archive/repo-cleanup-20260529/`: логи, backup JSON, старые credentials-снимки, `reports/`, `exports/`, `scan_backups/`, legacy runtime JSON и cache.
- В корне оставлены активные `credentials.json` и `TakSklad_data.json`, чтобы не сломать локальный запуск.
- В рабочих файлах удалены упоминания старого названия; официальное имя проекта и приложения — `TakSklad`.

**Проверки:**

- `.venv/bin/python -m unittest discover -s tests` - 35 тестов пройдены.
- `.venv/bin/python -m py_compile main.py sitecustomize.py taksklad/__init__.py src/taksklad/*.py tests/*.py` - успешно.

### `version.json` закреплен на рабочей стабильной версии 1.1.7

**Файл:** `version.json`.

**Что было:**

В манифесте стояло `latest_version = 1.1.17` и `min_supported_version = 1.1.17`. Для рабочих компьютеров на `1.1.7` это означало, что приложение видит себя ниже минимально поддерживаемой версии и может предлагать/требовать обновление.

**Что стало:**

- `latest_version = 1.1.7`
- `min_supported_version = 1.1.7`
- `mandatory = false`
- `download_url` / SHA очищены, чтобы стабильный манифест не ссылался на артефакты другой версии.

**Зачем:** рабочая линия склада остается на стабильной `1.1.7`, пока новая стабилизация не проверена вручную. Новый архив на этом этапе не собирается и не выкатывается.

### Обновление списка заказов больше не блокирует сканирование

**Файлы:** `main.py`, `docs/implementation-log.md`.

**Что было:**

Кнопка `Обновить` использовала общий флаг `operation_in_progress`. Пока Google Sheets загружал список заказов, приложение считало себя полностью занятым, поэтому сканер получал ошибку `Дождитесь завершения текущей операции`, даже если фактически шла только фоновая загрузка списка.

**Что стало:**

- Добавлено отдельное состояние `refresh_in_progress` для фонового обновления заказов.
- Ручное обновление при выбранной позиции больше не сбрасывает текущий заказ и не блокирует ввод КИЗов.
- Сохранение текущей позиции проверяется в момент завершения обновления: если пользователь выбрал заказ уже во время фоновой загрузки, выбор не сбрасывается.
- Повторное обновление во время уже идущего обновления показывает отдельное сообщение.
- Фоновый SkladBot не стартует параллельно с ручным обновлением, активным сканированием или блокирующей операцией.
- `fetch_sheet_data` и `fetch_sheet_data_with_sync` переиспользуют уже загруженные строки Google Sheets для списка существующих КИЗов, вместо второго чтения всего листа.
- Добавлен cooldown для фоновых Google Sheets обращений после `429`/timeout. В первую очередь это защищает Telegram lock/state от частых повторов, которые добивают квоту.
- Добавлен SkladBot `dry_run=True` для безопасной проверки сопоставления без записи в Google Sheets.
- Добавлен SkladBot API timeout `SKLADBOT_API_TIMEOUT_SECONDS = 8`, чтобы фоновый синк не зависал надолго на медленных деталях заявки.
- Обновлен устаревший тест SkladBot: сохраненный `requests_limit=100` больше не ожидается как рабочий лимит, потому что код держит минимум 500 заявок для 14-дневного окна.
- Заведен `docs/implementation-log.md` для фиксации сделанного, нерешенного, ошибок, решений и ручных проверок.

**Зачем:** можно обновлять список заказов и продолжать сканирование на выбранной позиции, не ловя ложное состояние "приложение занято".

**Проверка:** требуется ручная проверка в UI: выбрать заказ, начать сканирование, нажать `Обновить`, убедиться, что КИЗы продолжают приниматься, а текущая позиция не сбрасывается.

**Фактические проверки:**

- UI-smoke без реальных сетевых вызовов пройден: КИЗ принят во время фонового обновления, текущая позиция сохранена.
- SkladBot dry-run прошел без записи. В текущем `data` нет активных невыполненных заказов, поэтому сопоставлять нечего.
- Read-only SkladBot API с лимитом 10 заявок прошел, примеры заявок содержат дату выгрузки, получателя и товары.

### Добавлена единая база знаний и roadmap складской экосистемы

**Файлы:** `docs/project-knowledge-base.md`, `docs/warehouse-ecosystem-roadmap.md`.

**Что добавлено:**

- `project-knowledge-base.md` - единый документ по текущему состоянию TakSklad: архитектура, модули, Google Sheets, локальные файлы, SkladBot, Telegram, очереди, автообновление, логи, известные проблемы, запреты и ближайшая повестка.
- `warehouse-ecosystem-roadmap.md` - развёрнутый план перехода от desktop-приложения к складской экосистеме: VPS, Docker Compose, PostgreSQL, backend API, Telegram worker, SkladBot worker, report/backup worker, web panel, миграционные этапы и риски.

**Зачем:** собрать проектные знания в `docs/`, чтобы дальнейшая разработка не держалась в переписке. Эти документы фиксируют текущую систему и направление расширения в WMS-экосистему.

**Важно по безопасности:** документы не содержат Telegram token, Google private key, реальные chat_id, пароли VPS или другие секреты.

## 2026-05-28

### `version.json` переключён на `onefile_exe`, чтобы остановить активный цикл автообновления на ноуте

**Файл:** `version.json`.

**Что было:**

После фикса автообновления в коде (26.05) на ноуте пользователя всё ещё крутился старый билд без правок, поэтому продолжал срабатывать цикл: каждый старт → старый exe видит `package_type: onedir_zip` → решает «нужен переход на onedir» → запускает PowerShell-updater → `self.destroy()` → PowerShell делает `Start-Process -FilePath $NewExe` → новый старт → goto 1. Перевыкатывать билд можно только через сборку, а пока — пользователь не может закрыть приложение.

**Что стало:**

В манифесте `version.json`:

```diff
-  "mandatory": true,
-  "package_type": "onedir_zip",
+  "mandatory": false,
+  "package_type": "onefile_exe",
```

После push'a в `main`-ветку старый exe на ноуте при следующем запуске прочитает обновлённый манифест:

- `update_available` = `compare_versions("1.1.17", "1.1.17") < 0` → False
- `below_min_version` = `compare_versions("1.1.17", "1.1.17") < 0` → False
- `package_transition_required` = `manifest_targets_onedir(...)` → False (потому что `'onefile_exe'` нет в `('onedir','onedir_zip','zip')`)

Все три условия в `handle_update_info` дают False → ранний return → `start_auto_update` не вызывается → цикл прерывается.

**Зачем:** ноут починится без пересборки exe. Манифест — самый лёгкий способ дотянуться до уже задеплоенной версии без перекомпиляции.

**Когда менять обратно:** после следующего билда (1.1.18+) с моими правками `handle_update_info`, можно вернуть `package_type: onedir_zip` и `mandatory: true`. Новый exe уже умеет показывать промпт и не входить в цикл, поэтому старая модель «жёсткого обновления» снова безопасна.

**Шаги для пользователя:**

1. `git add version.json && git commit -m "..." && git push` из этой машины.
2. На ноуте через Диспетчер задач убить все процессы `TakSklad.exe`, `cmd.exe` и `powershell.exe`, связанные с обновлением (правый клик → «Завершить дерево процессов»).
3. Удалить из `%TEMP%` остатки скриптов: `TakSklad_updater_*.bat`, `TakSklad_updater_*.ps1`, `TakSklad_rename_*.bat`.
4. Запустить `TakSklad.exe` — он не должен зайти в цикл обновления.

### `maybe_rename_windows_executable`: убрана вторая лестница цикла перезапуска

**Файлы:** `main.py` (функция `maybe_rename_windows_executable`).

**Что было:**

В правке 26.05 я закрыл цикл автообновления в `create_windows_exe_updater` и `create_windows_onedir_updater` — при ошибке установки они больше не запускают старый exe. Но **пропустил третье место с тем же антипаттерном** — `.bat`-скрипт внутри `maybe_rename_windows_executable`.

Эта функция срабатывает, когда пользователь скачал приложение под нестандартным именем (например `TakSklad-windows-x64.exe` вместо ожидаемого `TakSklad.exe`). Скрипт копирует OLD→NEW, запускает NEW, удаляет OLD. При ошибке копирования (NEW занят антивирусом, нет прав на запись и т.п.) бат делал `start "" "%OLD%"` — запускал OLD под старым именем. OLD при старте снова входил в `maybe_rename_windows_executable` (потому что basename(OLD) != APP_EXECUTABLE_NAME), снова создавал .bat, копия снова падала, снова `start OLD` → бесконечный цикл «приложение само открывается после закрытия».

В отличие от auto-update loop, этот срабатывал даже когда пользователь ничего не делал с обновлениями — только при «нестандартном» имени исполняемого файла.

**Что стало:**

Из `.bat`-скрипта убран `start "" "%OLD%"` в ветке ошибки `copy /Y`. Теперь скрипт пишет в `TakSklad_update.log` (в `docs/`):
```
[date time] Не удалось создать "%NEW%", перезапуск старого exe отключён во избежание цикла
```
и выходит с кодом 1. Приложение не открывается заново — пользователь видит, что оно не стартовало, смотрит лог, решает что делать (часто — переименовать exe вручную в `TakSklad.exe`).

**Зачем:** закрыть ту же яму, что и в updater'ах. Теперь во всех трёх местах, где `.bat`/`.ps1` могли запустить старый exe из ветки ошибки, такого больше нет:
1. `create_windows_exe_updater` — закрыто 26.05
2. `create_windows_onedir_updater` — закрыто 26.05
3. `maybe_rename_windows_executable` — закрыто сейчас

**Тесты:** покрыто compile-check'ом `main.py`. Поведение завязано на Windows `.bat`-runtime, full regression возможен только на Windows-машине.

### SkladBot: окно синхронизации расширено до 14 дней, фильтр в детали идёт по `unloading_date`

**Файлы:** `config.py`, `skladbot.py`.

**Что было:**

Лог `2026-05-28 14:02:57 [INFO] SkladBot: список=500, к проверке за сегодня/вчера=0` чётко показал: из 500 заявок SkladBot **ни одна** не проходит первичный фильтр окна. Причина в комбинации:

1. `SKLADBOT_SYNC_LOOKBACK_DAYS = 1` — окно «вчера и сегодня».
2. `list_item_in_sync_window` фильтрует по `created_at` — когда заявка была создана в SkladBot, не когда она отгружается.
3. У оператора склада типовой цикл: заявка создаётся в SkladBot за 2–4 дня до отгрузки. Сегодня 28.05, отгрузки 26.05 — заявки созданы 24–26.05. С окном в 1 день они уже за бортом.

После первичного фильтра то же самое делает `request_in_sync_window` уже на полной детали заявки — снова по `created_at` вместо `unloading_date`. То есть даже если первичный фильтр пропустил, повторная проверка отсеет.

Видимое следствие — «номера заявок не подтягиваются»: матчингу нечего проверять, все группы помечаются `Не найдено`.

**Что стало:**

- `SKLADBOT_SYNC_LOOKBACK_DAYS = 14` (было 1). Покрывает обычный логистический цикл, при котором заявка создаётся за 1–7 дней до отгрузки.
- `request_in_sync_window` теперь смотрит сначала на `unloading_date` (дата отгрузки из детали), и только если её нет — на `created_at`. Это концептуально правильно: нам важна дата отгрузки, не дата создания.
- Первичный фильтр `list_item_in_sync_window` остаётся на `created_at`, потому что в листинге заявок SkladBot нет `unloading_date` (он только в детали). Но окно стало достаточно широким, чтобы потенциально интересные заявки прошли — а точечный фильтр по детали отсеет лишнее.
- Расширен диагностический лог `fetch_candidate_requests`: теперь пишет фактический разброс `created_at` всех 500 заявок и размер окна — `SkladBot: список=500 (created_at 14.05.2026..28.05.2026), окно=14 дн., к проверке=N, активных=A, завершённых/архивных=C`. Сразу видно, не отсекает ли окно полезные строки.

**Зачем:** убрать ситуацию «0 заявок к проверке», когда в SkladBot реально есть нужные заявки, просто на 3+ дня старше окна.

**Регрессионный риск:** при увеличении окна с 1 до 14 дней первичный фильтр пропускает примерно в 14 раз больше заявок (грубая оценка). Каждая из них требует одного `GET /requests/show/{id}` для получения детали. При типовом сценарии (200 пропущенных вместо 15) и `request_delay_seconds = 0.05` это +10 секунд к синку. Допустимо для периодической задачи с интервалом 10 минут. Если станет узким местом — можно вернуть `SKLADBOT_SYNC_LOOKBACK_DAYS` ближе к 7.

**Тесты:** покрыто прогоном существующих регрессионных тестов в `tests/test_skladbot_sync.py` — все 9 моих проходят, включая `test_fetches_details_only_for_today_and_yesterday_requests` (он передаёт `today` и `lookback_days` явно в `fetch_candidate_requests`, поэтому смена дефолта на него не влияет).

### SkladBot матчинг клиента стал устойчив к кавычкам и пунктуации + диагностический лог результата

**Файлы:** `skladbot.py`, `skladbot_sync.py`, `tests/test_skladbot_sync.py`.

**Что было:**

После правки 26.05 (строгое сравнение клиента через `normalize_lookup_text`) появилась обратная проблема: матчинг стал слишком жёстким. В Excel клиент часто записан с кавычками — `"MARKET AL-KABIR" MChJ` — а SkladBot отдаёт recipient без кавычек или с типографскими `«»`. `normalize_lookup_text` снимал регистр и лишние пробелы, но кавычки, дефисы и точки сохранял. Любое такое расхождение валило строгое равенство, и заявка помечалась `Не найдено`.

В лог при этом писалось только «список=500, к проверке за сегодня/вчера=58» — сколько именно групп нашлось / не нашлось не было видно. Диагностика вслепую.

**Что стало:**

- В `skladbot.py` добавлена функция `normalize_company_name(value)`. Делает то же что `normalize_lookup_text`, плюс удаляет ВСЕ небуквенно-цифровые символы (кавычки `"'«»“”„`, дефисы, точки, запятые, скобки) и схлопывает пробелы. Слова и цифры сохраняются и должны совпадать один-в-один.
- `request_matches_order_group` теперь сравнивает клиента через `normalize_company_name` вместо `normalize_lookup_text`. Семантика прежняя — нужен тот же контрагент. Терпимость только к пунктуации.
- В `sync_skladbot_request_numbers` после прохода всех групп пишется итоговый INFO в лог:
  ```
  SkladBot sync: групп=X, заявок-кандидатов=Y, matched=A, not_found=B, multiple=C, ячеек обновлено=D
  ```
  Если есть `not_found` — отдельной строкой выводятся до 5 примеров с датой, клиентом и числом товаров. Это позволяет сразу видеть в логе, кто не матчится, и проверить написание в SkladBot.

**Примеры что теперь матчится:**

| Excel (Клиент) | SkladBot (recipient) | Раньше | Сейчас |
|---|---|---|---|
| `"MARKET AL-KABIR" MChJ` | `MARKET AL-KABIR MChJ` | Не найдено | Найдено |
| `"MARKET AL-KABIR" MChJ` | `«MARKET AL-KABIR» MChJ` | Не найдено | Найдено |
| `ООО "Аэропорт"` | `ООО Аэропорт` | Не найдено | Найдено |
| `"MARKET AL-KABIR" MChJ` | `"MARKET AL-KEBIR" MChJ` | Не найдено | Не найдено (правильно — разные слова) |

**Зачем:** закрыть регрессию от прошлой правки, но не возвращаться к нечёткому токен-матчингу, из-за которого номера заявок сползали к соседним клиентам.

**Тесты (`tests/test_skladbot_sync.py`):**

- `test_matches_request_when_client_quotes_differ` — три кейса нормализации (`normalize_company_name` отдаёт одинаковый результат для разных кавычек, разные слова дают разные результаты) + полный e2e: заявка SkladBot без кавычек матчится с группой Excel с кавычками.
- Регрессионные `test_does_not_match_request_from_different_client`, `test_does_not_match_request_with_different_unloading_date`, `test_does_not_match_request_when_unloading_date_is_missing` продолжают проходить — защита от сползания номеров и нестрогих дат не нарушена.

Все 9 моих тестов в файле зелёные. Падает только предсуществующий `test_load_settings_respects_saved_skladbot_limits` — не связан с этой правкой (там `SKLADBOT_REQUESTS_LIMIT` в config.py подняли с 100 до 500, тест не обновили).

## 2026-05-26

### Логи приложения переехали в `docs/`

**Файлы:** `config.py`, `main.py`, `.gitignore`.

**Что было:**

`TakSklad.log` и `TakSklad_update.log` писались в корень папки приложения рядом с `main.py`, `credentials.json`, `*.json`-очередями и историческим `TakSklad.log`. Корневая папка постепенно превращалась в свалку: код, секреты, бэкапы Google Sheets, очереди, логи — всё в одном месте. Найти нужное по списку файлов становилось всё дольше.

**Что стало:**

- В `config.py` добавлены `LOG_DIR = os.path.join(APP_DIR, "docs")` и `UPDATE_LOG_FILE = os.path.join(LOG_DIR, "TakSklad_update.log")`. `LOG_FILE` перенесён в `docs/TakSklad.log`.
- В `main.py` перед `logging.basicConfig` теперь `os.makedirs(LOG_DIR, exist_ok=True)` — на первом запуске после клона/установки папка создаётся автоматически, без `FileNotFoundError`.
- Три места, где `TakSklad_update.log` собирался вручную через `os.path.join(APP_DIR, f"{APP_NAME}_update.log")` (`create_windows_exe_updater`, `create_windows_onedir_updater`, `maybe_rename_windows_executable`), теперь используют единый `UPDATE_LOG_FILE`.
- В `.gitignore` добавлена строка `docs/*.log` — `.md`-файлы в `docs/` остаются в git, а логи игнорируются.

**Зачем:** держать всё связанное с историей проекта и его диагностикой в одном месте (`docs/`). Поиск по корню становится короче, а changelog, документация и логи лежат рядом, что удобно при отладке.

**Что НЕ меняется:**

- Старые `TakSklad.log` и `TakSklad.log` в корне репозитория остаются как есть (исторические артефакты, уже в `.gitignore`). При следующем запуске приложение начнёт писать в `docs/TakSklad.log`. Корневой `TakSklad.log` можно удалить вручную, когда захочешь.
- `*.log` exclusion в robocopy внутри PowerShell-апдейтера сработает по filename-паттерну независимо от подкаталога, поэтому при обновлении `docs/TakSklad.log` и `docs/TakSklad_update.log` не затрутся.
- Расположение `credentials.json`, `TakSklad_data.json`, `pending_*.json` и прочих рабочих файлов не меняется — это отдельный вопрос (см. ранее раздел про беспорядок в корне).

**Тесты:** покрытие compile-check'ом `config.py` и `main.py`; полный прогон unit-тестов — 25/26 проходят (единственный fail `test_load_settings_respects_saved_skladbot_limits` пре-существующий, не из этой правки).

### Автообновление больше не зацикливается и спрашивает разрешение

**Файлы:** `main.py` (функции `handle_update_info`, `create_windows_exe_updater`, `create_windows_onedir_updater`), `config.py` (константа `UPDATE_RETRY_COOLDOWN_SECONDS`).

**Что было:**

На onefile-сборке `package_transition_required` возвращал True на каждом запуске (манифест `"package_type": "onedir_zip"`, а у клиента ещё onefile). Автообновление **запускалось без подтверждения пользователя**, скачивало ZIP, дёргало PowerShell-installer и закрывало приложение через `self.destroy()`. PowerShell в финале всегда делал `Start-Process -FilePath $NewExe`, запуская приложение заново. Если установка падала (robocopy не смог перезаписать файлы — антивирус, права доступа, занятый файл), в `catch`-блоке PowerShell-скрипт всё равно делал `Start-Process -FilePath $current_exe` — то есть запускал **старую** версию. Эта старая версия снова видела «нужно обновиться», снова запускала тот же updater, который снова падал, который снова запускал старый exe.

Пользователь видел это как «приложение постоянно открывается после закрытия» — приложение действительно закрывалось само (это `self.destroy()` после updater), но через пару секунд PowerShell-скрипт запускал его снова. Каждый запуск выглядел как «оно вернулось».

Аналогичная проблема была и в `create_windows_exe_updater` (онефайл-апдейтер): после 60 неудачных попыток `copy /Y` он делал `start "" "%APP%"` — запускал старый exe.

**Что стало:**

- В `handle_update_info` добавлен **диалог подтверждения** перед стартом обновления. Кнопка «Нет» — обновление откладывается. В сообщении показывается версия, причина (новая версия / минимально поддерживаемая / переход onefile→onedir) и текст из `update_info.message`.
- Добавлен **cooldown 1 час** на повторную попытку обновления той же версии (константа `UPDATE_RETRY_COOLDOWN_SECONDS` в `config.py`, состояние хранится в секции `update_skip_state` файла `TakSklad_data.json`). Если пользователь отказался или установка падала, следующая проверка той же версии не сработает раньше, чем через час. Это страховка на случай, если кто-то в `version.json` укажет более новую версию чем фактически выкатил.
- В `create_windows_onedir_updater` из `catch`-блока убран `Start-Process` старого exe. Теперь при падении updater пишет в `TakSklad_update.log` и выходит с кодом 1 — приложение **не запускается заново**. Пользователь видит, что оно не открылось, идёт в лог, понимает причину.
- В `create_windows_exe_updater` после 60 неудачных попыток `copy /Y` убран `start "" "%APP%"`. Поведение симметрично onedir-апдейтеру: пишем в лог, выходим, ничего не запускаем.

**Зачем:** прервать бесконечный цикл «закрытие → updater → старый exe → закрытие». Тихое автообновление без подтверждения — само по себе антипаттерн, а в комбинации с always-restart-on-failure оно превращалось в плохо отлаживаемый цикл.

**Как поведёт себя приложение после правки:**

- Если установка успешна (типовой случай) — поведение не изменилось: новый exe запускается через `Start-Process`.
- Если установка падает — приложение не открывается заново; в `TakSklad_update.log` появляется строка с причиной.
- Если пользователь нажал «Нет» в диалоге — приложение продолжает работать на текущей версии; следующий промпт появится не раньше чем через час и только при следующем запуске.

**Тесты:** покрытие через unit-тесты ограничено (логика завязана на `messagebox.askyesno`, PowerShell-скрипты и Windows-специфичные subprocess). Сейчас проверено compile-check'ом `main.py` и `config.py`. Полная регрессия — на реальной Windows-машине после следующей сборки.

**Откат, если что-то пошло не так:** временно вернуть автообновление без промпта можно, удалив блок `messagebox.askyesno(...)` в `handle_update_info` и условие `if not user_confirmed: return`. Cooldown можно отключить, поставив `UPDATE_RETRY_COOLDOWN_SECONDS = 0`.

### Telegram-бот: общий `last_update_id` в Google Sheets, чтобы два компа не обрабатывали один и тот же файл

**Файлы:** `sheets.py`, `main.py`, `tests/test_telegram_lock.py`.

**Что было:**

Single-listener lock через лист `_TakSklad_System` уменьшил, но не убрал двойную обработку. `process_telegram_updates` читал `telegram_state.last_update_id` **из локального** `TakSklad_data.json` через `load_telegram_state()`. Каждый компьютер вёл свой счётчик прочитанных Telegram-апдейтов. Плюс в `ensure_telegram_poll_lock` локальный кэш `telegram_lock_owned_until` мог 60 секунд считать, что lock у него, даже если сосед уже его перехватил.

В результате при гонке lock'а оба компьютера получали тот же `update_id`, оба обрабатывали один и тот же Excel-файл, оба отвечали в чат: один — «Файл не импортирован: занят другой операцией», второй — «Excel импортирован... Позиций загружено: 23». Данные уходили в чужую таблицу того компьютера, у которого был неактуальный `SPREADSHEET_ID` или старый `credentials.json`.

**Что стало:**

- В лист `_TakSklad_System`, строка 3 (после header и lock-строки), добавлена общая строка `telegram_state` со схемой `key/owner_id/owner_label/updated_at/updated_ts`. В `owner_id` пишется `last_update_id` строкой, в `owner_label` — компьютер, который последним подтвердил апдейт.
- Новые функции в `sheets.py`: `read_shared_telegram_state()` и `write_shared_telegram_state(last_update_id, owner_label, now_ts=None)`. Запись отказывается перезаписывать большее значение меньшим — это защищает от того, что параллельный писатель откатит чужой прогресс.
- `process_telegram_updates` теперь сначала читает общий `last_update_id` из Google Sheets, берёт максимум между общим и локальным, и передаёт его в `getUpdates(offset=last+1)`. Если общий state временно недоступен (Google Sheets лежит), откатывается на локальный кэш и продолжает работу.
- Внутри цикла обработки апдейтов добавлена явная проверка `update_id <= last_update_id → skip`. Это страхует от случая, когда Telegram всё-таки вернул уже обработанный апдейт.
- После обработки локальный state пишется как раньше, а общий — только если он строго больше текущего значения в Google.

**Зачем:** даже при кратком сбое lock'а второй компьютер больше не сможет повторно прогнать тот же `update_id` и записать дубль в Google Sheets.

**Что НЕ закрывается этим фиксом:** если оба компьютера запущены с разными `SPREADSHEET_ID` или разными `credentials.json` (например, на одном — старый ключ от katering, на другом — новый от taksklad), они пишут в РАЗНЫЕ таблицы и общий state у них тоже разный. В этом случае нужно сначала привести оба компьютера к одной конфигурации (см. ниже «Действия эксплуатанта»).

**Тесты (`tests/test_telegram_lock.py`):**

- `test_read_returns_zero_when_state_row_missing` — если строки state нет, читается 0.
- `test_write_creates_state_row_with_last_update_id` — первая запись создаёт строку.
- `test_write_refuses_to_go_backwards` — попытка записать меньшее значение игнорируется.
- `test_write_updates_when_new_value_is_greater` — большее значение перезаписывает старое.

**Действия эксплуатанта (не код, делается руками):**

1. На втором компьютере открыть `credentials.json` и проверить, что это актуальный service account TakSklad, а не старый ключ от другого проекта. Реальные `client_email`, `project_id` и `private_key_id` не фиксируются в документации и Git.
2. На втором компьютере открыть `config.py` строки 4-5 и проверить, что указан актуальный `SPREADSHEET_ID` рабочей таблицы TakSklad и `SHEET_NAME = "data"`. Реальный идентификатор таблицы сверяется по локальной рабочей конфигурации.
3. В Google Sheets открыть доступ к рабочей таблице для актуального service account TakSklad с ролью **Editor**.
4. После синхронизации перезапустить TakSklad на обоих компьютерах, чтобы оба прочитали общий `last_update_id` из листа `_TakSklad_System` с самого начала.

### Telegram-бот слушает только один компьютер через временный lock

**Файлы:** `config.py`, `main.py`, `sheets.py`, `tests/test_telegram_lock.py`.

**Что было:**

Если два компьютера запускали TakSklad с одним Telegram bot token, оба вызывали `getUpdates`, и Telegram возвращал `HTTP Error 409: Conflict`.

**Что стало:**

- добавлен временный lock в Google Sheets на листе `_TakSklad_System`;
- компьютер, который получил lock `telegram_poll`, опрашивает Telegram;
- второй компьютер пропускает Telegram polling и раз в 15 секунд пробует получить lock снова;
- lock обновляется раз в 20 секунд и считается устаревшим через 60 секунд;
- проверка lock выполняется в Telegram worker, не в UI-потоке, поэтому сканирование не ждёт Google Sheets;
- при закрытии приложения текущий владелец пытается освободить lock.

**Как быстро отключить:** в `telegram_settings` можно добавить `"single_listener_lock": false`; также есть общий флаг `TELEGRAM_SINGLE_LISTENER_LOCK_ENABLED` в `config.py`.

**Зачем:** убрать конфликт Telegram на двух компьютерах без большой архитектурной переделки. Это временный изолированный механизм, который потом можно быстро заменить или удалить.

**Тесты (`tests/test_telegram_lock.py`):**

- `test_acquire_creates_lock_sheet_and_writes_owner`;
- `test_active_other_owner_blocks_lock`;
- `test_stale_other_owner_can_be_replaced`;
- `test_release_clears_only_own_lock`.

### Release-архивы теперь включают локальный `version.json`

**Файл:** `.github/workflows/build-windows-release.yml`, шаг `Build onedir app`.

**Что было:**

В папочную Windows-сборку копировался `README.txt`, но `version.json` оставался только в репозитории и на GitHub raw URL.

**Что стало:**

Workflow создаёт локальный `version.json` рядом с `TakSklad.exe` в папке `TakSklad` перед упаковкой ZIP. Внутри фиксируются `app_version`, release tag, URL публичного update manifest и ссылка на release.

**Зачем:** готовые архивы становятся самодостаточнее: по папке приложения видно, какая версия установлена и откуда приложение проверяет обновления. Сам механизм автообновления по-прежнему читает публичный `version.json` с GitHub, поэтому будущие уведомления об обновлении приходят через интернет-адрес из `config.py`, а не через локальную копию файла.

**Почему не копируется публичный manifest один-в-один:** публичный `version.json` содержит SHA ZIP-архива. Если положить этот файл внутрь ZIP, SHA архива изменится, и поле `sha256_onedir` внутри станет устаревшим. Поэтому в архиве хранится локальный manifest без self-hash, а контрольные суммы остаются в публичном `version.json`.

**Тесты:** изменение упаковки; проверено обновлением текущего локального архива `TakSklad-ready-v1.1.16-with-data.zip` и наличием `TakSklad/version.json` внутри.

### Флаг “занято” больше не остаётся висеть после сбоя интерфейса

**Файл:** `main.py`, функции `set_busy`, `clear_busy`, `show_busy_error`, фоновые операции обновления/импорта/печати.

**Что было:**

Финализаторы некоторых операций сначала включали кнопки, а уже потом сбрасывали `operation_in_progress`. Если Tkinter уже пересоздал или закрыл кнопку, мог возникнуть UI-сбой до `clear_busy()`, и приложение продолжало отвечать `Дождитесь завершения текущей операции`, хотя рабочий поток уже завершился.

**Что стало:**

- начало и завершение операции пишутся в лог с длительностью;
- сообщение “занято” показывает, какая операция держит блокировку;
- критичные финализаторы сначала сбрасывают `operation_in_progress`, затем безопасно обновляют кнопки через `safe_config`;
- импорт из Telegram тоже заполняет данные текущей операции и очищает их через общий `clear_busy`.

**Зачем:** во время сканирования КИЗов оператор должен видеть реальную причину блокировки, а интерфейс не должен оставаться навсегда занятым после побочной ошибки UI.

**Тесты:** покрыто компиляцией `main.py`; поведение завязано на Tkinter callbacks.

### Сохранение общего JSON повторяется, если Windows кратко держит файл

**Файл:** `storage.py`, функция `save_app_data`.

**Что было:**

Сохранение всегда писало в один и тот же `TakSklad_data.json.tmp`, затем делало `os.replace`. Если второй процесс, антивирус или Windows на короткое время держали `.tmp` или основной JSON, появлялись ошибки `WinError 32` / `WinError 5`.

**Что стало:**

- временный файл теперь уникальный для каждой записи;
- при `PermissionError` замена повторяется до 8 раз с короткой паузой;
- недозаменённый временный файл удаляется в `finally`.

**Зачем:** два запущенных экземпляра или краткая блокировка файла больше не должны сразу ломать локальные очереди, настройки и общий файл данных.

**Тесты (`tests/test_storage_credentials.py`):**

- `test_save_app_data_retries_when_replace_is_temporarily_locked` — первый `os.replace` падает с `PermissionError`, второй успешно сохраняет данные.

### Ошибки Google Sheets стали понятнее для оператора

**Файл:** `sheets.py`, функция `format_google_sheets_error`.

**Что было:**

Ошибки Google могли уходить в интерфейс техническим текстом вроде `('invalid_grant: Invalid JWT Signature.', ...)`, а `PermissionError` после `403` иногда отображался пустой строкой.

**Что стало:**

- `403 / The caller does not have permission` показывается как проблема доступа service account к таблице;
- `invalid_grant / Invalid JWT Signature` показывается как старый или повреждённый Google-ключ;
- запись КИЗов в Google Sheets возвращает тот же понятный текст ошибки.

**Зачем:** оператору сразу видно, что надо заменить ключ или открыть таблицу сервисному аккаунту, а не ждать завершения несуществующей операции.

**Тесты (`tests/test_google_error_messages.py`):**

- `test_permission_error_gets_actionable_message`;
- `test_invalid_jwt_gets_actionable_message`.

### Обновление списка заказов больше не ждёт SkladBot

**Файл:** `main.py`, функции `fetch_sheet_data_with_sync`, `refresh_from_sheet`, `sync_skladbot_async`.

**Что было:**

Кнопка `ОБНОВИТЬ` и стартовая загрузка списка читали Google Sheets, затем сразу синхронно запускали SkladBot и только после этого отдавали управление интерфейсу. На втором компьютере это приводило к состоянию `Обновляю список заказов...`: список ещё пустой, кнопки заблокированы, хотя другой компьютер продолжает сканировать.

**Что стало:**

- быстрое обновление читает Google Sheets и очередь сохранений без ожидания SkladBot;
- список для КИЗов становится доступен сразу после чтения Google Sheets;
- SkladBot запускается отдельной фоновой задачей и не держит `operation_in_progress`;
- если фоновая SkladBot-синхронизация записала номера и оператор не находится внутри заказа, список перечитывается мягко.

**Зачем:** второй компьютер должен иметь возможность обновить список и начать работу, даже если SkladBot долго отвечает или временно недоступен.

**Тесты (`tests/test_refresh_fallback.py`):**

- `test_can_refresh_without_blocking_on_skladbot_sync` — быстрый refresh не вызывает SkladBot-синхронизацию и возвращает заказы из Google.

### Матчинг заявок SkladBot: дата выгрузки теперь обязательный критерий

**Файл:** `skladbot.py`, функция `request_matches_order_group`.

**Что было:**

```python
if parse_date_to_standard(group.get("date")) != parse_date_to_standard(request.get("unloading_date")):
    return False
```

Если обе даты приходили пустыми, `parse_date_to_standard` возвращал `""` для обеих сторон, `"" != ""` — это False, и проверка пропускала запись дальше. То есть дата по факту не была обязательной.

**Что стало:**

```python
group_date = parse_date_to_standard(group.get("date"))
request_date = parse_date_to_standard(request.get("unloading_date"))
if not group_date or not request_date or group_date != request_date:
    return False
```

Обе даты обязаны быть непустыми и строго равными после нормализации (`dd.mm.yyyy`). Если хотя бы одна пустая или не парсится — матчинг не делается.

**Зачем:** «Дата отгрузки» в листе `data` должна один-в-один совпадать с «Дата выгрузки» (`unloading_date`) в SkladBot. Без этого под одну строку могут схлопнуться заявки разных дней одного клиента.

**Тесты (`tests/test_skladbot_sync.py`):**

- `test_does_not_match_request_with_different_unloading_date` — заявка того же клиента за другой день не привязывается.
- `test_does_not_match_request_when_unloading_date_is_missing` — пустая дата в заявке SkladBot не даёт привязки.

### Матчинг заявок SkladBot: клиент сравнивается строго, без fuzzy токенов

**Файл:** `skladbot.py`, функция `request_matches_order_group`.

**Что было:**

```python
if not text_tokens_match(group.get("client"), request.get("recipient"), NOISE_COMPANY_TOKENS):
    return False
```

Нечёткое сравнение токенов с порогом 75% и вырезанием шумовых слов (`mchj`, `ooo`, `ип`, `мчж` и т.п.). Из-за этого:

- Похожие, но разные клиенты могли совпасть по токенам.
- При повторной синхронизации после второго импорта группа из одной строки могла «прицепиться» к заявке соседнего клиента, у которого совпали адрес и количество.

В рабочей выгрузке `TakSklad рабочая база.xlsx` (фильтр «Перечисление», 26.05.2026) это давало 14 номеров заявок, привязанных к 2 разным клиентам, и расхождение 40 шт по клиенту `"MARKET AL-KABIR" MChJ` между TakSklad и `Список_заказов_на_доставку_Чапамана_на_26_05_2026.xlsx`.

**Что стало:**

```python
group_client = normalize_lookup_text(group.get("client"))
request_recipient = normalize_lookup_text(request.get("recipient"))
if not group_client or not request_recipient or group_client != request_recipient:
    return False
```

Строгое равенство после `normalize_lookup_text` (приведение к нижнему регистру, `ё→е`, удаление `*` и `:`, схлопывание пробелов). Обе стороны обязаны быть непустыми.

**Зачем:** «Название компании/Имя человека» в SkladBot должно один-в-один совпадать с «Клиент» в листе `data`. Любое расхождение — это другая компания.

**Поведение в граничных случаях:**

- Если у одного клиента в SkladBot 2+ подходящих заявки (после нескольких импортов) — статус становится `Несколько совпадений` вместо случайной привязки.
- Если recipient в SkladBot пустой или отличается формулировкой, которую `normalize_lookup_text` не схлопывает (например лишняя пунктуация) — статус `Не найдено`. Видно в столбце «Статус SkladBot», правится вручную.

**Тесты (`tests/test_skladbot_sync.py`):**

- `test_does_not_match_request_from_different_client` — заявка от чужого клиента с теми же адресом, оплатой, датой и количеством не привязывается.

### Заведён журнал изменений

**Файл:** `docs/changelog.md` (этот файл).

Заведено правило: при любой правке в коде сюда добавляется запись с файлом, диффом сути, причиной и тестами.

## Что осталось за рамками этих правок

- `address_matches` всё ещё нечёткое (порог 55% токенов). При необходимости — сделать строгим аналогично клиенту и дате.
- `match_group_to_requests` требует, чтобы набор товаров заявки полностью совпадал с набором товаров группы. После второго импорта в группу попадает только новая строка — она не совпадёт с полной заявкой SkladBot и пометится `Не найдено`. Отдельный вопрос: разрешать ли частичное сопоставление или пересинхронизировать всю группу при дозагрузке.
- Сверку правок на живом SkladBot API я выполнить из своей среды не могу (внешний доступ к `api.skladbot.ru` заблокирован прокси). После прогона правки локально пришли свежий `TakSklad рабочая база.xlsx` — проверю, что номера встали корректно.

### Безопасный Git-снимок desktop-стабилизации

**Дата:** 2026-05-29.

**Что сделано:**

- Подготовлен Git-снимок текущей стабилизации без публикации нового автообновления.
- `version.json` закреплен на `1.1.7`, `mandatory: false`, ссылки на загрузку и SHA очищены.
- Документация очищена от конкретных значений Google service account, `private_key_id` и `SPREADSHEET_ID`.
- Зафиксировано правило: обычный push кода не должен менять публичный manifest обновления для рабочих компьютеров.

**Что не сделано:**

- Новый release-архив не собирался.
- Новый tag/release не публиковался.
- Push-уведомление об обновлении не готовилось.

**Проверки:**

- Python compile - успешно.
- Unit tests - 35 тестов пройдены.
- `version.json` - валидный JSON и закреплен на `1.1.7`.
- Старое имя проекта в рабочем дереве не найдено.
- Ручной Windows-smoke остается обязательным перед release-архивом.

### GitHub-репозиторий переименован в TakSklad

**Дата:** 2026-05-30.

**Что сделано:**

- Репозиторий GitHub переименован со старого исторического имени на `1fear/TakSklad`.
- Локальный `origin` переключен на `https://github.com/1fear/TakSklad.git`.
- Проверено, что новый репозиторий доступен, `main` на месте.
- Старый URL GitHub редиректит на новый репозиторий.

**Что не менялось:**

- `version.json` не повышался: рабочая линия остается `1.1.7`.
- Новый release/tag не создавался.
- Workflow-сборка Windows не запускалась.
- Push-уведомления об обновлении не готовились.

**Проверки:**

- Python compile - успешно.
- Unit tests - 35 тестов пройдены.
- `version.json` - валидный JSON и закреплен на `1.1.7`.
- `git diff --check` - успешно.

### Desktop-стабилизация без релиза

**Дата:** 2026-05-30.

**Файлы:** `src/taksklad/main.py`, `src/taksklad/sheets.py`, `src/taksklad/skladbot.py`, `src/taksklad/skladbot_sync.py`, `src/taksklad/app_skladbot.py`.

**Что сделано:**

- Google Sheets ошибки теперь переводятся в понятные сообщения: доступ к таблице, повреждённый ключ, quota/429, сеть/DNS/timeout/SSL.
- Ошибка обновления списка заказов больше не идёт через критическое окно приложения и Telegram-лог: оператор видит мягкое сообщение, а последний список остаётся доступным.
- Если обновление списка уже идёт, повторное нажатие показывает длительность операции и явно говорит, что можно работать с уже загруженным списком.
- Для долгого обновления добавлен UI-статус каждые 15 секунд, чтобы было видно, что приложение не зависло.
- SkladBot ошибки нормализованы: неверный API-токен, 429, timeout/network и некорректный JSON.
- SkladBot-синхронизация не падает наружу при ошибке чтения/записи Google Sheets; она возвращает результат с `errors` и не блокирует список заказов.

**Тесты:**

- Добавлены проверки Google-friendly messages.
- Добавлены проверки SkladBot-friendly messages.
- Добавлены проверки, что SkladBot read/write failure не выбрасывает исключение.
- Добавлена проверка fallback-сообщения обновления списка.
- Полный набор: 42 теста пройдены.

**Что не менялось:**

- `version.json` не повышался и остается на `1.1.7`.
- Релиз, тег, Windows-архив и push-уведомление не создавались.
