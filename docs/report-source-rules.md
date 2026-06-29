# Report Source Rules

Актуально на: 29.06.2026

Цель документа: зафиксировать, откуда отчеты и складской hot path берут данные и как должны вести себя при ошибках. Главное правило: Postgres/backend является source of truth для backend-mode hot path, Google Sheets не является source of truth для отчетов и сканирования, он остается зеркалом/export.

## Матрица Источников

| Отчет | Endpoint/команда | Основной источник | Google Sheets | Важные правила |
| --- | --- | --- | --- | --- |
| Отчет за день TakSklad | `GET /api/v1/reports/day` | Postgres: `orders`, `order_items`, `scan_codes` | Не используется | Дата считается в бизнес-таймзоне. В отчет попадают заказы по дате отгрузки и сканы, попавшие в выбранный бизнес-день. |
| Верхние карточки web-панели | `GET /api/v1/admin/dashboard/day-summary` | Postgres: `order_items.created_at`, `orders`, `order_items`, `scan_codes` | Не используется | Дата считается по дате загрузки позиции в backend. `Всего заказов` - уникальные операционные заказы, у которых есть позиции, загруженные в выбранный день. `Всего блоков` - все блоки в этих загруженных позициях, готовые и активные. `Отскан. блоков` - текущий прогресс сканирования по этим позициям. Возвраты, отмены, архив без КИЗ и строки `removed_from_google_sheet` не учитываются. |
| Логистика | `GET /api/v1/logistics/report` | Postgres: `orders`, `order_items`, `raw_payload.coordinates`, `client_points` | Не используется | Самовывоз и stock-shortage blocked заказы исключаются. Delivery-заказы с валидными координатами попадают в лист `Заявки`; delivery без валидных координат попадают в лист `Требуют координаты`. `Доставка С/ПО` берется из сохраненной точки `client_name + address`, fallback остается `10:00-18:00`. Если на дату нет ни одного delivery-кандидата, backend возвращает понятную ошибку. |
| КИЗы по дате | `GET /api/v1/reports/kiz/date` | Postgres: `order_items.scan_codes`, `orders`, import metadata | Не используется | Выгружаются только КИЗы, записанные в backend. Частичная дата разрешена: выгружается то, что реально отпикано. |
| КИЗы по файлу | `GET /api/v1/reports/kiz/source-file` | Postgres: `source_file`, `backend_import_id`, `scan_codes` | Не используется | Один filename может иметь несколько `source_key`. По файлу требуется завершенность выбранной партии, иначе backend возвращает ошибку. |
| Ежедневный SkladBot отчет | `/skladbot_daily ДД.ММ.ГГГГ`, schedule `22:00` | SkladBot API: requests/detail, transactions, products/stock | Не используется | В лист `Заявки` и Telegram-счетчики попадают только заявки `Выполнена` + `В архиве`, у которых `created_at`/`createdAt` равен дате отчета в бизнес-таймзоне. `updated_at`, `unloading_date`, `completed_at`, `archived_at` и fallback `впервые найдена выполненной` не переносят старые заявки в сегодняшний отчет. Складские движения берутся отдельно через `/warehouse/transactions` за дату отчета и дополнительно фильтруются по дате строки перед попаданием в лист `Движения`/строку `Движения`. Старый WH-R с движением сегодня может быть виден в `Движениях`, но не считается сегодняшней заявкой, если создан не сегодня. Фактическая приемка для включенных сегодняшних заявок берется из `acceptedAmount`, значение уже в блоках. Тип `Отгрузка в браке` показывается отдельной строкой в Telegram-сводке и XLSX-сводке, но в движении остатков остается расходом. |
| Ежедневная сверка | `GET /api/v1/reports/reconciliation/day`, schedule после SkladBot daily | Postgres: `orders`, `order_items`, SkladBot metadata в `raw_payload` | Только зеркало для сравнения | Основной статус считается по DB. Google-only, DB-only active, status mismatch и WH-R mismatch считаются отдельно. При падении Google создается mirror issue, но DB workflow не считается упавшим. |

## Ошибки И Edge Cases

- Невалидная дата отчета возвращает явную ошибку, а не молча подставляет сегодня.
- Частичные ошибки SkladBot/API записываются в список `errors` и лист `Ошибки`, а не скрываются как успешный отчет.
- Telegram показывает пользователю действие и причину ошибки, но токены, Bearer credentials и длинные КИЗы маскируются.
- Если Google Sheets недоступен, DB-first отчеты продолжают работать, потому что Google не участвует в чтении отчетов.
- Ежедневная сверка при недоступном Google создает warning incident `google_mirror_unavailable` и не отправляет critical alert, если в DB/SkladBot нет критичных расхождений.
- Critical alerts ежедневной сверки агрегируются по incident/date/source и содержат прямое следующее действие. Повторный запуск за ту же дату не создает дубль Telegram-события.
- Для логистики адреса вида `Самовывоз`, `Самовывоз со склада`, `Самовывоз: склад` не должны попадать в маршрут даже при наличии координат.
- Delivery-заказы без координат не должны исчезать из логистического XLSX: они остаются вне маршрутного листа, но видны логисту в листе `Требуют координаты` с причиной `Нет координат` или `Невалидные координаты`.
- Для сохраненных точек логистический XLSX должен каждый раз подставлять текущий таймслот из `client_points`. Неизвестные точки остаются на дефолте `10:00-18:00`, чтобы старый импорт не блокировал отчет.

## Проверочные Тесты

- `tests.test_backend_api_persistence` проверяет DB day report, web dashboard summary по дате загрузки, business timezone, логистику, таймслоты сохраненных точек, лист `Требуют координаты`, KIZ source/date exports и invalid report date.
- `tests.test_skladbot_daily_report` проверяет SkladBot daily XLSX, SKU-колонки, `acceptedAmount`, отдельную строку `Отгрузка в браке`, частичные ошибки и retry на `429`.
- `tests.test_backend_telegram_import` проверяет Telegram-ошибки логистики/KIZ и ограничение меню последних файлов.
- `tests.test_reconciliation_service` проверяет DB-first ежедневную сверку, отдельные счетчики Google/DB/WH-R/status, SkladBot gaps, dedupe Telegram alerts и Google-down mirror issue.
