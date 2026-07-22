# TakSklad: access map без секретов

Дата сверки: 2026-07-20.

## Главный маршрут

- `access_id`: `taksklad.production`;
- центральный metadata registry: `/Users/anton/.codex/ACCESS_INDEX.md`;
- реальные значения запрещено копировать в этот репозиторий, knowledge graph,
  отчеты, логи, prompts или chat.

Запись в registry должна показывать актуальный статус покрытия. Сам факт наличия
`access_id` не доказывает, что все значения присутствуют, работают и разрешены для
конкретного действия.

## Логические группы доступа

| Группа | Безопасные логические имена/ссылки | Назначение |
|---|---|---|
| Server/SSH | `VDS_HOST`, `VDS_USER`, `VDS_SSH_KEY`, `VDS_SSH_KNOWN_HOSTS`, `VDS_APP_DIR` | Read-only inventory и отдельно согласованный deploy |
| PostgreSQL | `DATABASE_URL`, `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD` | Backend runtime, migration, backup/restore |
| Backend/service auth | `TAKSKLAD_API_TOKEN`, web login/session secret refs | Desktop, workers, web/admin contracts |
| Telegram | `TELEGRAM_BOT_TOKEN`, allow/admin routing refs | Import, reports, notifications |
| SkladBot | `SKLADBOT_API_TOKEN` или token-set ref | Requests, returns, read-only reports |
| Smartup | `SMARTUP_BASE_URL`, project/filial/user/password refs | Read-only export и separately gated write/status flows |
| Geocoder | `YANDEX_GEOCODER_API_KEY` | Address normalization |
| GitHub/release | repository auth, environment/SSH refs, Windows signing refs | CI, immutable release, controlled deploy |

Имена переменных — интерфейс конфигурации, не место хранения значений. Не все поля
обязательны для каждой операции.

## Как агент получает доступ

1. Сначала читает metadata записи `taksklad.production` в `ACCESS_INDEX.md`.
2. Выбирает только минимальную группу доступа для текущей задачи.
3. Проверяет разрешенный scope, дату/статус и approval boundary.
4. Значение использует только в памяти процесса или штатном secret mechanism;
   не печатает его и не передает субагентам.
5. Начинает с read-only проверки. Production write выполняет только после
   отдельного явного разрешения Антона.

## Запрещено без отдельного approval

- deploy, restart, migration, restore и worker replay;
- DB mutation, bulk update и cleanup;
- Smartup/SkladBot/Telegram write или внешняя отправка;
- ротация credentials и изменение GitHub Environment/Secrets;
- чтение всех секретов «для ориентации»;
- перенос значения в `SERVER.md`, `CURRENT_STATUS.md`, graph или task tracker.

Server topology и текущая readiness: [SERVER.md](SERVER.md) и
[CURRENT_STATUS.md](CURRENT_STATUS.md).
