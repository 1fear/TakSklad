# Report Source Rules

Актуально на: 16.07.2026

Цель документа: зафиксировать, откуда отчеты и складской hot path берут данные и как должны вести себя при ошибках. Главное правило: Postgres/backend является единственным operational source of truth. Google Sheets в runtime не используется.

## Матрица Источников

| Отчет | Endpoint/команда | Основной источник | Важные правила |
| --- | --- | --- | --- | --- |
| Отчет за день TakSklad | `GET /api/v1/reports/day` | Postgres: `orders`, `order_items`, `scan_codes` | Дата считается в бизнес-таймзоне. В отчет попадают заказы по дате отгрузки и сканы выбранного бизнес-дня. |
| Верхние карточки web-панели | `GET /api/v1/admin/dashboard/day-summary` | Postgres: `order_items.created_at`, `orders`, `order_items`, `scan_codes` | Возвраты, отмены и архив без КИЗ не учитываются. |
| Логистика | `GET /api/v1/logistics/report` | Postgres: `orders`, `order_items`, `raw_payload.coordinates`, `client_points` | Самовывоз и stock-shortage blocked заказы исключаются; строки без координат остаются в диагностическом листе. |
| КИЗы по дате | `GET /api/v1/reports/kiz/date` | Postgres: `order_items.scan_codes`, `orders`, import metadata | Выгружаются только КИЗы, записанные в backend. |
| КИЗы по файлу | `GET /api/v1/reports/kiz/source-file` | Postgres: import metadata и `scan_codes` | Для выбранной партии требуется завершённость. |
| Административный экспорт заказов | `GET /api/v1/admin/orders/export.xlsx` | PostgreSQL с admin-фильтрами | XLSX формируется backend; таблица не является хранилищем. |
| Ежедневный SkladBot отчет | `/skladbot_daily ДД.ММ.ГГГГ`, schedule `22:00` | SkladBot API: requests/detail, transactions, products/stock | Read-only отчёт; XLSX содержит заявки, товары заявок, движения, остатки и diagnostics; partial/failed coverage блокирует scheduled send. |
| Ежедневная сверка | `GET /api/v1/reports/reconciliation/day`, schedule после SkladBot daily | PostgreSQL + SkladBot metadata | Сверка не читает Google и не создаёт Google mirror incidents. |

## Ошибки И Edge Cases

- Невалидная дата отчета возвращает явную ошибку, а не молча подставляет сегодня.
- Частичные ошибки SkladBot/API записываются в список `errors`, coverage diagnostics и лист `Ошибки`, а не скрываются как успешный отчет.
- Scheduled SkladBot daily не отправляет Telegram и не запускает daily reconciliation, если coverage не `complete`, есть API/detail/list warning/error, detail limit не дал проверить in-scope/unknown candidates, или отчет содержит `0` operational rows при наличии diagnostic/excluded rows.
- Если за дату отчета есть заявки, созданные сегодня на будущую дату выгрузки, они остаются обычными операционными строками при статусе `Выполнена` + `В архиве`.
- Manual `/skladbot_daily` использует тот же blocker по coverage: partial/failed/truncation/date-conflict/API-error отчет не отправляет XLSX по умолчанию. Ручная отправка неполного отчета возможна только через explicit admin flag `--allow-partial`; такой отчет помечается текстом `НЕПОЛНЫЙ ОТЧЕТ`, не пишет scheduled registry и не запускает reconciliation.
- Detail budget для SkladBot daily сначала расходуется на known in-scope candidates, затем на unknown-date candidates, и только потом на diagnostic/out-of-scope sample. Known out-of-scope rows могут быть записаны в `Исключенные заявки` без detail fetch и не должны вытеснять полезные строки из лимита детализации.
- Coverage counters разделяют aggregate list pages и per-type guard: `pages_fetched`/`list_pages_fetched`, `max_pages_per_request_type`, `list_page_guard_max_total`, `detail_pages_fetched`, `total_http_pages_fetched`. Поэтому aggregate `list_pages_fetched` может быть больше `max_pages_per_request_type`, но не должен превышать `list_page_guard_max_total`.
- Read-style POST endpoints SkladBot daily: `/warehouse/transactions`, `/products`, `/report/stock`. Они проходят через retry для timeout/429/5xx и coverage counters `read_style_post_retry_count`, `read_style_post_error_count`. Write POST вроде `/requests` не использует этот retry policy.
- `/warehouse/transactions`, `/products`, `/report/stock` имеют лимиты и conservative truncation guard. Если endpoint вернул строк ровно по лимиту, coverage получает `movements_possible_truncation`, `products_possible_truncation` или `stock_possible_truncation`, а отчет становится `partial`; scheduled send блокируется.
- Движения фильтруются по выбранной бизнес-дате. Повтор одной и той же строки удаляется только при наличии стабильного ID движения от SkladBot; строки без ID не схлопываются по эвристике, чтобы не потерять реальные одинаковые операции. Счетчик `duplicate_movement_ids` остается в листе `Покрытие`.
- Для SkladBot daily нельзя отбрасывать строку только по старому `created_at`, если list response не содержит достаточных данных для primary daily scope. Такая строка должна дойти до detail fetch в пределах detail limit или сделать отчет `partial`.
- Если включенная operational request имеет конфликт `unloading_date` и `movement_date`, строка остается видимой в `Заявки`/`Диагностика дат`, но coverage получает `date_conflict_unloading_vs_movement`, становится `partial`, и scheduled send блокируется.
- In-scope строки со статусом не `Выполнена + В архиве` попадают в diagnostics/excluded rows. Если такая строка относится к primary daily scope, coverage становится `partial` через `status_not_completed_archived`.
- Любой успешный daily event для date + chat, включая approved manual_catchup, блокирует повторную scheduled-отправку независимо от mode/version.
- До сегодняшнего schedule latest due date равна вчерашней дате, после schedule — сегодняшней. В пределах `SKLADBOT_DAILY_REPORT_LOOKBACK_DAYS` worker сначала догоняет самый старый пропуск и не считает более новый success закрытием старого gap. Без явной настройки окно равно одному дню, чтобы восстановление не рассылало многодневный backlog неожиданно.
- Failed event автоматически повторяется только до начала Telegram delivery, не чаще SKLADBOT_DAILY_REPORT_RETRY_MINUTES и не больше SKLADBOT_DAILY_REPORT_MAX_ATTEMPTS. После sendMessage/sendDocument started результат считается неоднозначным и требует manual recovery, чтобы не создавать дубль.
- Каждый failed retry-cycle ставит не более одного durable `telegram_notification` строго в `TAKSKLAD_AUTOMATION_ALERT_CHAT_ID`. Это должен быть strictly positive numeric personal-like ID, входящий в `TELEGRAM_ADMIN_CHAT_IDS`; отрицательный group ID запрещен, production config без exact route не стартует. Broadcast по admin list и fallback к получателям отчета запрещены; при невалидном route событие не создается, а ошибка остается в daily event/readiness.
- `/ready` после schedule + `SKLADBOT_DAILY_REPORT_GRACE_MINUTES` становится unhealthy, если для latest due date нет успешного event по каждому настроенному daily chat. Public response показывает только `daily_report.status`, `daily_report.due_date`, `daily_report.missing_count` и не раскрывает chat ID.
- Daily report collector использует read-only SkladBot client wrapper. Он не вызывает `create_request` и блокирует write-style POST из daily path. SkladBot create/return workers остаются отдельными write-capable компонентами и не являются частью daily report flow.
- `Сводка` показывает блоки/заявки по категориям, отдельные итоги прихода/расхода в нейтральном поле `Количество` и число строк движений, а также актуальный остаток. Historical opening stock не вычисляется и не заявляется.
- Этот документ описывает local code/test contract. Production live truth не заявляется без отдельной approved проверки live logs/DB/runtime.
- Telegram показывает пользователю действие и причину ошибки, но токены, Bearer credentials и длинные КИЗы маскируются.
- Недоступность Google Sheets не является runtime-событием: у приложения нет Google-клиента и Google worker.
- Critical alerts ежедневной сверки агрегируются по incident/date/source и содержат прямое следующее действие. Повторный запуск за ту же дату не создает дубль Telegram-события.
- Для логистики адреса вида `Самовывоз`, `Самовывоз со склада`, `Самовывоз: склад` не должны попадать в маршрут даже при наличии координат.
- Delivery-заказы без координат не должны исчезать из логистического XLSX: они остаются вне маршрутного листа, но видны логисту в листе `Требуют координаты` с причиной `Нет координат` или `Невалидные координаты`.
- Для сохраненных точек логистический XLSX должен каждый раз подставлять текущий таймслот из `client_points`. Неизвестные точки остаются на дефолте `10:00-18:00`, чтобы старый импорт не блокировал отчет.
- Логистика не смешивает SkladBot и Smartup в одном поле: `ID заявки SkladBot` берет WH-R/ID SkladBot, `ID заявки Smartup` берет `smartup:<deal_id>`, а `ID источника` сохраняет исходный `ID заказа`/`ID импорта` для трассировки.
- Новый лист `Orders` не содержит отдельных колонок цены и типа оплаты. Тип оплаты сохраняется в диагностическом листе `Требуют координаты`; маршрутный лист не должен искусственно добавлять поля, которых нет в шаблоне.

## Проверочные Тесты

- `tests.test_backend_api_persistence` проверяет DB day report, web dashboard summary по дате загрузки, business timezone, логистику, таймслоты сохраненных точек, лист `Требуют координаты`, KIZ source/date exports и invalid report date.
- `tests.test_skladbot_daily_report` проверяет SkladBot daily XLSX, листы движений/coverage/errors, spreadsheet formula safety, стабильный movement-ID dedupe, пустой набор, SKU-колонки, `acceptedAmount`, отдельную строку `Отгрузка в браке`, page-based `/requests` crawl, coverage diagnostics, excluded rows, July 7 transfer batch regression, truncation/date-conflict/status partial coverage, read-style POST retry, manual partial block/override, admin-only bounded failure alert, scheduled partial-send block, detail-budget priority, split max-pages counters, source identity keys, scheduled registry и retry на `429`.
- `tests.test_readiness_policy` проверяет безопасную сериализацию и OpenAPI-контракт `daily_report` без chat ID.
- `tests.test_backend_telegram_import` проверяет Telegram-ошибки логистики/KIZ и ограничение меню последних файлов.
- `tests.test_reconciliation_service` проверяет DB/SkladBot сверку, SkladBot gaps и dedupe Telegram alerts без Google runtime.
