# TakSklad Docs Index

Этот индекс отделяет актуальную документацию TakSklad от исторических планов и служебных журналов.

## Статусы

- `ACTIVE` - можно использовать как текущую документацию после проверки даты и кода.
- `UPDATE` - полезно, но документ старее текущей архитектуры, сначала сверить с `taksklad-system-stack-overview.md`.
- `HISTORY` - evidence, аудит или старый план; не использовать как текущий статус.
- `SENSITIVE_HISTORY` - historical/reference с возможными чувствительными деталями; не цитировать наружу и не отправлять агентам целиком.
- `WORKING` - документ сопровождает текущую незавершенную ветку/рефакторинг, сверять с `git status` и кодом.

## Читать Сначала

1. `../README.md` - быстрый вход в продукт.
2. `taksklad-system-stack-overview.md` - актуальная суть приложения, архитектура и стек.
3. `report-source-rules.md` - правила источников отчетов и DB-first логика.
4. `implementation-log.md` - подробная история работ агентов, deploy/evidence и причин изменений.
5. `changelog.md` - журнал пользовательских и релизных изменений.
6. `local-development-setup.md` - локальная среда и проверочные команды.
7. `../README.txt` - инструкция для Windows onedir-сборки; это не дубль `README.md`.

## ACTIVE Runbook И Проверки

| Файл | Для чего |
|---|---|
| `manual-acceptance-runbook.md` | Ручная приемка Telegram, VDS MVP, Windows desktop. |
| `deploy-rollback-runbook.md` | Deploy, backup, restore и rollback. |
| `database-migrations-runbook.md` | Миграции и rollback posture. |
| `event-queue-lifecycle.md` | Жизненный цикл очередей и retry. |
| `windows-backend-acceptance.md` | Windows acceptance для backend flags. |
| `local-development-setup.md` | Локальная Python/Docker/VDS-compose среда разработки. |
| `restore-points.md` | Reference по restore/checkpoint; не отправлять наружу без проверки локальных путей и содержимого snapshot. |

## HISTORY Evidence И Аудиты

| Файл | Статус |
|---|---|
| `goal-completion-audit.md` | `HISTORY`: аудит цели 2026-05-31; не релизный чеклист и не разрешение выкатывать на рабочие ПК. |
| `vds-release-readiness.md` | `HISTORY`: readiness-проверка VDS; сверять со свежим `implementation-log.md`, `changelog.md` и stack overview. |
| `repo-cleanup-inventory.md` | `HISTORY`/`REFERENCE`: правила отделения кода от локальных данных, секретов, логов, backup и release-артефактов. |

## Product, Архитектура И UPDATE Docs

| Файл | Статус |
|---|---|
| `taksklad-system-stack-overview.md` | `ACTIVE`: текущий общий обзор, версия по коду/release manifest `2.0.15` на 15.06.2026. |
| `report-source-rules.md` | `ACTIVE`: DB-first правила отчетов и источников. |
| `user-business-process-guide.md` | `ACTIVE`/`REFERENCE`: пользовательская инструкция и бизнес-процесс, сверять точечные детали с кодом. |
| `project-overview.md` | `UPDATE`: обзор продукта, useful background, не source of truth по текущей архитектуре. |
| `project-architecture.md` | `UPDATE`/`HISTORY`: architecture reference, сверять со stack overview и кодом. |
| `project-knowledge-base.md` | `HISTORY`: knowledge base desktop/Google Sheets периода. |
| `taksklad-full-functionality.md` | `UPDATE`: полный функционал версии `1.1.17` от 26.05.2026; текущую DB-first архитектуру брать из stack overview. |
| `warehouse-ecosystem-roadmap.md` | `HISTORY`/`REFERENCE`: strategic roadmap; часть реализована или перекрыта WMS Core. |
| `roadmap.md` | `HISTORY`: старый roadmap высокого уровня; не использовать как текущий статус без сверки. |
| `product-mvp-2.0-plan.md` | `HISTORY`: исторический план релиза 2.0. |

## Интеграции И Текущие Рабочие Заметки

| Файл | Статус |
|---|---|
| `skladbot-api-key-functionality.md` | `SENSITIVE_HISTORY`: аудит SkladBot API от 23.05.2026. Использовать для ограничений API, но не цитировать ключи/токены/идентификаторы и не считать write-возможности подтвержденными без свежей проверки. |
| `main-refactor-inventory.md` | `WORKING`: инвентаризация refactor `src/taksklad/main.py`, создана 2026-06-21. Сверять с текущим `git status`, потому что рядом есть незавершенные изменения кода. |

## Не Удалять Без Проверки

- `implementation-log.md` и `changelog.md`: это основная история действий агентов.
- runbook-файлы: нужны для эксплуатации и rollback.
- документы про SkladBot/API: могут быть historical и sensitive, но содержат проверенные ограничения интеграции.
- любые файлы, на которые есть ссылки из README, AGENTS или активных задач.

## Кандидаты На Архив После Сверки

- устаревшие roadmap/overview, если их выводы перенесены в `taksklad-system-stack-overview.md`;
- старые acceptance/readiness документы после появления более свежего final audit;
- локальные отчеты и generated artifacts вне `docs`.

Перед архивированием не открывать и не перемещать `reports`, `outputs`, `scan_backups`, реальные Excel/CSV/PDF/DOCX, credentials, `.env*`, токены и локальные рабочие JSON без отдельного подтверждения.
