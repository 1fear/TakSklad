# Report Source Rules

Актуально на: 16.06.2026

Цель документа: зафиксировать, откуда отчеты берут данные и как должны вести себя при ошибках. Главное правило: Google Sheets не является source of truth для отчетов, он остается зеркалом/export.

## Матрица Источников

| Отчет | Endpoint/команда | Основной источник | Google Sheets | Важные правила |
| --- | --- | --- | --- | --- |
| Отчет за день TakSklad | `GET /api/v1/reports/day` | Postgres: `orders`, `order_items`, `scan_codes` | Не используется | Дата считается в бизнес-таймзоне. В отчет попадают заказы по дате отгрузки и сканы, попавшие в выбранный бизнес-день. |
| Логистика | `GET /api/v1/logistics/report` | Postgres: `orders`, `order_items`, `raw_payload.coordinates` | Не используется | Самовывоз и заказы без валидных координат исключаются. Пустая дата или отсутствие маршрутизируемых заказов возвращает понятную ошибку. |
| КИЗы по дате | `GET /api/v1/reports/kiz/date` | Postgres: `order_items.scan_codes`, `orders`, import metadata | Не используется | Выгружаются только КИЗы, записанные в backend. Частичная дата разрешена: выгружается то, что реально отпикано. |
| КИЗы по файлу | `GET /api/v1/reports/kiz/source-file` | Postgres: `source_file`, `backend_import_id`, `scan_codes` | Не используется | Один filename может иметь несколько `source_key`. По файлу требуется завершенность выбранной партии, иначе backend возвращает ошибку. |
| Ежедневный SkladBot отчет | `/skladbot_daily ДД.ММ.ГГГГ`, schedule `22:00` | SkladBot API: requests/detail, transactions, products/stock | Не используется | В итог попадают только заявки `Выполнена` + `В архиве`. Дата факта берется из даты закрытия/архивации, если SkladBot ее отдал; иначе закрытая заявка попадает в ближайший отчет как впервые найденная выполненной и затем защищается registry от повторной отправки. Фактическая приемка берется из `acceptedAmount`, значение уже в блоках. |
| Ежедневная сверка | `GET /api/v1/reports/reconciliation/day`, schedule после SkladBot daily | Postgres: `orders`, `order_items`, SkladBot metadata в `raw_payload` | Только зеркало для сравнения | Основной статус считается по DB. Google-only, DB-only active, status mismatch и WH-R mismatch считаются отдельно. При падении Google создается mirror issue, но DB workflow не считается упавшим. |

## Ошибки И Edge Cases

- Невалидная дата отчета возвращает явную ошибку, а не молча подставляет сегодня.
- Частичные ошибки SkladBot/API записываются в список `errors` и лист `Ошибки`, а не скрываются как успешный отчет.
- Telegram показывает пользователю действие и причину ошибки, но токены, Bearer credentials и длинные КИЗы маскируются.
- Если Google Sheets недоступен, DB-first отчеты продолжают работать, потому что Google не участвует в чтении отчетов.
- Ежедневная сверка при недоступном Google создает warning incident `google_mirror_unavailable` и не отправляет critical alert, если в DB/SkladBot нет критичных расхождений.
- Critical alerts ежедневной сверки агрегируются по incident/date/source и содержат прямое следующее действие. Повторный запуск за ту же дату не создает дубль Telegram-события.
- Для логистики адреса вида `Самовывоз`, `Самовывоз со склада`, `Самовывоз: склад` не должны попадать в маршрут даже при наличии координат.

## Проверочные Тесты

- `tests.test_backend_api_persistence` проверяет DB day report, business timezone, логистику, KIZ source/date exports и invalid report date.
- `tests.test_skladbot_daily_report` проверяет SkladBot daily XLSX, SKU-колонки, `acceptedAmount`, частичные ошибки и retry на `429`.
- `tests.test_backend_telegram_import` проверяет Telegram-ошибки логистики/KIZ и ограничение меню последних файлов.
- `tests.test_reconciliation_service` проверяет DB-first ежедневную сверку, отдельные счетчики Google/DB/WH-R/status, SkladBot gaps, dedupe Telegram alerts и Google-down mirror issue.
