# Архитектура проекта TakSklad

Актуально на 31.05.2026. Версия приложения: `APP_VERSION = 2.0.0`. Документ не содержит секретов (токены, ключи, реальные chat_id, пароли VPS).

Соглашение: факты взяты из `docs/` и кода репозитория. Архитектурные выводы помечены **`(Inference)`**. Инфраструктурные факты, которые есть только в чек-листе пользователя, помечены **`(infra context)`**. Недостающее — в разделе 15.

---

## 1. Purpose

Основной архитектурный reference для команды и AI-ассистентов: единая картина текущего и целевого состояния и безопасный поэтапный путь между ними без остановки склада.

Как использовать: точка входа для нового разработчика; чек-лист перед расширением («что затронет, что нельзя сломать, какой это этап»); реестр архитектурных решений (раздел 12); карта рисков и открытых вопросов. Надстраивается над `project-knowledge-base.md`, `warehouse-ecosystem-roadmap.md`, `changelog.md`, `taksklad-full-functionality.md`, `skladbot-api-key-functionality.md`.

---

## 2. Project Overview

**Что делает.** TakSklad — desktop + backend система для складской обработки заказов и маркировки КИЗ: импорт Excel → нормализация → запись в backend/PostgreSQL → Google mirror/export → сканирование КИЗ → контроль дублей/недосканов → печать сводных листов → дневной отчёт → уведомления в Telegram. Репозиторий обновлений: `1fear/TakSklad`.

**Для кого.** Оператор склада (сканирование, печать, отчёт), руководитель/админ (отчёты в Telegram, в будущем веб-панель), разработчик/эксплуатант (credentials, релизы, два ПК, разбор логов).

**Проблема.** Ручная и ошибкоопасная работа с маркировкой в таблицах. Приложение централизует процесс, защищает от дублей КИЗ, потери сканов и сбоев сети/печати.

**Ключевые бизнес-сценарии.** Импорт (UI/Telegram) → группировка заказов → сканирование с локальным backup → запись в backend/PostgreSQL → server-side Google mirror/export → печать сводки → завершение дня и отчёт. Дополнительно: подтягивание номеров заявок SkladBot, автообновление через GitHub Releases.

**Характеристика.** Сейчас это переходная складская экосистема: desktop остаётся основным интерфейсом склада, backend/PostgreSQL становится hot-path source of truth, Google Sheets остаётся mirror/export и legacy fallback. Широкий backend-only rollout требует Windows-приёмки.

---

## 3. Current State

### Implemented Features

Импорт Excel (`.xlsx/.xlsm`, гибкий нормализатор шаблонов, алиасы колонок) из UI и Telegram; нормализация строк, дедупликация, геокодирование адреса по координатам; запись в backend/PostgreSQL с Google mirror/export; группировка заказов (`номер SkladBot + клиент + оплата + адрес`); подтягивание номеров SkladBot со статусами `Найдено/Не найдено/Несколько совпадений`; сканирование КИЗ с многоуровневой валидацией и контролем дублей, локальный backup до принятия кода; запись сканов в backend с server-side Google export; печать PNG-сводок (Windows PowerShell / `lp`); очереди `pending_backend_events/pending_saves/prints/telegram` и `scan_backups/`; дневной и подокументный Excel-отчёт, автоотправка (23:55); Telegram-бот (whitelist `chat_ids`, запрет отправки секретов); server-side Telegram worker как нормальный listener; автообновление с SHA256, подтверждением и cooldown; регрессионные тесты в `tests/`.

### Planned Features

Из roadmap: backend API (FastAPI) как единая точка записи; PostgreSQL как операционная БД; воркеры Telegram (24/7), SkladBot, Report, Backup; централизация сканов и очередей для нескольких ПК; веб-панель (статус смены, прогресс, недосканы, история, ошибки, роли); аналитика и WMS-сверки (TakSklad vs SkladBot, остатки сейчас/по дням, движения, SLA). Точечно: безопасный partial-match SkladBot, диагностика `not_found`, backoff на `429`, ротация логов, активация процессов `Архив`/`Возвраты`, поведение флага `requires_kiz`.

### Existing Modules

| Модуль | LOC | Роль |
|---|---:|---|
| `src/taksklad/main.py` | ~1172 | Tkinter UI + сканирование + выбор/сохранение позиций; god-модуль частично разобран |
| `src/taksklad/sheets.py` | — | Google Sheets: чтение заказов, запись импортов/КИЗ, Telegram lock/state, формат ошибок |
| `src/taksklad/skladbot.py` | — | API-клиент SkladBot: заявки, нормализация, критерии сравнения |
| `src/taksklad/excel_import.py` | — | Импорт: нормализация, дедуп, план блоков, геокод, запись |
| `src/taksklad/excel_normalizer.py` | — | Распознавание Excel-шаблона, алиасы, дата из контекста |
| `src/taksklad/skladbot_sync.py` | — | Матчинг заявок и запись номеров в `data` |
| `src/taksklad/storage.py` | — | `TakSklad_data.json`, миграция legacy JSON, retry при file lock |
| `src/taksklad/http_client.py` | — | Общий HTTPS-клиент с certifi context |
| `src/taksklad/update_service.py` | — | Автообновление, проверка манифеста, скачивание и Windows updater |
| `src/taksklad/printing.py` | — | Настройки печати, генерация PNG-сводки, отправка на принтер |
| `src/taksklad/pending_store.py` | — | Локальные очереди `pending_saves/prints` и `scan_backups` |
| `src/taksklad/reports.py` | — | Дневной отчет, отчеты по документам, сортировка групп, сводки |
| `src/taksklad/ui_widgets.py` | — | Переиспользуемые Tkinter-виджеты |
| `src/taksklad/telegram_service.py` | — | Telegram API, настройки, отправка сообщений/документов, очередь Telegram |
| `src/taksklad/app_telegram.py` | — | Telegram-действия UI: отчеты, меню, уведомления, polling, обработка сообщений |
| `src/taksklad/app_updates.py` | — | UI-обвязка автообновления и prompt установки |
| `src/taksklad/app_imports.py` | — | UI-обвязка ручного Excel-импорта |
| `src/taksklad/app_catalog.py` | — | UI-обвязка справочника товаров |
| `src/taksklad/app_control_panel.py` | — | UI контрольной панели и расчет дневной статистики |
| `src/taksklad/app_skladbot.py` | — | UI-оркестрация фонового SkladBot-синка |
| `src/taksklad/app_printing.py` | — | UI параметров печати и повторная печать очереди |
| `src/taksklad/app_day_end.py` | — | UI завершения дня и отображение текущей статистики |
| `src/taksklad/duplicate_codes.py` | — | Детализация и форматирование дублей КИЗ |
| `src/taksklad/utils.py` | — | Нормализация текста/дат/чисел, хэши, коды |
| `src/taksklad/config.py` | — | Константы: версия, ID/имена таблиц и колонок, настройки интеграций, UI |
| `src/taksklad/orders.py` | — | ID/ключи дублей, статусы, группировка |
| `src/taksklad/geocoding.py` | — | Яндекс Геокодер |
| `src/taksklad/catalog.py` | — | Справочник товаров, план блоков |

Плюс `tests/`, `.github/workflows/build-windows-release.yml`, `version.json`, `assets/`.

### Infrastructure

Серверная база развёрнута и готова принимать сервисы **(infra context — подтверждено пользователем, в `docs/` пока не зафиксировано)**:

- **VPS** арендован и настроен; структура директорий под проекты.
- **Безопасный SSH:** только ключи, отказ от root, firewall-политики.
- **Docker + Compose:** изоляция сервисов, воспроизводимая среда деплоя.
- **Traefik:** reverse proxy, интеграция с Docker, маршрутизация по доменам, HTTPS.
- **PostgreSQL** + **Adminer** (администрирование БД).
- **Portainer** — управление контейнерами.
- **GitHub** — контроль версий и командная работа.
- **VS Code remote** к VPS + интеграция **Claude Code**.

**Важный нюанс:** backend/воркеры уже существуют, но backend-only desktop rollout должен идти по staged/shadow процедуре. Legacy desktop без backend flags может работать напрямую с Google Sheets; это не целевой hot path.

### Known Constraints

Telegram работает только при открытом desktop (нет 24/7). Очереди локальны для ПК (нет общего состояния). `data` хранит меньше полей, чем Excel → дедуп ограничен. Флаг `requires_kiz` хранится, но не влияет на сканирование. Один операторский процесс на ПК. SkladBot API только на чтение (create/edit → `404/405`); при неоднозначности — `Несколько совпадений` без случайного выбора. Листы `Архив`/`Возвраты` зарезервированы, процессы не автоматизированы.

### Technical Debt

`main.py` — всё ещё крупный модуль (~1172 строк): основной UI, сканирование, выбор/сохранение позиций и завершение заказа смешаны **(Inference)**. Часть логики уже вынесена в `http_client`, `update_service`, `printing`, `pending_store`, `reports`, `ui_widgets`, `telegram_service`, `app_telegram`, `app_updates`, `app_imports`, `app_catalog`, `app_control_panel`, `app_skladbot`, `app_printing`, `app_day_end`, `duplicate_codes`, `startup_check`, `desktop_diagnostics`. Google Sheets перегружен ролями (БД + lock + Telegram-state). Ключ Яндекс Геокодера вынесен из `config.py`: приложение читает его из env `YANDEX_GEOCODER_API_KEY` или локального `yandex_geocoder_key.txt`, который не попадает в Git. Desktop-лог ротируется через `RotatingFileHandler`, startup self-check пишет безопасный fingerprint конфигурации, refresh diagnostic summary пишет счётчики загрузки заказов. Широкая эвристика retryable-ошибок. Нечёткий `address_matches` (~55%). SkladBot product matching допускает безопасный partial-match: все товары TakSklad-группы должны совпасть по SKU и блокам, лишние товары в SkladBot-заявке не блокируют номер, но неоднозначность остаётся `Несколько совпадений`. Манифест `version.json` временно на `onefile_exe` (переходное состояние).

### Risks

`429` Google quota (два ПК, локи, частые чтения); конфликты двух ПК при разных `SPREADSHEET_ID`/`credentials`; зависание busy-state; хрупкость автообновления на старых exe; отсутствие ротации старого ключа геокодера; при будущем backend — зависимость от сети без offline-fallback. Подробно — раздел 13.

---

## 4. Domain Model

| Сущность | Назначение | Ключевые атрибуты | Связи | Роль |
|---|---|---|---|---|
| **Заказ/Группа (Order)** | Единица складской работы | дата отгрузки, оплата, клиент, адрес, ТП, статус, ID заказа/импорта, поля SkladBot | содержит позиции; матч с заявкой; из импорта | В UI «заказ» = группа по `SkladBot+клиент+оплата+адрес`; не смешивает оплату/адрес |
| **Позиция (Order Item)** | Товар для сканирования | товар, кол-во ШТ, план блоков/КИЗ, статус, scanned | принадлежит заказу; ссылается на товар | План сканирования; «выполнена» при КИЗ ≥ плана |
| **КИЗ (Scan Code)** | Код маркировки | код (нач. `01`, длина 20–120, ASCII+GS), время, источник, флаг дубля | принадлежит позиции; пишется в `data` и `scan_backups` | Главный артефакт; не принят без локального backup |
| **Импорт (Import)** | Excel-источник строк | имя, SHA256, кто/источник, статус, строк всего/загружено, предупреждения | порождает заказы/позиции | Единица истории и прогресса (`файл | 20/30`) |
| **Товар справочника** | Норматив расчёта | название, штук в блоке (деф. 10), `requires_kiz` | используется позициями/отчётами | План блоков = ceil(штук/блок) |
| **Заявка SkladBot** | Внешняя WMS-заявка | request_id, номер, получатель, тип `3PL отгрузка`, created_at, unloading_date, адрес, товары, raw | сопоставляется с группой | Обогащает заказ номером; не блокирует скан |
| **Match SkladBot** | Результат сопоставления | статус `matched/not_found/multiple/error`, причина, время | заказ ↔ заявка | Пишется в поля SkladBot строки `data` |
| **Очередь/Backup** | Защита от потери | вид, ссылка на заказ/файл, причина, попытки | ссылается на заказ/позицию | `pending_saves/prints/telegram`, `scan_backups`; восстановимость |
| **User / Device** *(Inference)* | Аутентификация, аудит | — (в desktop пока нет) | — | Целевые: роли и трассировка «кто/что/когда»; сейчас whitelist Telegram частично |

---

## 5. Current Architecture

**Тип.** Монолитное desktop-приложение: один процесс на ПК совмещает UI, воркер, Telegram-бот, SkladBot-синк, Google-клиент и локальное хранилище. Координация ПК — через общую Google-таблицу.

**Слои (по ответственности; явного разделения в коде нет).** Представление и оркестрация — в `main.py`; бизнес-логика — `orders`, `excel_import`+`excel_normalizer`, `catalog`, `skladbot`+`skladbot_sync`; доступ к данным/интеграции — `sheets`, `geocoding`, HTTP в `skladbot`, `storage`; фундамент — `config`, `utils`. **(Inference)** Логика и I/O вызываются преимущественно из `main.py` — граница «UI ↔ логика» размыта.

**Зависимости (по импортам).**

```text
config ◄ всеми;  utils,storage ◄ config
catalog ◄ config,storage,utils;  orders,geocoding,excel_normalizer ◄ config,utils
sheets ◄ config,orders,storage,utils;  skladbot ◄ config,storage,utils
skladbot_sync ◄ config,orders,skladbot,utils
excel_import ◄ catalog,config,excel_normalizer,geocoding,orders,sheets,storage,utils
main ◄ почти все модули
```

`config`/`utils` — изолированный фундамент. `main.py` зависит почти от всего (кандидат на декомпозицию). `sheets`→`orders`: доступ к данным знает о бизнес-статусах — развязать **(Inference)**.

**Интеграции.** Backend/PostgreSQL — operational store; Google Sheets (`gspread`/`oauth2client`) — mirror/export и legacy fallback; Telegram Bot API через backend worker; SkladBot API (`urllib`, чтение/создание через worker); Яндекс Геокодер; GitHub Releases/`version.json` (TLS через `certifi`).

**Точки входа.** `TakSklad.exe` (`main.py`); Telegram-команды/файлы от whitelisted чатов; кнопки UI; GitHub Actions (сборка по тегу).

**Хранилища.** PostgreSQL (`orders`, `order_items`, `scan_codes`, `pending_events`, audit/import tables); Google Sheets (`data`, `Архив`, `Возвраты`, `_TakSklad_System`) как mirror/legacy; `TakSklad_data.json` (настройки и локальные очереди); legacy JSON (миграция); `scan_backups/*.jsonl`; `reports/`; `docs/*.log`.

**Ключевые потоки.** Импорт: Excel→normalizer→backend import→Postgres→Google mirror/export. Скан: группа→КИЗ→валидация/дубли→локальный backup→backend scan или `pending_backend_events`. SkladBot worker: список/детали→матчинг/создание→Postgres→Google mirror/export. Telegram: backend worker→`pending_events`→import/report/notification. Отчёт: Postgres/SkladBot→Excel→Telegram. Обновление: `version.json`→сравнение→(подтверждение)→ZIP/SHA256→PowerShell→перезапуск.

**Деплой сейчас.** Desktop распространяется как Windows-сборка через GitHub Releases (PyInstaller `--onedir`, переходный `onefile`). Backend и workers доставляются на VDS через Docker/Compose; production deploy требует restore point, selective deploy и readiness/operations checks.

---

## 6. Target Architecture

Цель — разнести совмещённые роли desktop на сервисы поверх **уже существующей** инфраструктуры (Docker/Traefik/Postgres), оставив desktop тонким клиентом сканирования/печати.

```text
ПК #1/#2 ─► Desktop client ─► Backend API (FastAPI, в Docker) ─► PostgreSQL
                                   ▲             │
        Telegram/SkladBot/Report/Backup workers  └─► Google Sheets (export/fallback)
        Traefik ─► HTTPS + маршрутизация (backend, web-panel, Adminer, Portainer)
```

**Слои.** Desktop (UI, локальный backup, offline-очередь, синк с backend); Backend API (единственный писатель в Postgres, выдаёт заказы, принимает сканы/импорты, отдаёт отчёты, синхронизирует Sheets); воркеры (Telegram 24/7, SkladBot, Report, Backup); хранилище (Postgres — источник истины, Sheets — производная витрина); инфраструктура (Docker Compose, Traefik, Adminer/Portainer за защитой).

**Границы и правила.** Бизнес-логика уходит из UI в backend/воркеры. Desktop ходит только в Backend API (не напрямую в Sheets/Telegram/SkladBot). **Единственный писатель** в Postgres — backend; идемпотентность (импорт по SHA256, скан по коду, Telegram по `update_id`); деградация — при недоступности backend desktop пишет в локальную очередь и синхронизирует позже. **(Inference из принципа roadmap.)**

**Изоляция логики (Inference).** Доступ к данным — через репозитории (`OrdersRepository`, `ScanRepository`); внешние API — за клиентами (`SkladbotClient`, `TelegramGateway`, `GeocoderClient`); чистые функции нормализации/матчинга остаются без I/O.

**PostgreSQL/данные.** Postgres — дефолтная основная БД и источник истины (заказы, позиции, КИЗ, импорты, очереди, Telegram-state, заявки/матчи SkladBot, отчёты, аудит). Миграции — Alembic. Google Sheets — экспорт/витрина/временный fallback, не lock и не очередь.

**API.** REST на FastAPI: `/health`, `/orders/active`, `/scans`, `/orders/{id}/complete`, `/imports`, `/reports/day`. Стабильный версионируемый контракт. Аутентификация: сервисный токен на старте, пользователи/роли позже **(Inference — модель в Open Questions)**.

**Интеграции.** Каждый внешний сервис — адаптер с таймаутами/backoff/кэшем; SkladBot — отдельный воркер с кэшем заявок в Postgres.

**Фоновая обработка.** Отдельные воркеры-контейнеры (не потоки в UI); расписания (SkladBot-синк, отчёты, backup) живут в воркерах.

**Docker-деплой.** Docker/Compose — модель деплоя по умолчанию: каждый сервис (backend, воркеры, web-panel) — отдельный контейнер в общем compose-стеке, воспроизводимая среда. Новые сервисы добавляются как сервисы compose с健康-проверками.

**Traefik/HTTPS — внешний веб-уровень.** Любой HTTP-сервис публикуется только через Traefik (домен + HTTPS); admin-инструменты (Adminer/Portainer) — только за защитой (Basic Auth/IP allowlist/VPN).

**Конфигурация и эксплуатация.** Секреты — через env/secret manager, `.env` вне git. Документировать: env-переменные каждого сервиса, volumes (данные Postgres, backup, локальные тома desktop-синка), процедуры backup/restore (`pg_dump` + retention + проверка восстановления) и rollback деплоя (откат образа/compose к предыдущему тегу). **(Inference, согласовано с infra baseline.)**

---

## 7. Module Boundaries

«Should Not Know About» — инвариант (часть целевая, **Inference**).

| Module | Responsibility | Owns Data | Depends On | Should Not Know About |
|---|---|---|---|---|
| `config.py` | Константы и настройки | Статическая конфигурация | — | UI, сеть, бизнес-правила |
| `utils.py` | Чистые нормализаторы, хэши | — | `config` | Sheets, Telegram, SkladBot, Tkinter |
| `storage.py` | Локальный JSON, миграция, retry | Локальные секции/очереди | `config` | UI, сетевые интеграции, матчинг |
| `catalog.py` | Справочник, план блоков | `product_catalog` | `config,storage,utils` | Структура Sheets, Telegram, SkladBot |
| `orders.py` | ID/дубли/статусы/группировка | — (логика) | `config,utils` | Хранилище (Sheets/БД), сеть, UI |
| `geocoding.py` | Адрес по координатам | Кэш геокода (импорт) | `config,utils` | Sheets, Telegram, SkladBot, UI |
| `excel_normalizer.py` | Распознавание шаблона | — | `config,utils` | Sheets, сеть, UI |
| `sheets.py` | Доступ к Google Sheets | Данные в Sheets | `config,orders,storage,utils` | UI, SkladBot, печать; *(Inference)* бизнес-статусы (развязать с `orders`) |
| `skladbot.py` | API-клиент, критерии сравнения | Кэш заявок (память) | `config,storage,utils` | Запись в Sheets, печать, UI |
| `skladbot_sync.py` | Матчинг и запись номеров | Матчи (в `data`) | `config,orders,skladbot,utils` | UI, Telegram, печать |
| `excel_import.py` | Импорт Excel | Строки, `import_history` | `catalog,config,excel_normalizer,geocoding,orders,sheets,storage,utils` | Виджеты, SkladBot, печать |
| `app_imports.py` | UI-обвязка ручного импорта | UI-состояние импорта | `catalog,config,excel_import,utils` | Google Sheets напрямую, Telegram polling |
| `app_catalog.py` | UI-обвязка справочника товаров | UI-состояние справочника | `catalog,config,ui_widgets,utils` | Сканирование, Telegram, Sheets |
| `app_control_panel.py` | UI контрольной панели и дневная статистика | UI-состояние контроля | `config,orders,pending_store,sheets,ui_widgets,utils` | Сканирование, импорт, Telegram |
| `app_skladbot.py` | UI-оркестрация фонового SkladBot-синка | UI-состояние SkladBot-синка | `config,skladbot_sync` | Алгоритм матчинга, Google Sheets fallback |
| `app_printing.py` | UI параметров печати и повторная печать очереди | UI-состояние печати | `config,pending_store,printing,ui_widgets,utils` | Завершение заказа, сканирование, Telegram |
| `app_day_end.py` | UI завершения дня и статистика | UI-состояние завершения дня | `config,orders,pending_store,reports,sheets,telegram_service` | Сканирование, SkladBot, импорт |
| `app_telegram.py` | Telegram UI/polling callbacks | UI-состояние Telegram | `config,excel_import,storage,telegram_service` | Сканирование, печать, SkladBot |
| `app_updates.py` | UI-обвязка обновлений | Update-state UI | `config,update_service` | Сканирование, импорт, Telegram |
| `main.py` | UI + оркестрация сканирования | UI-состояние, фон | почти все | — (цель *(Inference)*: сузить до UI + сервис-слой) |

---

## 8. Data Architecture

**Какие данные.** Заказы/позиции/КИЗ, импорты, справочник, заявки/матчи SkladBot, очереди, Telegram-state, состояния отчёта/обновления, настройки, credentials, логи/backup сканов.

**Источник истины.** Сейчас: Google Sheets `data` (заказы/сканы), `scan_backups` (страховка КИЗ), `TakSklad_data.json` (настройки/очереди). Целевое: **PostgreSQL — основной источник истины**; Sheets — производная витрина; `scan_backups` остаётся локальной страховкой desktop.

**Ownership данных (целевое).** Заказы/позиции/сканы → backend (`orders/order_items/scan_codes`); импорты → `imports/import_files`; очереди → `pending_events/print_jobs` (+ локальный буфер desktop); SkladBot → `skladbot_requests/_items/_matches` (воркер); Telegram-state → `telegram_chats/updates`; аудит → `audit_log/system_events`. Стартовая схема — из roadmap §6.

**Кэшировать можно:** справочники SkladBot, активный список заказов (короткоживущий), геокод (в импорте), активные заявки. **Нельзя как источник истины:** принятые КИЗ и статусы записи (фиксировать backup→БД).

**Нельзя смешивать:** операционные данные и координацию (lock/Telegram-state — отдельно от заказов); сырые и нормализованные заявки SkladBot; заказы разных дат одного клиента (дата — часть ключа); секреты и бизнес-данные; данные разных конфигураций ПК.

**Миграции.** Главная: Sheets → Postgres с dual-write (Alembic). Расширение ключа дубля в `data` при добавлении ИНН/координат/типа лида. Вынос lock/state из `_TakSklad_System` после Telegram-воркера. Партиционирование/ротация для `scan_codes`/`audit_log`/логов **(Inference)**.

**Backup/restore.** `pg_dump` по расписанию (backup-воркер) + retention + регулярная проверка восстановления; том данных Postgres и том бэкапов — отдельные volumes; бэкап не должен содержать незашифрованные секреты. **(Inference, согласовано с infra baseline.)**

**Open (данные).** Глубина миграции и долгосрочная роль Sheets; ретеншен/PII; одна организация vs `workspaces` (см. раздел 15).

---

## 9. Integration Architecture

| Integration | Purpose | Data Direction | Criticality | Failure Risks | Isolation Recommendation |
|---|---|---|---|---|---|
| **Google Sheets** | Заказы/сканы/статусы; сейчас ещё lock/state | Чтение+запись | Критична сейчас | `429`, `403`, `invalid_grant`, таймауты | Backoff/cache, вынести lock/state в Postgres, понизить до экспорта на backend |
| **Telegram Bot API** | Отчёты, лог, импорт, уведомления | Polling + отправка | Средняя (не блокирует склад) | `409` на двух ПК, гонки lock | Сейчас lock+общий `update_id`; цель — единственный Telegram-воркер |
| **SkladBot API** | Номера заявок; далее остатки/сверки | Только чтение | Низкая для скана, средняя для обогащения | `429`, форматы дат, неполные данные, нет write API | Лимиты/паузы/кэш, окно 14 дн.+`unloading_date`, отдельный воркер; write не предполагать |
| **Яндекс Геокодер** | Адрес по координатам | Только чтение | Низкая (есть fallback) | Лимиты, локальная настройка ключа | Кэш в импорте; env/local secret file; `GeocoderClient` |
| **GitHub Releases/`version.json`** | Автообновление desktop | Чтение+скачивание | Средняя (риск циклов) | SSL, занятый файл, неверный манифест | SHA256, подтверждение, cooldown, не запускать старый exe; вернуть `onedir_zip` |
| **Backend API** *(planned)* | Desktop/воркеры ↔ Postgres | Двунаправленно | Будет критичной | Точка отказа | Версионируемый контракт, сервисный токен, offline-fallback desktop |
| **PostgreSQL** *(planned как primary)* | Источник истины | Чтение+запись | Будет критичной | Потеря данных без backup | Только через backend; backup/restore; роли БД |

---

## 10. Extension Strategy

**Первым (низкий риск):** доработки SkladBot (`skladbot.py`/`skladbot_sync.py`), backoff Google (`sheets.py`), ротация логов, health-check на старте. **Стабилизировать до масштабирования:** `main.py` (декомпозиция), роль Sheets как lock/очереди, манифест обновления (вернуть `onedir_zip`).

**Абстракции (Inference):** репозитории данных, клиенты интеграций, абстракция очереди (`PendingQueue`: локальная/backend), сервис-слой (`ImportService/ScanService/ReportService`) — общий для UI и backend.

**Не принимать преждевременно:** полный отказ от Sheets до надёжного backend; брокер очередей до того, как Postgres-polling станет узким местом; веб-панель раньше backend; жёсткие роли/мультитенантность без подтверждения.

**Backward-compatible:** локальные файлы/очереди не сбрасываются при обновлении; `data` мигрирует без потери рабочей части; новые поля контракта — необязательные; импорт единый для UI/Telegram.

**Разделение логики/инфраструктуры/интеграций:** бизнес-логика не знает о транспорте и хранилище; интеграции — за адаптерами; инфраструктура (Docker/Traefik/Postgres) — вне кода приложения, через конфиг/compose.

**Добавление сервиса в Docker/Traefik:** новый сервис — контейнер в compose с healthcheck, env через `.env`/secret, публикация наружу только через Traefik (домен+HTTPS), внутренние сервисы (Postgres) — не наружу; запись в БД — через backend, не напрямую.

---

## 11. Phased Implementation Plan

Учтено, что серверная инфраструктура уже развёрнута — этап «поднять VPS/Docker/Traefik/Postgres» из roadmap в основном закрыт, поэтому усилия смещены к backend и миграции.

### Phase 1: Stabilize Existing Foundation

- **Goal:** убрать текущие боли без архитектурной переделки; склад работает без остановок; зафиксировать инфраструктурные assumptions.
- **Tasks:** partial-match SkladBot + диагностика `not_found`; backoff/cache Google против `429`; расширить health-check на старте реальной проверкой доступа к таблице и lock owner; стабильный билд 1.1.18+ и возврат манифеста на `onedir_zip`/`mandatory:true`; ротировать ключ Яндекс Геокодера после вынесения из `config.py`; синхронизировать `taksklad-full-functionality.md` с changelog.
- **Affected Areas:** `skladbot.py`, `skladbot_sync.py`, `sheets.py`, `config.py`, `main.py`, `geocoding.py`, `version.json`, `tests/`, `docs/`.
- **Result:** меньше блокировок/`429`/`не найдено`; безопасное обновление; нет утечки ключа.
- **Done Criteria:** в логе виден итог матчинга и причины `not_found`; `429` обрабатывается backoff без потери lock; логи ротируются; релиз на обоих ПК; ключ не в git; тесты зелёные.

### Phase 2: Clarify Module Boundaries *(Inference)*

- **Goal:** подготовить код к backend без смены поведения; разорвать сцепку «UI↔логика».
- **Tasks:** вынести логику из `main.py` в сервисы; ввести репозитории и клиентов интеграций за интерфейсами; развязать `sheets.py` от `orders.py`; расширить unit-тесты на сервисы.
- **Affected Areas:** новый `services/`/`repositories/`, `main.py` (тонкий UI), `sheets.py`, `skladbot.py`, `geocoding.py`, `tests/`.
- **Result:** UI вызывает сервисы; логика тестируется без UI; хранилище подменяемо.
- **Done Criteria:** `main.py` заметно уменьшен; ключевые сценарии покрыты тестами сервисов; поведение не изменилось.

### Phase 3: Introduce Extension Points (Backend on existing infra)

- **Goal:** построить backend как контейнер в существующем Docker/Traefik-стеке и начать dual-write в Postgres параллельно Sheets.
- **Status 2026-05-30:** начат backend MVP-каркас: FastAPI shell, `/health`, SQLAlchemy-модели, init SQL-схема, Dockerfile, VDS compose, `.env.example`, тесты структуры. Реализованы первые endpoint'ы Postgres-логики: активные заказы, скан КИЗ и завершение заказа. Импорты, отчеты, SkladBot worker и desktop-подключение ещё не production-ready.
- **Tasks:** реализовать реальные endpoint'ы `GET /orders/active`, `POST /scans`, `POST /orders/{id}/complete`, `POST /imports`, `GET /reports/day`; добавить миграции Alembic или утверждённую миграционную процедуру; поднять compose на VDS через Traefik; desktop за репозиторием шлёт копию событий в backend, продолжая писать в Sheets; feature-флаги.
- **Affected Areas:** новый `backend-api` (compose-сервис), `repositories/` (реализация backend) на desktop, `config.py` (адрес backend/флаги), `compose.yml`/Traefik-метки.
- **Result:** сервер принимает события; данные копятся в Postgres без отказа от Sheets.
- **Done Criteria:** `backend-api` поднят в compose за Traefik, `/health` по HTTPS отвечает; события сканов/импортов видны в Postgres; включение/выключение флагами без поломки desktop.

### Phase 4: Scale Ecosystem Features

- **Goal:** перенести фоновые роли с desktop на воркеры и централизовать данные.
- **Tasks:** Telegram-воркер (единственный poll/webhook, state в Postgres) → исчезают `409`/lock в Sheets; централизация сканов (desktop берёт список и шлёт сканы в backend; Sheets — фоновый экспорт; `pending_saves` — fallback к backend); SkladBot-воркер (raw+нормализованные заявки и матчи в Postgres); веб-панель за Traefik (статус смены, прогресс, недосканы, история, ошибки, роли).
- **Affected Areas:** `telegram-worker`, `skladbot-worker`, `report-worker`, web-panel; desktop (отключение polling/тяжёлых чтений); backend (эндпоинты).
- **Result:** Telegram 24/7 без desktop; два ПК без конфликтов; SkladBot не тормозит UI и не ест Google quota.
- **Done Criteria:** Telegram работает с закрытым desktop; параллельная работа ПК без дублей; сканы в Postgres; матчинг идёт воркером; в панели виден статус смены.

### Phase 5: Hardening, Observability and Maintenance

- **Goal:** сделать экосистему эксплуатируемой и устойчивой.
- **Tasks:** backup-воркер (`pg_dump`+retention+проверка восстановления); наблюдаемость (логи контейнеров + business-audit в БД, healthchecks, алерты по ошибкам/очередям); rollback деплоя (откат образа/compose к предыдущему тегу); безопасность (Adminer/Portainer за защитой, Postgres не наружу, роли БД, `.env` вне git); offline-fallback desktop; аналитика/WMS-сверки; документация деплоя и эксплуатационные процедуры.
- **Affected Areas:** `backup-worker`, `report-worker` (аналитика), backend (audit/metrics), инфраструктура (Traefik/секреты/compose), desktop (offline), `docs/`.
- **Result:** есть бэкапы и восстановление, аудит, мониторинг, rollback, сверки; склад не зависит критично от сети.
- **Done Criteria (DoD экосистемы):** Telegram без desktop; два ПК без конфликтов; сканы в Postgres; Sheets не критичен для скана; SkladBot — воркером; есть история, восстановление после сбоя сети, backup БД с проверкой restore, dashboard смены, audit log, документированный rollback.

---

## 12. Architectural Decisions (ADR-lite)

### ADR-001: Сканирование не зависит от внешних интеграций
- **Status:** Accepted
- **Context:** SkladBot/Telegram/Google падают; склад не должен стоять.
- **Decision:** КИЗ принимается после локального backup; список активных заказов виден без SkladBot и при его недоступности.
- **Consequences:** высокая отказоустойчивость; данные временно расходятся и синхронизируются позже.
- **Related Docs:** knowledge-base §5/§16, full-functionality §31–32.

### ADR-002: Строгий матчинг SkladBot без fuzzy и случайного выбора
- **Status:** Accepted
- **Context:** нечёткие токены привязывали номера к соседним клиентам.
- **Decision:** клиент строго через `normalize_company_name`; дата обязана быть непустой и равной `unloading_date`; при множестве — `Несколько совпадений`.
- **Consequences:** меньше ложных привязок и меньше ложных `Не найдено`; безопасный partial-match допускает лишние товары в SkladBot-заявке, но при нескольких кандидатах сохраняет `Несколько совпадений`.
- **Related Docs:** changelog 26.05/28.05, knowledge-base §7.

### ADR-003: Динамическое окно SkladBot для VDS worker и отдельный desktop fallback
- **Status:** Accepted
- **Context:** для VDS нужен быстрый worker, который не перебирает сотни старых заявок; для старой desktop fallback-линии нужно не потерять заявки, созданные заранее.
- **Decision:** VDS worker использует базовое окно `SKLADBOT_SYNC_LOOKBACK_DAYS=1`, но расширяет его по датам активных заказов без номера SkladBot до `SKLADBOT_SYNC_MAX_LOOKBACK_DAYS=7` с запасом `SKLADBOT_ORDER_CREATE_LEAD_DAYS=3`; детальная загрузка ограничена `SKLADBOT_DETAIL_LIMIT=3`, detail-запросы идут с паузой `SKLADBOT_REQUEST_DELAY_SECONDS=20`, `customer_id=6211`, `type_id=3389`; desktop fallback оставлен шире. Точное совпадение всегда проверяется по `unloading_date`.
- **Consequences:** серверный MVP 2.0 не обрывает рабочий день на первых 30 заявках, но всё ещё имеет явный лимит запросов и не возвращается к бесконечному перебору.
- **Related Docs:** changelog 28.05, knowledge-base §7, `config.py`, `deploy/vds/docker-compose.yml`.

### ADR-004: Координация двух ПК через Google Sheets (временно)
- **Status:** Accepted (временное)
- **Context:** два ПК с одним токеном → `409`; гонки → двойная обработка.
- **Decision:** lock `telegram_poll` + общий `last_update_id` в `_TakSklad_System` (без отката большего значения меньшим).
- **Consequences:** убирает `409`/дубли без переделки; расходует quota; заменяется Telegram-воркером.
- **Related Docs:** knowledge-base §9, changelog 26.05.

### ADR-005: Безопасное автообновление
- **Status:** Accepted
- **Context:** тихое обновление + restart-on-failure → бесконечный цикл.
- **Decision:** подтверждение, cooldown 1 час, во всех трёх местах не запускать старый exe при ошибке, лог в `docs/`.
- **Consequences:** цикл разорван; при ошибке приложение не стартует само.
- **Related Docs:** changelog 26.05/28.05, full-functionality §22.

### ADR-006: PostgreSQL — основная БД, Google Sheets → экспорт
- **Status:** Proposed
- **Context:** Sheets перегружен (БД+lock+очередь), упирается в quota.
- **Decision:** операционные данные → Postgres (уже развёрнут в baseline); Sheets — витрина/экспорт/fallback.
- **Consequences:** снимает quota/конфликты; требует поэтапной миграции с dual-write.
- **Related Docs:** roadmap §3/§6/§10; infra baseline.

### ADR-007: Backend API (FastAPI) — единственный писатель; desktop тонкий
- **Status:** Proposed
- **Context:** desktop совмещает несовместимые роли; нужен 24/7 и координация.
- **Decision:** Backend API — единственная точка записи в Postgres; desktop через API + локальный backup/очередь.
- **Consequences:** убирает гонки; новая точка отказа (нужны backup и offline-fallback).
- **Related Docs:** roadmap §2/§4.2/§7.

### ADR-008: Docker/Compose — модель деплоя по умолчанию; Traefik — внешний веб-уровень
- **Status:** Accepted (инфраструктура развёрнута; приложение ещё не мигрировано)
- **Context:** VPS с Docker/Compose, Traefik (домены+HTTPS), Adminer, Portainer уже настроены (infra context).
- **Decision:** каждый новый сервис — контейнер в compose; публикация наружу только через Traefik; admin-инструменты и Postgres — не публично.
- **Consequences:** воспроизводимый деплой и изоляция; больше операционной сложности (нужны healthchecks/rollback).
- **Related Docs:** infra baseline; roadmap §4/§8/§9.

### ADR-009: Очередь MVP = таблица Postgres + polling воркером
- **Status:** Proposed
- **Context:** нужна централизованная очередь без преждевременной сложности.
- **Decision:** очередь как таблица в Postgres с polling; брокер (Redis/Celery) — только при узком месте.
- **Consequences:** простой старт; возможный рефактор позже.
- **Related Docs:** roadmap §12.

### ADR-010: Изоляция логики за сервисами/репозиториями *(Inference)*
- **Status:** Proposed
- **Context:** `main.py` был god-модулем; первый слой уже вынесен, но файл всё ещё смешивает основной UI, сканирование и оркестрацию фоновых операций.
- **Decision:** сервис-слой и репозитории/клиенты за интерфейсами; одна логика для UI и backend.
- **Consequences:** тестируемость, подменяемое хранилище; аккуратный рефактор (Phase 2).
- **Related Docs:** §6/§10; вывод из кода.

### ADR-011: Вынести секреты из репозитория (ключ Яндекс Геокодера) *(Inference)*
- **Status:** Accepted
- **Context:** ключ Яндекс Геокодера не должен жить в исходном коде или релизном архиве как константа.
- **Decision:** читать ключ из env `YANDEX_GEOCODER_API_KEY`, затем из локального `yandex_geocoder_key.txt`; зашитого fallback-значения нет.
- **Consequences:** новый релиз требует настроенный env или локальный файл рядом с приложением; старый ключ нужно ротировать отдельно.
- **Related Docs:** `config.py`; knowledge-base §16, full-functionality §26.

### ADR-012: Бизнес-ключи Chapman для импорта, SkladBot и логистики
- **Status:** Accepted
- **Context:** Smartup/Excel, SkladBot и логистика используют разные формы одних данных: дата задаётся менеджером, Excel может быть в пачках/штуках, SkladBot работает в блоках, товарные названия отличаются, адрес может быть неполным.
- **Decision:** дата отгрузки задаётся менеджером в Telegram; SkladBot-сравнение количества выполняется только в блоках; товар сравнивается по нормализованному цвету `brown`/`red`/`gold` и формату `OP`/`SSL`; адрес является мягким признаком; логистический отчёт обязан содержать координаты, а не только адрес.
- **Consequences:** меньше ложных несовпадений и меньше риска неверной заявки SkladBot; требуется строгая нормализация блоков, товара и координат перед отчётами.
- **Related Docs:** knowledge-base §0.1, full-functionality §6.1, user-business-process-guide §1.1.

---

## 13. Risks and Mitigations

| Risk | Impact | Probability | Mitigation |
|---|---|---|---|
| `429` Google quota | Высокий (лок не освобождается, срыв синков) | Высокая | Backoff/cache, реже lock, одна копия; стратегически Postgres |
| Конфликт двух ПК (разные `SPREADSHEET_ID`/credentials) | Высокий (запись в чужую таблицу, дубли) | Средняя | Единая конфигурация, health-check, общий `update_id`; backend |
| Утечка ключа геокодера | Высокий | Средняя | Ключ только в env/локальном файле; ротировать старый ключ (ADR-011) |
| Цикл автообновления (старый exe) | Высокий | Низкая после фиксов | Подтверждение, cooldown, без рестарта старого; стабильный билд |
| SkladBot не находит номера | Средний (ручная правка) | Средняя | Окно 14 дн., строгий клиент, блоки, нормализованный SKU, адрес как мягкий признак, диагностика, partial-match |
| Логистический отчёт без координат | Высокий (маршрут строится неверно) | Средняя | Координаты - обязательное поле логистического отчёта; адрес только справочно |
| Неясные границы модулей / рост `main.py` | Средний (тормозит развитие/тесты) | Высокая | Phase 2: сервисы/репозитории, тесты |
| Смешивание доменной логики и инфраструктуры | Средний (хрупкость при миграции) | Средняя | Логика без транспорта/хранилища; адаптеры; конфиг вне кода |
| Преждевременный отказ от Sheets | Высокий (сломан процесс) | Средняя | Поэтапно, dual-write, Sheets как fallback |
| Backend — новая точка отказа | Высокий | Средняя | Offline-fallback, локальные очереди, мониторинг, откат на Sheets |
| Нет backup/restore Postgres | Критический (потеря истории) | Средняя (если забыть) | backup-воркер + retention + проверка restore (Phase 5) |
| Нет rollback деплоя | Высокий (нельзя откатить плохой релиз) | Средняя | Теги образов, откат compose к предыдущему тегу |
| Слабая наблюдаемость | Средний (медленная диагностика) | Высокая | Логи контейнеров + audit в БД, healthchecks, алерты |
| Поломка существующих сценариев при расширении | Высокий | Средняя | Backward-compatible контракт, feature-флаги, тесты, обкатка на 1 ПК |

---

## 14. Engineering Rules

**Новые модули:** соблюдать направление зависимостей (`config`/`utils` — фундамент, логика не зависит от UI); новый I/O — за интерфейсом, не прямым вызовом из логики/UI; чистые функции — без I/O.

**Бизнес-логика:** изменения матчинга/дедупа/статусов сопровождать unit-тестами; не возвращать fuzzy-match и случайный выбор (ADR-002); не блокировать скан ожиданием внешних систем (ADR-001).

**PostgreSQL/миграции:** запись только через backend; схема — через Alembic-миграции (одна миграция = одно логическое изменение, обратимо где возможно); не смешивать операционные данные и координацию; перед деплоем миграции — backup БД.

**Новые Docker-сервисы:** добавлять как сервис compose с healthcheck; env через `.env`/secret (не в git); внутренние сервисы (Postgres) не публиковать наружу; ресурсы/логи — на stdout контейнера.

**Подключение к Traefik:** публиковать HTTP-сервис только через Traefik (домен+HTTPS, метки маршрутизации); admin-инструменты — за Basic Auth/IP allowlist; проверять, что сертификат и маршрут поднялись.

**Документация:** при правке кода — запись в `changelog.md` (файл, суть, причина, тесты); архитектурные изменения отражать здесь (§5–8, §11–13) и в knowledge-base; новое решение — как ADR перед реализацией.

**Changelog:** записи от новых к старым; затронутые файлы и тесты; для временных решений — условие отката.

**Перед merge:** прогон `tests/` (затронутые регрессии зелёные); compile-check изменённых модулей; нет секретов в git; запись в changelog; при инфраструктурных изменениях — обновлены env/compose/Traefik-доки; Windows-специфику (печать/обновление) помечать «полная регрессия — на Windows после сборки».

**Не ломать сценарии:** инварианты — локальный backup до КИЗ; локальные файлы/очереди не сбрасываются при обновлении; импорт пишет только в `data`; служебные колонки с `AA`; контракт backward-compatible; обкатка на одном ПК до двух.

---

## 15. Open Questions

| Question | Why It Matters | Owner | Blocks |
|---|---|---|---|
| Есть ли у SkladBot write API (создание/редактирование заявок)? | Публичный API только на чтение (404/405) | Поставщик SkladBot | Управление WMS из TakSklad |
| Partial-match: частичное совпадение товаров или ресинк всей группы? | После 2-го импорта группа не матчится с полной заявкой | Продукт-овнер + dev | Надёжное подтягивание номеров |
| Делать `address_matches` строгим (как клиента/дату)? | Нечёткий ~55% → ложные совпадения | Dev + эксплуатация | Точность матчинга |
| Модель аутентификации backend (сервисный токен vs пользователи/роли)? | Определяет дизайн API/панели | Владелец + dev | Контракт API, веб-панель, аудит |
| Одна организация или мультитенантность (`workspaces`)? | В схеме roadmap есть `workspaces` | Владелец | Схема БД и модель доступа |
| Домены/поддомены и доступ к admin-инструментам за Traefik? | Adminer/Portainer/панель нельзя публично | Владелец + эксплуатация | Безопасная публикация сервисов |
| Политика backup/retention и проверка restore (RPO/RTO)? | Без неё Postgres — SPOF | Владелец + эксплуатация | Phase 5, надёжность данных |
| `requires_kiz` должен отключать обязательный скан? | Флаг хранится, но не влияет | Бизнес + dev | Процесс для товаров без КИЗ |
| Долгосрочная роль Google Sheets (только отчёт или fallback)? | Определяет глубину миграции | Владелец + dev | Стратегия миграции |
| Доступен ли живой SkladBot API из среды разработки? | `api.skladbot.ru` был заблокирован прокси | Эксплуатация / dev | Проверка матчинга на реальных данных |

---

## 16. Next Steps

1. Реализовать partial-match SkladBot и диагностику `not_found` по полям.
2. Добавить backoff/cache Google против `429`; снизить частоту lock и обращений к `_TakSklad_System`.
3. Расширить diagnostic summary дневным агрегатом ошибок; refresh summary уже включён.
4. Расширить startup self-check реальной проверкой доступа к таблице и lock owner.
5. Ротировать старый ключ Яндекс Геокодера и проверить настройку env/локального файла на Windows (ADR-011).
6. Собрать стабильный билд 1.1.18+ и вернуть манифест на `onedir_zip`/`mandatory:true`.
7. Зафиксировать инфраструктурный baseline в `docs/` (сделано в roadmap) и описать env/volumes/backup/restore/rollback по сервисам.
8. Принять решения по разделу 15 (минимум: аутентификация backend, домены/доступ за Traefik, backup-политика) — это разблокирует Phase 3.

Далее — продолжать Phase 3: поднять backend compose на VDS, проверить `/health` через Traefik и реализовать первый endpoint записи событий в Postgres.

---

## Источники

`docs/`: `project-knowledge-base.md`, `warehouse-ecosystem-roadmap.md`, `changelog.md`, `taksklad-full-functionality.md`, `skladbot-api-key-functionality.md`, `project-overview.md`, `roadmap.md`. Код: `config.py`, `main.py`, `sheets.py`, `skladbot.py`, `skladbot_sync.py`, `excel_import.py`, `excel_normalizer.py`, `orders.py`, `catalog.py`, `storage.py`, `geocoding.py`, `utils.py`; `backend/`, `deploy/vds/`, `version.json`, `requirements.txt`, `tests/`. Инфраструктурный чек-лист — предоставлен пользователем (infra context).
