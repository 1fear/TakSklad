# TakSklad

TakSklad - складская система для WMS-процессов вокруг Excel-заказов, КИЗов, SkladBot, Telegram и отчетов. Рабочий контур состоит из Windows desktop, web-панели, backend API, PostgreSQL и серверных workers.

PostgreSQL через backend API является единственным operational source of truth. Google Sheets выведен из runtime и не используется как хранилище, зеркало или fallback. Для актуальной архитектуры читать [docs/db-only-architecture.md](docs/db-only-architecture.md).

## Что Уже Умеет

- Импортировать Excel-заказы через desktop или Telegram worker.
- Нормализовать заказы и сохранять их в backend/PostgreSQL.
- Группировать заказы для сканирования по клиенту, типу оплаты и адресу.
- Принимать КИЗы через сканер.
- Проверять формат, дубль, SKU и доступность КИЗа перед записью.
- Записывать сканы и движения КИЗов в backend/PostgreSQL.
- Сохранять локальные `pending_backend_events`, scan backup и очередь печати при временном сбое сети или печати.
- Печатать сводные листы напрямую из приложения.
- Создавать локальные backup-файлы для восстановления.
- Формировать Excel-отчеты.
- Отправлять отчеты, логи, backup-файлы и документы по импорту через Telegram.
- Создавать или сопоставлять SkladBot-заявки, где это подтверждено текущим кодом и runbook.
- Поддерживать автообновление собранного Windows-приложения через GitHub.

## Основной Процесс

1. Сотрудник склада импортирует Excel-документ через приложение или отправляет его Telegram-боту.
2. TakSklad нормализует строки и создает заказы/позиции в backend/PostgreSQL.
3. Заказы группируются по клиенту, типу оплаты, адресу и SkladBot-номеру, если он уже найден или создан.
4. Сотрудник сканирует КИЗы по выбранной группе.
5. Приложение записывает сканы в backend/PostgreSQL или сохраняет идемпотентное событие в локальную backend-очередь при сбое связи.
6. Приложение печатает сводные листы или сохраняет их в очередь допечати.
7. Отчеты, логи, backup-файлы и документы по импорту можно получить через Telegram.

## Локальные Файлы

Рядом с приложением используются рабочие и служебные файлы:

- `telegram_settings.json` - локальный токен Telegram-бота и chat IDs. Файл приватный, его нельзя коммитить.
- `telegram_settings.example.json` - безопасный пример настроек без реальных секретов.
- `pending_backend_events.json` - страховочная очередь событий для backend при временной потере сети.
- `pending_prints.json` - очередь сводных листов, которые еще нужно распечатать.
- `pending_telegram.json` - очередь Telegram-сообщений или файлов на отправку.
- `telegram_state.json` - локальное состояние Telegram polling.
- `import_history.json` - локальная история импортированных Excel-документов.
- `product_catalog.json` - локальный справочник товаров и размера блока.
- `TakSklad.log` - лог приложения.
- `scan_backups/` - локальные backup-файлы сканов.
- `reports/` - созданные Excel-отчеты.

Реальные ключи, токены, локальное состояние, логи, backup-файлы и отчеты не должны попадать в Git.

## Telegram

Telegram-интеграция используется как рабочий канал управления через серверный Telegram worker. Конкретное поведение сверять с `docs/db-only-architecture.md`, `docs/report-source-rules.md` и текущим кодом.

Текущие действия бота:

- импортировать отправленный Excel-файл в backend/PostgreSQL;
- скачать сканы за сегодня;
- открыть документы по импорту;
- скачать сегодняшний лог.

Если один и тот же Excel-файл уже был успешно импортирован, повторный импорт через Telegram блокируется по хэшу файла.

Пользователь должен один раз написать боту `/start`, чтобы бот мог отправлять ему файлы и уведомления.

## Daily SkladBot Report

Ежедневный SkladBot отчет собирается read-only из SkladBot requests/detail, warehouse movements и stock endpoints. Primary scope: дата создания заявки, `Дата выгрузки` / `unloading_date` или movement date за дату отчета.

Если заявка создана в дату отчета, она попадает в обычные листы `Заявки` и `Товары заявок` независимо от плановой даты выгрузки. Статусный фильтр остается прежним: в операционные итоги попадают только заявки `Выполнена` + `В архиве`.

Scheduled send допускает только `coverage_status=complete`. Partial/failed/truncation/date-conflict/API-error отчеты не отправляют production Telegram document, не пишут reported registry и не запускают reconciliation. Manual `/skladbot_daily` тоже блокирует partial по умолчанию; explicit admin override `--allow-partial` помечает файл как `НЕПОЛНЫЙ ОТЧЕТ` и не является scheduled recovery.

Лист `Сводка` показывает `Расчетный начальный остаток` как формулу, а не historical opening stock snapshot. Production live state по daily report не считается подтвержденным без отдельной approved runtime проверки.

## Документы По Импорту

Для каждой позиции из импортированного Excel-документа TakSklad должен хранить:

- ID импорта;
- исходный файл;
- строку исходного файла;
- дату импорта.

Telegram-раздел документов по импорту может показывать последние документы с прогрессом, например `davron.xlsx | 20/30`, и отправлять Excel-файл с листами:

- Сводка;
- Позиции;
- КИЗы;
- Недосканировано.

## Автообновление

TakSklad поддерживает автообновление собранного приложения через GitHub.

Важные элементы:

- `APP_VERSION` в `src/taksklad/config.py`;
- `UPDATE_INFO_URL` на raw GitHub `version.json`;
- `version.json` в репозитории.
- код приложения в `src/taksklad/`;
- корневой `main.py` - тонкая точка запуска для локального режима и PyInstaller.

Файл `version.json` должен быть доступен в GitHub, иначе проверка обновлений может получить `404`.

## Разработка

Создать виртуальное окружение и установить зависимости:

```bash
python -m venv .venv
./.venv/bin/pip install -r requirements.txt
```

Запустить приложение в режиме разработки:

```bash
./.venv/bin/python main.py
```

Базовые проверки:

```bash
./.venv/bin/python -m py_compile main.py
./.venv/bin/python -m py_compile src/taksklad/*.py
git diff --check
```

## Направление Проекта

TakSklad должен оставаться практичным и надежным инструментом для склада. Текущее развитие идет вокруг:

- стабильности Windows desktop, backup, recovery, логирования и обновлений;
- backend/PostgreSQL как основного источника данных;
- безопасной интеграции с SkladBot, Smartup и Telegram;
- отчетов, аудита, очередей, rollback и ручной приемки;
- web/admin-контроля заказов, прогресса сканирования, документов и отчетов;
- аналитики процессов, чтобы снижать ручную работу и количество ошибок.

Подробнее начинать с [docs/README.md](docs/README.md) и [docs/db-only-architecture.md](docs/db-only-architecture.md). Старые `taksklad-system-stack-overview.md`, `project-overview.md` и `roadmap.md` оставлены как historical/reference.
