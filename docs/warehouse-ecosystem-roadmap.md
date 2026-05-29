# Roadmap Расширения TakSklad В Экосистему Склада

Документ описывает путь от текущего desktop-приложения к складской экосистеме: локальное сканирование остаётся стабильным, а тяжёлые фоновые процессы постепенно выносятся в серверную часть.

## Infrastructure Baseline

Базовая серверная инфраструктура уже развёрнута и готова принимать сервисы (подтверждено эксплуатантом на 29.05.2026). Это закрывает большую часть Этапа 1 (см. ниже).

Current baseline:

- VPS арендован и настроен; организована структура директорий под проекты.
- Безопасный SSH: только ключи, отказ от root, firewall-политики.
- Docker + Compose: изоляция сервисов, воспроизводимая среда деплоя.
- Traefik: reverse proxy с интеграцией в Docker, маршрутизация по доменам и HTTPS.
- PostgreSQL + Adminer для администрирования БД.
- Portainer для централизованного управления контейнерами.
- VS Code remote к VPS + интеграция Claude Code.
- GitHub для контроля версий и командной работы.

Roadmap implications:

- Docker/Compose — модель деплоя по умолчанию для всех новых сервисов.
- PostgreSQL — основная (primary) база данных проекта.
- Любой HTTP-сервис публикуется только через Traefik (домен + HTTPS); Adminer/Portainer/панель — за защитой, Postgres — не наружу.
- Документировать по каждому сервису env-переменные, volumes, процедуры backup/restore.
- До масштабирования добавить наблюдаемость, политику backup и шаги rollback деплоя.

Ещё не сделано (вне baseline): backup-том и политика backup/restore с проверкой восстановления, healthchecks, rollback деплоя, и сама миграция приложения TakSklad (backend API, воркеры) на эту инфраструктуру.

## Strategic Decision 2026-05-29

Так как проект в любом случае переходит на VDS, desktop-версия больше не является местом для крупных улучшений и глубокого рефакторинга.

Правило приоритета:

- desktop правим только для критичных складских блокеров;
- новые фоновые процессы проектируем как server worker, а не как очередной поток внутри `TakSklad.exe`;
- Telegram, SkladBot, отчёты, общие lock/state и интенсивная работа с Google Sheets должны постепенно уходить из desktop;
- подготовка backend API, PostgreSQL-схемы, Docker Compose и worker-процессов важнее косметической чистки desktop-кода;
- desktop должен остаться тонким клиентом для сканирования, печати, локального backup и offline fallback.

## 1. Цель Экосистемы

Цель - сделать систему, которая:

- поддерживает несколько складских компьютеров;
- не зависит от одного открытого desktop-приложения;
- работает с Telegram 24/7;
- хранит историю в нормальной базе;
- снижает нагрузку на Google Sheets;
- подтягивает SkladBot без торможения интерфейса;
- даёт руководителю прозрачные отчёты и статус склада;
- сохраняет возможность работать локально при временном сбое сети.

## 2. Главный Архитектурный Переход

Текущее состояние:

```text
Windows PC #1 -> TakSklad.exe -> Google Sheets
Windows PC #2 -> TakSklad.exe -> Google Sheets
TakSklad.exe -> Telegram getUpdates
TakSklad.exe -> SkladBot API
```

Проблема: каждый desktop одновременно является UI, worker, Telegram bot, SkladBot sync, Google client и локальным хранилищем.

Целевое состояние:

```text
Windows PC #1 -> Desktop client -> Backend API -> PostgreSQL
Windows PC #2 -> Desktop client -> Backend API -> PostgreSQL

Telegram Worker -> Backend API -> PostgreSQL
SkladBot Worker -> Backend API -> PostgreSQL
Report Worker   -> Backend API -> PostgreSQL

Backend API -> Google Sheets export/sync
Backend API -> SkladBot API
Backend API -> Telegram API
```

Desktop остаётся для сканирования и печати. Сервер берёт на себя фоновые задачи, координацию и историю.

## 3. Почему Нужны VPS, Docker И PostgreSQL

### VPS

Нужен как постоянная точка, которая работает даже когда складской компьютер закрыт.

На VPS можно держать:

- backend API;
- PostgreSQL;
- Telegram worker;
- SkladBot worker;
- report/backup worker;
- web admin panel;
- reverse proxy;
- мониторинг и backup.

### Docker / Docker Compose

Контейнеризация нужна для воспроизводимого запуска:

- каждый сервис изолирован;
- Postgres не конфликтует с backend;
- Telegram worker можно перезапустить отдельно;
- зависимости не смешиваются на сервере;
- новый сервер поднимается через `docker compose up -d`;
- проще обновлять сервисы;
- проще смотреть логи каждого сервиса.

Минимальная compose-схема:

```text
compose.yml
├─ traefik
├─ postgres
├─ backend-api
├─ telegram-worker
├─ skladbot-worker
├─ report-worker
├─ adminer
└─ backup-worker
```

### PostgreSQL

Postgres нужен как центральная operational database.

Туда нужно переносить:

- заказы;
- строки импортов;
- документы импорта;
- КИЗы;
- статусы сканирования;
- очереди;
- Telegram update state;
- SkladBot заявки и результаты матчинга;
- историю действий;
- ошибки;
- отчёты;
- audit log.

Google Sheets после этого должен стать внешним интерфейсом/отчётом, а не основной базой.

## 4. Целевая Серверная Инфраструктура

### 4.1. Traefik

Задачи:

- HTTPS;
- маршрутизация доменов;
- reverse proxy к backend/web/admin;
- автоматические сертификаты;
- ограничение доступа к admin-инструментам.

Важно: Adminer и Portainer нельзя оставлять публично без защиты. Минимум - Basic Auth + firewall/VPN/IP allowlist.

### 4.2. Backend API

Задачи:

- принять сканы от desktop;
- выдать список активных заказов;
- принять импорт;
- выдать статус документов;
- управлять очередями;
- отдавать отчёты;
- быть единственной точкой записи в Postgres;
- синхронизировать нужные данные с Google Sheets.

Начать можно с Python/FastAPI.

### 4.3. Telegram Worker

Задачи:

- единственный `getUpdates` или webhook;
- импорт Excel из Telegram;
- отправка отчётов;
- отправка логов;
- команды состояния смены;
- уведомления об ошибках;
- уведомления о завершении импорта.

После выноса в worker исчезают:

- `HTTP 409 Conflict`;
- Telegram-lock в Google Sheets;
- зависимость Telegram от открытого TakSklad.exe.

### 4.4. SkladBot Worker

Задачи:

- регулярно читать SkladBot;
- кэшировать заявки;
- матчить заявки с заказами;
- писать номера в Postgres;
- обновлять Google Sheets только если это ещё нужно;
- не мешать desktop UI.

Преимущество: worker может спокойно работать 1-2 минуты, потому что оператор не ждёт его в интерфейсе.

### 4.5. Report Worker

Задачи:

- дневные отчёты;
- отчёты по недосканированным позициям;
- отчёты по документам;
- SkladBot/TakSklad сверки;
- рассылка по расписанию.

### 4.6. Backup Worker

Задачи:

- backup PostgreSQL;
- backup важных файлов;
- выгрузка snapshot в защищённое место;
- retention policy.

## 5. Миграция По Этапам

### Этап 0. Стабилизировать Текущий Desktop

Цель: склад работает без остановок.

Задачи:

- SkladBot 14-дневное окно + `unloading_date`;
- partial-match SkladBot;
- диагностика `not_found`;
- Google 429 backoff;
- ротация логов;
- health-check двух ПК;
- стабильный релиз с безопасным auto-update.

Результат: текущая модель ещё на Google Sheets, но меньше блокировок и ошибок.

### Этап 1. Поднять VPS И Базовую Инфраструктуру

Задачи:

- VPS;
- отдельный non-root user;
- SSH keys only;
- firewall;
- Docker Engine;
- Docker Compose;
- Traefik;
- PostgreSQL;
- Adminer только за защитой;
- Portainer только за защитой;
- backup volume.

Результат: сервер готов принимать сервисы.

**Статус на 29.05.2026:** этот этап в основном выполнен — см. раздел «Infrastructure Baseline» в начале документа. Остаётся backup-том и политика backup/restore с проверкой восстановления; миграция самого приложения — задача следующих этапов.

### Этап 2. Создать Backend API MVP

Минимальные сущности:

- `orders`;
- `order_items`;
- `scan_codes`;
- `imports`;
- `import_files`;
- `pending_events`;
- `users`;
- `audit_log`.

Минимальные endpoints:

- `GET /health`;
- `GET /orders/active`;
- `POST /scans`;
- `POST /orders/{id}/complete`;
- `POST /imports`;
- `GET /imports`;
- `GET /reports/day`;

Desktop на этом этапе может всё ещё писать в Google Sheets, но backend уже принимает копию событий.

### Этап 3. Централизовать Сканирование

Задачи:

- desktop получает список заказов из backend;
- desktop отправляет сканы в backend;
- backend пишет в Postgres;
- Google Sheets обновляется фоново или только как отчёт;
- локальный `pending_saves` становится fallback к backend.

Результат: два ПК больше не конфликтуют через Google Sheets.

### Этап 4. Вынести Telegram

Задачи:

- отдельный `telegram-worker`;
- общий Telegram state в Postgres;
- импорт Excel через backend;
- отчёты через backend;
- уведомления из backend events.

Результат: Telegram работает 24/7, desktop не слушает Telegram.

### Этап 5. Вынести SkladBot

Задачи:

- отдельный `skladbot-worker`;
- хранить raw заявки в Postgres;
- хранить нормализованные заявки;
- хранить match result;
- обновлять номера без участия desktop;
- делать отчёт по `not_found` и `multiple`.

Результат: SkladBot не тормозит desktop и не расходует Google quota.

### Этап 6. Веб-Панель

Разделы:

- активные документы;
- прогресс сканирования;
- недосканированные позиции;
- история импортов;
- история сканов;
- дубли;
- ошибки;
- SkladBot match status;
- отчёты;
- настройки Telegram/SkladBot;
- пользователи и роли.

Дизайн: рабочий dashboard, компактный и плотный, без marketing landing page.

### Этап 7. Аналитика И WMS-Сверки

Задачи:

- сверка TakSklad vs SkladBot;
- остатки клиента сейчас;
- остатки по дням;
- движения клиента;
- финансы/счета;
- задержки по документам;
- скорость сканирования;
- частые проблемные товары/клиенты;
- SLA по смене.

## 6. Предлагаемая Схема Базы

Начальный набор таблиц:

```text
workspaces
users
devices
imports
import_files
orders
order_items
scan_codes
scan_backups
pending_events
print_jobs
telegram_chats
telegram_updates
skladbot_requests
skladbot_request_items
skladbot_matches
reports
audit_log
system_events
```

### orders

Хранит шапку заказа/группы:

- дата отгрузки;
- клиент;
- тип оплаты;
- адрес;
- торговый представитель;
- SkladBot number/id;
- статус;
- source import;
- timestamps.

### order_items

Позиции:

- товар;
- количество штук;
- план блоков;
- план КИЗов;
- статус;
- scanned_count.

### scan_codes

КИЗы:

- code;
- item_id;
- order_id;
- device_id;
- scanned_at;
- status;
- duplicate flag;
- source.

### imports

Документы:

- filename;
- sha256;
- uploaded_by;
- source: desktop/telegram;
- status;
- rows_total;
- rows_loaded;
- warnings.

### skladbot_requests

Кэш заявок:

- request_id;
- number;
- customer;
- type;
- created_at;
- unloading_date;
- recipient;
- address;
- comment;
- is_completed;
- archived;
- raw JSON.

### skladbot_matches

Результаты сопоставления:

- order_id;
- request_id;
- status: matched/not_found/multiple/error;
- reason;
- score/details;
- checked_at.

## 7. Desktop После Миграции

Desktop должен остаться простым и надёжным:

- показать список заказов;
- принять сканы;
- печатать;
- держать локальный backup;
- работать при кратком сбое сети;
- синхронизировать очередь с backend;
- показывать понятный статус связи.

То, что нужно убрать из desktop постепенно:

- Telegram polling;
- SkladBot API polling;
- тяжёлые Google Sheets reads;
- бизнес-логику shared locks;
- формирование сложной аналитики.

## 8. Безопасность

Минимальные правила:

- SSH только по ключам;
- root login запрещён;
- firewall открыт только на нужные порты;
- Postgres не наружу;
- Adminer/Portainer только через защищённый доступ;
- `.env` не в git;
- secrets через environment/secret manager;
- регулярные backup;
- отдельные роли БД;
- audit log действий.

Для desktop:

- не отправлять credentials через Telegram;
- не показывать токены в UI;
- не писать секреты в лог;
- проверять update SHA256;
- сохранять локальные очереди без приватных ключей.

## 9. Набор Контейнеров Для MVP

```yaml
services:
  traefik:
    purpose: HTTPS и маршрутизация
  postgres:
    purpose: центральная БД
  backend-api:
    purpose: API для desktop, Telegram и workers
  telegram-worker:
    purpose: Telegram bot 24/7
  skladbot-worker:
    purpose: фоновая синхронизация SkladBot
  report-worker:
    purpose: отчёты и расписания
  adminer:
    purpose: ручной просмотр БД
  backup-worker:
    purpose: регулярные backup
```

На первом этапе можно поднять только:

```text
postgres + backend-api + telegram-worker
```

SkladBot worker добавить вторым шагом.

## 10. Что Оставить В Google Sheets

Даже после Postgres Google Sheets может остаться полезным:

- как привычная таблица для просмотра;
- как export для менеджеров;
- как временный fallback;
- как простая интеграция с существующим процессом.

Но Google Sheets не должен быть:

- lock-сервисом;
- очередью событий;
- единственной базой истории;
- местом интенсивного polling.

## 11. Приоритеты На Ближайшие Релизы

### Release A: Desktop Stability

- SkladBot partial-match.
- SkladBot reasons log.
- Google 429 backoff.
- Log rotation.
- Health-check config on startup.
- Stable update manifest.

### Release B: Server Foundation

- VPS + Docker Compose.
- Postgres.
- Backend API skeleton.
- `/health`.
- Basic auth/service token.
- DB migrations.
- Backup script.

### Release C: Telegram Worker

- Telegram service in Docker.
- Shared update state in Postgres.
- Excel import through backend.
- Report sending through backend.
- Desktop Telegram polling can be disabled.

### Release D: Central Scans

- Desktop sends scans to backend.
- Backend writes Postgres.
- Local pending queue syncs to backend.
- Google export is background-only.

### Release E: SkladBot Worker

- Worker reads SkladBot.
- Worker caches requests.
- Worker matches orders.
- UI sees match status from backend.

### Release F: Web Panel

- Current orders.
- Progress.
- Import history.
- Reports.
- SkladBot match issues.
- Errors and queues.

## 12. Технические Решения По Умолчанию

Рекомендуемый стек:

- Backend: Python + FastAPI.
- DB: PostgreSQL.
- Migrations: Alembic.
- Workers: Python services in separate containers.
- Queue MVP: Postgres table + worker polling.
- Queue later: Redis/RQ/Celery only if Postgres polling станет узким местом.
- Reverse proxy: Traefik.
- Admin DB: Adminer, закрытый.
- Container UI: Portainer, закрытый.
- Logs: stdout контейнеров + file/DB audit for business events.
- Backups: `pg_dump` по расписанию + retention.

## 13. Риски Миграции

- Слишком быстрый отказ от Google Sheets может сломать привычный процесс.
- Backend добавит новую точку отказа.
- Без backup Postgres станет критичным single point of failure.
- Web panel может съесть время и не решить текущие боли.
- Если не сделать offline fallback, склад будет зависеть от интернета.

Митигировать:

- мигрировать поэтапно;
- desktop оставлять рабочим;
- сначала писать события в backend параллельно Google;
- включать server features флагами;
- тестировать на одном ПК до раскатки на два.

## 14. Definition Of Done Для Экосистемы

Систему можно считать перешедшей на экосистемную модель, когда:

- Telegram работает без открытого desktop;
- два ПК сканируют параллельно без конфликтов;
- сканы хранятся в Postgres;
- Google Sheets не является критичным для сканирования;
- SkladBot подтягивается worker'ом;
- есть история импортов и сканов;
- есть восстановление после сбоя сети;
- есть backup БД;
- есть понятный dashboard текущей смены;
- есть audit log.
