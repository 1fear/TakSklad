# TakSklad: полное описание функционала

Дата сверки: 2026-08-21. Ревизия `a8e1836`, backend `2.0.51`,
схема базы `20260817_0022`

Этот документ описывает **то, что работает сейчас**. Каждое утверждение
сверено с кодом, ссылка на файл стоит рядом с утверждением
Где поведение зависит от переменной окружения, указано имя переменной
и её состояние в боевом контуре на дату сверки

Предыдущая версия документа описывала контур до перехода на backend:
desktop-приложение как основной инструмент и Google Sheets как источник истины
Она осталась в истории git и достаётся так:

```
git log --oneline -- docs/taksklad-full-functionality.md
git show <ревизия>:docs/taksklad-full-functionality.md
```

Что искать в других файлах:

| Нужно | Куда идти |
|---|---|
Текущее состояние прода, открытые пункты | [CURRENT_STATUS.md](CURRENT_STATUS.md)
Риски и расхождения на 21.08.2026 | [taksklad-full-audit-2026-08-21.md](taksklad-full-audit-2026-08-21.md)
Откуда берутся цифры в каждом отчёте | [report-source-rules.md](report-source-rules.md)
Пробелы между веб-панелью и приложением | [web-desktop-parity.md](web-desktop-parity.md)
Жизненный цикл очереди событий | [event-queue-lifecycle.md](event-queue-lifecycle.md)
Целостность КИЗов, незакрытые инварианты | [kiz-lifecycle-integrity-architecture-plan-2026-07-30.md](kiz-lifecycle-integrity-architecture-plan-2026-07-30.md)

---

## 1. Назначение

Складская система для собственного склада: заказы приходят из двух источников,
собираются по маркам КИЗ, закрываются, попадают в отчёты клиенту, логистике
и в заявки WMS (SkladBot)

Главное свойство, которому подчинено всё остальное: **любое изменение
складского остатка проходит через движение, источник, причину и запись
в журнал аудита**. Отчёт или таблица никогда не являются базой данных

## 2. Контур

Пять процессов в Docker Compose (`deploy/vds/docker-compose.yml`):

| Процесс | Команда | Роль |
|---|---|---|
`backend-api` | FastAPI | 68 HTTP-роутов, единственный писатель через API
`frontend` | статика React | веб-панель администратора и оператора
`telegram-worker` | `app.telegram_worker_runner` | приём Excel, отчёты, меню, логи
`skladbot-worker` | `app.skladbot_worker_runner` | заявки WMS, возвраты, суточный отчёт
`smartup-auto-import-worker` | `app.smartup_auto_import_worker` | автоимпорт заказов по слотам

Плюс PostgreSQL 16 и Traefik как входной прокси

Windows-приложение на Tkinter (`src/taksklad`) работает **только через backend**
и разобрано в разделе 15

Единственный источник истины это PostgreSQL. Обоснование и границы:
[db-only-architecture.md](db-only-architecture.md)

## 3. Два входа заказов

### 3.1. Автоимпорт из Smartup

Модуль `backend/app/smartup_auto_import.py`, воркер
`backend/app/smartup_auto_import_worker.py`

Работает по слотам: список времён задаёт `SMARTUP_AUTO_IMPORT_TIMES`,
финальный слот `SMARTUP_AUTO_IMPORT_FINAL_TIME`
Опоздавший слот всё ещё считается своим в пределах
`SMARTUP_AUTO_IMPORT_SLOT_GRACE_MINUTES`, и это единственная причина, по которой
пропущенный слот не выглядит падением

Что делает один прогон:

1. Забирает выгрузку заказов из Smartup (`order$export`)
2. Нормализует строки тем же нормализатором, что и Excel-импорт
3. Если координат нет, ставит адрес геокодером Яндекса
   (`reverse_geocode_yandex` в `backend/app/excel_importer.py`,
   ключ `YANDEX_GEOCODER_API_KEY`)
4. Создаёт импорт и заказы через `create_import`
5. Строит dry-run заявок SkladBot и ставит события на создание
6. Формирует клиентскую выгрузку и отчёт логистики, ставит их в очередь отправки

Флаги, которые меняют поведение:

| Переменная | Что включает |
|---|---|
`SMARTUP_AUTO_IMPORT_ENABLED` | сам автоимпорт
`SMARTUP_AUTO_IMPORT_BACKEND_IMPORT_ENABLED` | запись заказов в TakSklad
`SMARTUP_AUTO_IMPORT_CHANGE_STATUS_ENABLED` | обратную запись статуса в Smartup
`SMARTUP_AUTO_IMPORT_SAGA_MODE` | разделение сделок по `deal_id`: `disabled`, `shadow`, `enforced`
`SMARTUP_AUTO_IMPORT_DISABLED_WEEKDAYS` | дни недели без импорта

На 21.08.2026 `SMARTUP_AUTO_IMPORT_SAGA_MODE=disabled`, поэтому таблицы
`smartup_fulfillments` и `smartup_fulfillment_orders` пусты: механизм
разделения сделок в прод не включён. Замысел описан в
[smartup-deal-split-design-2026-07-30.md](smartup-deal-split-design-2026-07-30.md),
состояния саги в `backend/app/smartup_saga.py`

Автоимпорт это крупнейший вход: на 21.08.2026 из 6031 заказа 3172 имеют
`source = smartup_auto`

### 3.2. Excel через Telegram

Оператор отправляет боту файл `.xlsx`, воркер
(`backend/app/telegram_import_processor.py`) скачивает его и отдаёт в тот же
`POST /api/v1/imports`

Ограничения задают `TELEGRAM_WORKER_MAX_FILE_BYTES`,
`TELEGRAM_WORKER_FILE_TIMEOUT_SECONDS`, `TELEGRAM_WORKER_IMPORT_TIMEOUT_SECONDS`

Ошибка одного обновления Telegram не блокирует следующие файлы, это закрыто
тестами `tests/test_backend_telegram_import.py`

Из 6031 заказа 2857 пришли этим путём (`source = telegram`)

### 3.3. Ручные входы

`POST /api/v1/imports/excel` и `POST /api/v1/imports/excel/preview` в веб-панели
Плюс два заказа в базе со источниками `backend` и `manual_correction`,
это следы разовых ремонтов

## 4. Импорт и нормализация

### 4.1. Два живых парсера

Excel разбирают два независимых модуля, и оба отправляют результат
в один и тот же `POST /api/v1/imports`:

- `backend/app/excel_importer.py` для веб-панели и Telegram
- `src/taksklad/excel_import.py` и `src/taksklad/excel_normalizer.py` для приложения

Новый ключ строки нужно заводить в обоих, плюс в `ImportFieldName`
(`backend/app/schemas.py:18`), это `Literal`, и незнакомый ключ туда не пройдёт

### 4.2. Обязательные колонки

`REQUIRED_ALIASES` в `backend/app/excel_importer.py:75`, четыре смысловых поля
и 35 алиасов на них
Шаблон подходит, если для каждого поля нашёлся хотя бы один алиас

| Поле | Что означает | Принимаемые заголовки |
|---|---|---|
`client` | Клиент или торговая точка | `ФИО или Наименование торговой точки`, `Клиент`, `Юр. лицо`, `Юр лицо`, `Наименование`, `Покупатель`, `Контрагент`, `Наименование клиента`, `Название компании`, `Название компании/Имя человека`, `Юридическое лицо`
`payment` | Тип оплаты | `Тип оплаты`, `Оплата`, `Способ оплаты`, `Форма оплаты`, `Комментарий оплаты`
`product` | Наименование товара | `Наименование Товара`, `Товары`, `Товар`, `Номенклатура`, `ТМЦ`, `SKU`, `Артикул`, `Продукт`, `Наименование продукции`
`quantity` | Количество | `Кол-во`, `Количество`, `Кол-во ШТ`, `Количество ШТ`, `Количество заказа`, `Кол-во заказа`, `Заказано`, `В заявке`, `Штук`, `ШТ`

### 4.3. Необязательные колонки

`OPTIONAL_ALIASES` там же, десять полей:

| Поле | Что означает | Принимаемые заголовки |
|---|---|---|
`date` | дата отгрузки | `Дата доставки`, `Дата отгрузки`, `Дата получения заказа`, `Дата заказа`, `Дата`, `Дата выгрузки`, `Дата поставки`, `Дата документа`
`coordinates` | координаты точки | `Координаты`, `Координаты клиента`, `GPS-координаты клиента`, `GPS`, `Локация`
`address` | адрес доставки | `Адрес доставки`, `Адрес`, `Адрес клиента`, `Адрес торговой точки`, `Адрес получателя`, `Локация`
`representative` | торговый представитель | `Торговый представитель`, `ТП`, `Менеджер`, `Номер телефона`, `Торговый`, `Агент`, `Ответственный`
`blocks` | количество в блоках | `Кол-во блок`, `Кол-во блоков`, `Блоков`, `Количество блоков`, `План КИЗ`
`unit_price` | цена за единицу | `Цена`, `Цена за блок`, `Цена блока`, `Цена за штуку`
`line_total` | сумма строки | `Цена заказа`, `Сумма с переоценкой`, `Сумма`, `Итого сумма`, `Итого`
`skladbot_request_number` | номер заявки SkladBot | `Номер заявки SkladBot`, `Заявка SkladBot`, `Номер заявки`
`skladbot_request_id` | идентификатор заявки SkladBot | `ID заявки SkladBot`, `SkladBot ID`, `ID заявки`
`smartup_order_id` | идентификатор заказа Smartup | `ИД заказа`, `Идентификатор заказа`, `Smartup ИД заказа`, `ID заказа Smartup`, `Smartup ID`

Дата отгрузки ищется ещё и вне таблицы: `CONTEXT_DATE_ALIASES` смотрит в шапку
листа над заголовками
Дата в шапке листа: `Дата доставки`, `Дата отгрузки`, `Дата поставки`

Три последних поля это связи с внешними системами: номер и идентификатор заявки
SkladBot и идентификатор заказа Smartup. Именно они делают импорт повторяемым
без дублей

### 4.4. Что делает нормализация

`normalize_import_row` в `backend/app/imports_service.py`

- вычисляет `order_key`, ключ группировки заказа
- чистит адрес от названия страны (`Узбекистан`, `Uzbekistan`, `O'zbekiston`)
- приводит количество к штукам и блокам
- определяет тип оплаты по классификатору свободного текста
  (`backend/app/payment_policy.py`)
- проставляет `raw_payload` со всем, что не разобрано в колонки

Тип оплаты держится на подстрочном классификаторе, а не на справочнике
На боевых данных значений ровно два: `Терминал` и `Перечисление`
Это зафиксировано как слабое место в пункте 6.15 плана целостности

### 4.5. Дедупликация

Дедуп идёт на трёх уровнях:

1. по `order_key` заказ находится и дополняется, а не создаётся заново
2. по связке с заявкой SkladBot
   (`find_skladbot_linked_order_for_import_rows` в `imports_service.py`)
3. повторная загрузка того же файла не создаёт новых заказов

Важное следствие, которое легко упустить: **дозаказ Telegram-файлом молча
вливается в уже отгруженный заказ и не рождает новую заявку в СкладБоте**
Разделение включено только для источника Smartup

### 4.6. Склейка позиций

Повтор одного товара внутри одного заказа сливается в одну позицию
Замысел и границы: [order-position-merge-2026-07-31.md](order-position-merge-2026-07-31.md)
На боевых данных счётчик `merged_position_rows` пока нулевой, то есть
на живом потоке механизм ещё не срабатывал

## 5. Группировка заказов и заявки SkladBot

Заказ это группа позиций с одним `order_key`. В группировку входят
номер заявки SkladBot, клиент, тип оплаты и адрес

Номер заявки либо приходит колонкой из шаблона, либо подтягивается воркером
(`backend/app/skladbot_worker.py`, интервал `SKLADBOT_WORKER_INTERVAL_SECONDS`)

Создание заявок:

- `backend/app/skladbot_request_dry_run.py` строит предварительный расчёт,
  его видно в панели по `GET /api/v1/admin/skladbot/dry-runs`
- событие `skladbot_request_create` создаёт заявку в WMS
- режим задаёт `SKLADBOT_CREATE_REQUESTS_MODE`
- лимиты: `SKLADBOT_REQUEST_CREATE_LIMIT`, `SKLADBOT_REQUESTS_LIMIT`,
  `SKLADBOT_DETAIL_LIMIT`, задержка `SKLADBOT_REQUEST_DELAY_SECONDS`

Признание уже существующей заявки по содержимому, а не по маркеру, живёт
за флагом `SKLADBOT_CONTENT_RECONCILE_ENABLED`
В боевом `.env` этого ключа нет, механизм выключен

Ручная привязка через `already_linked` не обновляет `skladbot_status` заказа,
поэтому синхронизация продолжает держать заказ в `pending`, пока статус
не поправят отдельно

## 6. Сканирование КИЗов

### 6.1. Приём кода

`POST /api/v1/scans`. Формат проверяется на сервере
(`backend/app/kiz_format.py`), а не только в клиенте

| Правило | Значение |
|---|---|
Минимальная длина | 20
Максимальная длина | 120
Известные длины | 35 (штука), 67 (короб)
Голова марки | `01` плюс 14 цифр GTIN
Запрещено | кириллица, пробельные символы, две марки, склеенные в один код

Серверная проверка появилась потому, что в боевом реестре уже лежали коды
длиной 3, 46, 65, 70 и 71 символа, включая марку с приклеенным номером заказа
Правила проверены на всём реестре: 21 091 код из 21 096 проходят,
а 5 отказов это ровно известные артефакты

### 6.2. Короб и штука

`backend/app/scan_quantities.py`

Тип кода определяется по префиксу GTIN: `AGGREGATE_BOX_PRODUCT_PREFIXES` дают
короб, `UNIT_PRODUCT_PREFIXES` дают штуку
Короб закрывает `AGGREGATE_BOX_BLOCK_QUANTITY = 50` блоков одним сканом

Короб больше остатка позиции остаётся заблокированным, а не досчитывается
частично

### 6.3. Дубли и блок-лист

Один КИЗ нельзя использовать дважды в активной работе. Попытка даёт понятную
ошибку с указанием, где код уже занят

Отдельно есть аварийный блок-лист (`backend/app/kiz_blocklist.py`,
`TAKSKLAD_BLOCKED_KIZ_CODES`): перечисленные коды не принимаются вообще
Это рычаг быстрого сдерживания, а не штатный инструмент

На боевых данных 1086 кодов встречаются больше одного раза, и из них 1083
это штатная пара `returned` плюс `completed`, то есть возврат и перевыпуск
Три кода дублируются внутри одного заказа, это ненормально и требует разбора

### 6.4. Отмена скана

`POST /api/v1/scans/undo` снимает последний скан позиции

`undo_scan` работает и напрямую, это штатный путь освобождения КИЗа
из завершённого заказа, но требует переоткрытия заказа: без этого код остаётся
занятым и выдаёт «уже использован в другом задании»

### 6.5. Порядок блокировок

`backend/app/order_locking.py` задаёт единый порядок захвата для всех писателей
заказа и КИЗа. До него пять сценариев параллельной работы веб-панели
и приложения ломались, три давали настоящий `DeadlockDetected` PostgreSQL,
а два теряли работу оператора беззвучно

## 7. Завершение заказа

`POST /api/v1/orders/{order_id}/complete`

Статусы (`backend/app/order_statuses.py`):

| Статус | Смысл |
|---|---|
`not_completed` | активный, в сборке
`completed` | собран и закрыт
`returned` | возвращён
`archived_no_kiz` | закрыт административно без КИЗов
`cancelled` | отменён
`removed_from_google_sheet` | исторический, из выведенного контура

`COMPLETED_STATUSES` это `completed`, `done`, `closed` и `returned`
`INACTIVE_ORDER_STATUSES` добавляет к ним `archived_no_kiz` и `cancelled`

**Признак завершённости сборки это статус заказа, а не сравнение блоков**
Заказ можно закрыть без сканирования, и на боевых данных таких 179 из 5631
Любая логика вида «отсканировано меньше запланированного, значит ещё собирают»
на этих заказах врёт. Возврат тоже входит в `COMPLETED_STATUSES`, поэтому
ветку возврата надо проверять первой

### 7.1. Незакрытые инварианты

Четыре инварианта не выполняются, и это зафиксировано тестами
`tests/test_kiz_lifecycle_guards.py` под `@unittest.expectedFailure`:

| Инвариант | Пункт плана | Что происходит |
|---|---|---|
`reset-rescan` на закрытом заказе | 6.12 | штатно открывает закрытый заказ и стирает сканы
`complete-without-kiz` при наличии сканов | 6.11 | закрывает заказ, у которого сканы уже есть
Импорт в неактивный заказ | 13.1 | дописывает новую позицию
Завершение по счётчику | 6.19 | доверяет `scanned_blocks`, а не строкам сканов

Реализованная цена на 21.08.2026: 179 завершённых заказов без единого скана
и 138 позиций с расхождением счётчика, из них 131 в `completed` с разрывом
13 281 блок

## 8. Возвраты и перевыпуск

`GET /api/v1/returns`, `GET /api/v1/returns/lookup`,
`POST /api/v1/returns/{order_id}`

Возврат всегда рождает заявку в СкладБоте. Отчёт логистики заказы в статусе
`returned` исключает

Перевыпуск возвращённого заказа на следующий день собирается через
`re_outbound`, это отдельная операция, а не повторный импорт

## 9. Transfer КИЗ

`backend/app/transfer_kiz_service.py`

Для заказов с оплатой перечислением после последнего скана автоматически
готовится выгрузка КИЗов клиенту. Механика: событие
`transfer_kiz_completion_check` проверяет готовность по базе,
затем `transfer_kiz_client_delivery` отправляет файл

Ограничение: проверка ставится только для позиции из Telegram-импорта,
у которой в `raw_payload` есть `backend_import_id`
Если блокер снят не последним сканом, доставка не родится и её ставят вручную

## 10. Логистика

### 10.1. Зоны

`backend/app/logistics_zone_service.py`

Две зоны: `city` и `region`. Порядок правил важен:

1. точное совпадение имени клиента со справочником областных точек даёт `region`
2. нет координат даёт `city`
3. точка внутри полигона Ташкента (`TASHKENT_CITY_POLYGON`, буфер
   `CITY_BUFFER_METERS = 1000`) даёт `city`
4. всё остальное даёт `region`

Совпадение со справочником только по точному имени. Прежние догадки, точка
в 150 метрах и 70% общих значимых слов, уводили городские заказы в область:
по сверке 06-10.08.2026 так уехали семь чужих заказов

`ZONE_UNASSIGNED` оставлена в коде как страховка, классификатор её больше
не возвращает

Справочник областных точек живёт в таблице `logistics_region_points`,
наполнение через `tools/seed_logistics_region_points.py`,
холостой прогон через `tools/logistics_zone_dry_run.py`

### 10.2. Календарь

`backend/app/logistics_calendar_service.py`

Нерабочие дни по умолчанию суббота и воскресенье
(`DEFAULT_NON_WORKING_WEEKDAYS = (5, 6)`), переопределения хранит
`logistics_calendar_days`

Дата доставки сдвигается на ближайший рабочий день,
максимум `MAX_DELIVERY_SHIFT_DAYS = 31`

Роуты: `GET /api/v1/admin/logistics-calendar`,
`POST /api/v1/admin/logistics-calendar/day`,
`GET /api/v1/admin/logistics-calendar/day/{service_date}/orders`

Завершённый заказ без сканов в детализации дня считается собранным,
именно из-за раздела 7

### 10.3. Точки клиентов и окна

Таблица `client_points`, на 21.08.2026 в ней 1316 записей
Роуты `GET /api/v1/admin/client-points`,
`GET /api/v1/admin/client-points/order-summary`,
`POST /api/v1/admin/client-points/timeslot`

Заказы одного клиента в одной точке идут одной остановкой

### 10.4. Отчёт логистики

`backend/app/logistics_service.py`, роуты `GET /api/v1/logistics/dates`
и `GET /api/v1/logistics/report`

Лист `Orders` с полями маршрута: тип заказа, внешний ID, клиент, телефон,
координаты и адрес забора и доставки, окна времени и перерыва, время обслуживания
Строки без координат уходят на отдельный лист `Требуют координаты`,
а не исчезают

Из отчёта исключаются самовывоз и заказы, заблокированные по остатку
Адрес доставки в smartup-импорте обычно делает геокодер, потому что поля
Smartup чаще всего пустые

## 11. Отчёты

| Отчёт | Роут или расписание | Что внутри |
|---|---|---|
День | `GET /api/v1/reports/day` | сводка по заказам и сканам за дату
Сводка дня для панели | `GET /api/v1/admin/dashboard/day-summary` | то же для дашборда
КИЗ по дате | `GET /api/v1/reports/kiz/date` | марки по дате отгрузки
КИЗ по периоду | `GET /api/v1/reports/kiz/range` | то же за диапазон
КИЗ по исходному файлу | `GET /api/v1/reports/kiz/source-file` | марки по одному загруженному файлу
Списки для выбора | `/reports/kiz/dates`, `/reports/kiz/source-files` | доступные даты и файлы
Сверка за день | `GET` и `POST /api/v1/reports/reconciliation/day` | расхождения между TakSklad и WMS
Экспорт заказов | `GET /api/v1/admin/orders/export.xlsx` | выгрузка для админа
Суточный отчёт SkladBot | `22:00` | листы `Сводка`, `Заявки`, `Товары заявок`, движения, остатки

КИЗ-отчёт по файлу не соберётся, если хотя бы одна позиция файла осталась
`not_completed`: один незавершённый заказ блокирует отчёт по всему файлу

Сверка (`backend/app/reconciliation_service.py`) заводит инцидент типа
`skladbot_gap` как `critical`, если расхождение найдено

Откуда именно берётся каждая цифра: [report-source-rules.md](report-source-rules.md)

## 12. Telegram

### 12.1. Доступ

Белый список чатов `TELEGRAM_ALLOWED_CHAT_IDS`, административные чаты
`TELEGRAM_ADMIN_CHAT_IDS`. Токен в `TELEGRAM_BOT_TOKEN`

Ни один идентификатор чата и ни один токен не попадают в логи, документацию,
экспорты и скриншоты. За этим следит `backend/app/log_redaction.py`
и `backend/app/redaction.py`

### 12.2. Команды

`/start`, `/menu`, `/help`, `/status`, `/health`, `/date`, `/imports`,
`/kiz`, `/kiz_files`, `/logistics`, `/logs`, `/manual`, `/cancel`,
`/skladbot_report`, `/skladbot_daily`

`/manual` открывает ручные действия администратора: создать заказ,
удалить активный заказ с подтверждением
(`backend/app/telegram_admin_processor.py`)

### 12.3. Контракт маршрутизации

`backend/app/telegram_routing_manifest.json`, схема версии 1
Пять типов сообщений, у каждого зафиксированы получатель, расписание
и адрес ошибок:

| Тип | Получатель | Расписание | Ошибки |
|---|---|---|---|
`smartup_client_export` | client | 12:00, 15:00, 17:50 | admin
`smartup_logistics_report` | logistics | 17:50 | admin
`skladbot_daily_report` | client | 22:00 | admin
`transfer_kiz_export` | client | по завершении | admin
`admin_error` | admin | по ошибке | admin

Единственная активная автоматическая отправка суточного отчёта это server
Telegram worker в `22:00` по `Asia/Tashkent`. Локального расписания в desktop
нет, `check_daily_reports_async` там явный no-op

Контракт проверяется тестами и верификатором `tools/verify_telegram_routing_contract.py`
Любое изменение маршрутизации или адреса ошибок требует контрактных тестов
и прогона no-send verifier

### 12.4. Защита от отправки лишнего

`backend/app/telegram_output_contract.py` и `spreadsheet_safety.py`
Значения в выгрузках принудительно текстовые, чтобы Excel не превратил марку
в число. Секреты в исходящих сообщениях вырезаются

## 13. Веб-панель

`frontend/src`, 64 файла, 12 198 строк

Две рабочие поверхности:

- `workspace/AdminWorkspace.tsx`, 3323 строки: заказы, импорты, отчёты,
  календарь, точки клиентов, события, инциденты, dry-run заявок, паринг
- `workspace/OperatorWorkspace.tsx` плюс `features/warehouse/WarehousePanel.tsx`:
  экран сборки, сканер, печать листа

### 13.1. Офлайн-очередь сканов

`frontend/src/features/warehouse/offline/`

Два типа события в очереди: `scan` и `order_complete`
(`queueTypes.ts`)

Политика повторов (`errorPolicy.ts`):

- повторяемые статусы клиента: 401, 408, 429
- восстановимые коды браузера: `csrf_invalid`, `origin_denied`
- дубль скана подтверждается как принятый (`scan_duplicate_ack`)
- вердикт замены: `retry`, `blocked`, `synced`

Открытый вопрос, решение за владельцем: событие `order_complete`, отклонённое
как `incomplete`, уходит в заблокированные и в основную очередь не возвращается
Альтернатива подготовлена, но не выпущена, потому что меняет то, что видит
оператор

### 13.2. Печать

`features/warehouse/PrintSheetModal.tsx`
Обычная печать из браузера раньше давала пустой лист, дефект исправлен

## 14. Доступ и роли

### 14.1. Роли веб-сессии

`backend/app/access_policy.py`

| Роль | Права |
|---|---|
`admin` | все одиннадцать прав
`operator` | `warehouse:read/write`, `imports:read/write`, `reports:read`, `logistics:read`
`logistics_slots` | `client_points:read/write`, `logistics:read`
`denied` | ничего

Полный набор прав: `warehouse:read`, `warehouse:write`, `admin:read`,
`admin:write`, `imports:read`, `imports:write`, `reports:read`,
`client_points:read`, `client_points:write`, `logistics:read`,
`diagnostics:read`

Каждый роут описан политикой `RoutePolicy` с типом аутентификации
(`public`, `session`, `protected`), нужным правом, областью сервисного токена
и признаками `mutates` и `sensitive`

Счётчик защищённых роутов зашит в `tests/test_backend_rbac_policy.py`
и `tests/test_postgres_rbac_audit.py`: новый роут требует обновить оба

### 14.2. Вход в панель

`POST /api/v1/auth/login`, `/logout`, `GET /session`, `GET /check`
Логин и хеш пароля в `TAKSKLAD_WEB_LOGIN` и `TAKSKLAD_WEB_PASSWORD_HASH`,
сессия подписывается `TAKSKLAD_WEB_SESSION_SECRET`

Ограничитель попыток (`backend/app/login_limiter.py`):
`TAKSKLAD_WEB_LOGIN_MAX_ATTEMPTS`, `_WINDOW_SECONDS`, `_LOCK_SECONDS`

Защита от подделки запроса в `backend/app/csrf.py`

### 14.3. Сервисные принципалы

Таблицы `service_principals` и `service_principal_tokens`
На 21.08.2026 7 принципалов и 59 токенов

Управление через `tools/manage_service_principals.py`, выдача на сервере через
`deploy/vds/provision_service_principal.sh`
Перекрытие при ротации ограничено
`TAKSKLAD_SERVICE_TOKEN_ROTATION_MAX_OVERLAP_SECONDS`

Пул токенов SkladBot в `SKLADBOT_API_TOKENS`: мёртвый токен ищется отдельной
процедурой, применение `.env` не должно откатывать прод-ревизию

### 14.4. Паринг десктопа

`backend/app/device_pairing_service.py`

| Параметр | Значение |
|---|---|
Код установки | 32 байта, живёт 300 с
Токен до подтверждения | 300 с
Токен после подтверждения | 31 536 000 с
Интервал уборщика | 30 с
Глобальный лимит ожидающих | 100
Лимит на создателя | 5

Ограничители: создание админом 5 за 900 с, по IP 20 за 3600 с,
публичный bootstrap 20 за 3600 с, погашение 10 за 60 с, блокировка 900 с

Роуты: `POST /api/v1/admin/desktop-pairings`,
`POST /api/v1/auth/desktop-pairing/{pairing_id}/ack`,
`POST /api/v1/auth/desktop-bootstrap`,
`POST /api/v1/auth/desktop-pairing/redeem`

## 15. Windows-приложение в текущей роли

`src/taksklad`, 56 файлов, 16 628 строк

Приложение работает **только через backend**, и это зашито константами,
а не флагами (`src/taksklad/config.py:163`):

```
TAKSKLAD_BACKEND_ENABLED = True
TAKSKLAD_BACKEND_READ_ORDERS_ENABLED = True
TAKSKLAD_BACKEND_ONLY_REFRESH = True
TELEGRAM_DESKTOP_POLLING_ENABLED = False
```

Комментарий в коде объясняет почему: старый Google-режим нельзя вернуть флагом
на одном из складских компьютеров

Что в приложении отключено:

- кнопки импорта, каталога и панели контроля прибиты в `None`
  (`src/taksklad/app_layout.py:97`)
- Telegram-опрос выключен константой
- `src/taksklad/skladbot.py`, 639 строк, не импортируется ни одним
  прод-модулем, живёт только в тестах
- значительная часть `src/taksklad/telegram_service.py` без вызывающих

Что в приложении живо: сканирование, отображение заказов, печать, возвраты,
диагностика, автообновление, офлайн-очередь событий в SQLite

Отказ от приложения остаётся заблокированным подтверждёнными пробелами
паритета, разбор в [web-desktop-parity.md](web-desktop-parity.md)

Автообновление: канал `version.json` в корне репозитория,
`src/taksklad/update_service.py`, проверка SHA256 и подтверждение

## 16. Надёжность

### 16.1. Очередь событий

Таблица `pending_events`, сервис `backend/app/event_queue_service.py`

| Группа | Статусы |
|---|---|
Активные | `pending`, `failed`, `processing`
Терминальные | `completed`, `blocked`, `dead`, `cancelled`
Повторяемые | `failed`, `pending`

`STALE_PROCESSING_TIMEOUT = 10 минут`
Повтор вручную разрешён только для типов из `EVENT_QUEUE_RETRYABLE_TYPES`,
это белый список, а не любое событие

Заблокированное событие это осознанное решение бизнес-логики, а не сбой:
уведомление Telegram с битой полезной нагрузкой уходит в `blocked`,
а настоящая ошибка отправки остаётся `failed` и повторяется

Лизы событий: `backend/app/event_leases.py`
Восстановление после остановки воркеров перезапускает все события
в `processing` с непустым владельцем **без проверки срока лиза**,
поэтому делать это при живых воркерах нельзя, будут дубли

Роуты: `GET /api/v1/admin/events`, `GET /api/v1/admin/events/{id}`,
`POST /api/v1/admin/events/{id}/retry`

Полный жизненный цикл: [event-queue-lifecycle.md](event-queue-lifecycle.md)

### 16.2. Инциденты

Таблица `incidents`, сервис `backend/app/incidents_service.py`
Категория лежит в колонке `source`, не `kind`

Падение закрывается инцидентом, а не удалением строк из очереди

Роуты: `GET /api/v1/admin/incidents`, `GET /api/v1/admin/incidents/{id}`,
`POST /api/v1/admin/incidents`, `POST /api/v1/admin/incidents/{id}/status`

На 21.08.2026 открытых 18 плюс 2 в ручном разборе, все `critical`,
самый старый от 06.07

### 16.3. Сердцебиение воркеров

Таблица `worker_heartbeats`. У каждой записи свои `interval_seconds`
и `grace_seconds`

Обязательный набор задаёт `TAKSKLAD_REQUIRED_WORKERS`, по умолчанию три
Лишние строки в таблице readiness игнорирует: на 21.08.2026 там висит
запись `google_sheets_sync` со `status = success` и последним успехом
16.07, и `/ready` её справедливо не считает

### 16.4. Готовность

`GET /ready`, сервис `backend/app/health_service.py`
`EXPECTED_HEAD_REVISION` там же зашивает ожидаемую голову alembic

Обязательные проверки: `database`, `migrations`, `hot_path_queue`, `imports`,
`worker_main_loops`, `daily_report_delivery`, `desktop_pairing_cleanup`
Необязательных сейчас нет

Любое неразобранное `failed`, `error` или `blocked` событие горячего пути
делает готовность нездоровой с кодом 503, даже когда `last_error` пуст

`GET /version` отдаёт версию, `commit_sha`, digest образа,
`server_release_id` и `desktop_api_contract`

### 16.5. Наблюдаемость

`observability_metrics.py`, `observability_context.py`, `worker_observability.py`
Сквозной `correlation_id` в логах, метрики по группам роутов
Диагностика: `GET /api/v1/diagnostics/logs`, `GET /api/v1/admin/metrics`

Порядок разбора: [observability-runbook.md](observability-runbook.md)

## 17. Административные операции над заказом

Все под `/api/v1/admin/orders/`, все требуют `admin:write` и пишут аудит:

| Операция | Что делает |
|---|---|
`{id}/archive-without-kiz` | закрывает без КИЗов
`{id}/cancel` | отменяет
`{id}/delete-active` | удаляет активный заказ, штатный путь удаления
`{id}/reset-rescan` | сбрасывает сканы под пересборку
`{id}/restore` | возвращает из архива
`{id}/resync-skladbot` | пересинхронизирует заявку
`bulk/complete-without-kiz` | массовое закрытие без КИЗов

`delete-active` это единственный правильный способ убрать заказ:
ручная правка базы оставляет осиротевшие движения, после которых КИЗ
блокируется навсегда

## 18. Что выведено из рантайма

Не удалено из истории, потому что следы остались в данных и в старых логах

### 18.1. Google Sheets

Выведен миграцией `20260716_0019_google_runtime_decommission`
Последнее событие `google_sheets_export` в боевой очереди датировано 16.07.2026

Что осталось видимым:

- 52 138 терминальных событий `google_sheets_export` в `pending_events`, 61 MB
- статус заказа `removed_from_google_sheet` в `order_statuses.py`
  и в ограничении таблицы `orders`
- 278 097 записей аудита с `google` в имени действия, 110 MB
- пять ключей `GOOGLE_*` и `TAKSKLAD_GOOGLE_*` в боевом `.env`,
  которые код больше не читает
- разовые инструменты ремонта в `tools/google_cutover_*.py`

В коде `backend/app` живых обращений к Google нет

### 18.2. Локальные очереди приложения

Очереди `pending_saves` и `pending_prints` из десктопного контура больше
не являются частью рабочего пути. В локальном файле `TakSklad_data.json`
они присутствуют и пусты

Живая локальная очередь у приложения одна: события в SQLite
(`pending_backend_events` и `blocked_backend_events`)

## 19. Локальные файлы приложения

Все в `.gitignore`, права `600`, ни один не отслеживается git:

| Файл | Что внутри |
|---|---|
`TakSklad_data.json` плюс три `.last_good.N.bak` | настройки Telegram и SkladBot, история импортов, каталог товаров, состояние обновлений
`TakSklad_queues.sqlite3` | очередь событий приложения
`credentials.json` | наследие Google-контура

`telegram_settings.example.json` отслеживается и содержит только образец

## 20. Безопасность и секреты

- боевые значения только в `<проект>/.access.local.md` и
  `/Users/anton/.codex/LOCAL_SECRETS.md`, метаданные в `ACCESS_INDEX.md`
- секреты не попадают в docs, git, логи, Telegram, экспорты и скриншоты
- зависимости закреплены хешами: 33 пакета backend, 21 пакет desktop,
  установка с `--require-hashes`
- `tools/security_gate.py` на 21.08.2026 даёт ноль блокирующих находок,
  внутри pip-audit, npm audit, поиск секретов, SAST, проверка контейнеров
  и неизменяемых ссылок

## 21. Проверка изменений

| Слой | Команда |
|---|---|
Python | `PYTHONPATH=. ./.venv/bin/python -m unittest discover -s tests`
PostgreSQL | `./tools/run_postgres_tests.sh all`
Фронтенд | `npm run lint` и `npm test -- --run` в `frontend/`
Организация кода | `tools/check_code_organization.py`
Безопасность | `tools/security_gate.py`

Замеры 21.08.2026: 1870 тестов Python, 110 тестов матрицы PostgreSQL,
272 теста фронтенда

93 пропуска в общем прогоне это postgres-набор, он требует контейнера
и закрывается вторым прогоном
4 ожидаемых падения это незакрытые инварианты КИЗов из раздела 7.1

Полный прогон перезаписывает `test-artifacts/disaster-recovery/*.json`,
их надо откатывать перед push

Изменение client-facing вывода (Telegram-отчёты, строки и колонки XLSX,
подписи, имена файлов, веб-интерфейс, роуты, расписания) дополнительно
требует согласования, контрактных тестов и no-send verifier

## 22. Ограничения текущей версии

Известные и признанные, не гипотезы:

1. Четыре инварианта КИЗов не выполняются, раздел 7.1
2. Восстановление в точку времени недостижимо: WAL-архив есть, физического
   базового бэкапа нет, внешней копии нет
3. Очередь событий и журнал аудита растут без ретенции и занимают 82% базы
4. Тип оплаты держится на подстрочном классификаторе свободного текста
5. Порядок движений КИЗа не задан правилом, детерминированный хеш недостижим
6. Дозаказ Telegram-файлом не рождает заявку и молча вливается в отгруженный заказ
7. Заблокированное `order_complete` в офлайн-очереди панели не возвращается
   оператору
8. Отказ от Windows-приложения заблокирован пробелами паритета
9. GitHub Actions не используются, доказательством служат локальные прогоны

## 23. Журнал изменений документа

### 2026-08-21

Документ переписан целиком на действующий контур. Основание: аудит
[taksklad-full-audit-2026-08-21.md](taksklad-full-audit-2026-08-21.md)
показал, что предыдущая версия описывала выведенный из рантайма Google-контур
и desktop-приложение в роли, которой у него больше нет, а Smartup-автоимпорт,
логистика, веб-панель, паринг, RBAC, инциденты и офлайн-очередь
не упоминались вообще

Предыдущая версия описывала функционал `1.1.17` от 26.05.2026
и доступна в истории git
