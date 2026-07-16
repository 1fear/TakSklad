# TakSklad: DB-only архитектура

Актуально на: 16.07.2026

## Решение

PostgreSQL через backend API — единственный operational source of truth.
Google Sheets полностью выведен из runtime: приложение не читает и не пишет
таблицы, не запускает Google worker и не использует Google как fallback.

Excel остаётся только переносимым форматом:

- web и desktop отправляют XLSX в backend для preview/import;
- Telegram и Smartup импортируют через тот же backend-контур;
- административная выгрузка формируется backend из PostgreSQL;
- отчёты строятся из PostgreSQL или из явно указанного внешнего API SkladBot.

## Компоненты

| Компонент | Роль | Рабочие данные |
|---|---|---|
| Windows desktop | сканирование, печать, локальная страховочная очередь | backend API; локально только cache, `pending_backend_events` и scan backup |
| Web | импорт Excel, складские действия, контроль и выгрузка | backend API |
| Backend | бизнес-правила, KIZ lifecycle, импорт, отчёты, аудит | PostgreSQL |
| Telegram worker | импорт, отчёты и уведомления | backend/PostgreSQL |
| SkladBot workers | заявки, возвраты и read-only отчёты | SkladBot API + PostgreSQL |
| Smartup worker | автоматический импорт заказов | Smartup API + PostgreSQL |

## Потоки

### Импорт

`XLSX → backend preview → подтверждение → нормализация → PostgreSQL → audit`

Повторный импорт не должен создавать дубли. Исходный файл, его hash, строки и
идентификаторы партии сохраняются в import metadata.

### Сканирование

`scanner → desktop/web → backend availability check → transaction → scan_codes + kiz_codes + kiz_movements + audit_log`

Если backend недоступен, desktop не записывает КИЗ в другое хранилище. Событие
остаётся в локальной `pending_backend_events` и повторяется идемпотентно после
восстановления связи; исходный скан также остаётся в локальном backup.

### Возврат и повторная отгрузка

Возврат пишет movement `return` и освобождает КИЗ. Следующая подтверждённая
отгрузка того же кода пишет `re_outbound`. Активная привязка одного КИЗа к двум
заказам запрещена на backend.

### Отчёты и экспорт

Дневной, KIZ, логистический и административный XLSX формируются backend из БД.
SkladBot daily является отдельным read-only отчётом из SkladBot API и не меняет
операционные данные.

## Отказоустойчивость

- PostgreSQL backup/PITR — восстановление server-side данных.
- `pending_backend_events` и scan backup — страховка desktop при потере сети.
- Очередь `pending_events` — server-side фоновые операции с lease/retry/audit.
- Google Sheets не является rollback-механизмом и не включается автоматически.

## Release boundary

Код DB-only можно подготовить и проверить в отдельном worktree параллельно с
другими задачами. Merge в `main`, version bump, публикация Windows-архива,
изменение `version.json`, production deploy и cutover выполняются только после:

1. завершения параллельных задач;
2. повторной сверки с актуальным `origin/main`;
3. полного CI/verifier;
4. backup/data preflight;
5. явного подтверждения production-write владельцем.

Пошаговый порядок, stop conditions и rollback описаны в
`docs/runbook/google-sheets-decommission.md`.
