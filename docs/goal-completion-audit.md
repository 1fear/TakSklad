# TakSklad Goal Completion Audit

Дата аудита: 2026-05-31.

Цель документа - честно сверить исходную задачу с текущим состоянием проекта. Это не релизный чеклист и не разрешение выкатывать обновление на рабочие Windows-компьютеры.

## Исходная Задача

Антон попросил:

1. Сделать точку восстановления как в Windows.
2. Сделать локальную и, если возможно, серверную точку восстановления.
3. Приступить ко всем обновлениям и довести их до конца.
4. В конце дать отчёт: что сделано, что получилось, какие ошибки, какие тесты проведены.
5. Отправить шаблон в Telegram-бот, чтобы бот подтянул дату и импортировал файл.
6. Проверить SkladBot-сопоставление.
7. По возможности создать заявку SkladBot и прогнать весь путь.
8. В приложении всё отпикать.

## Сводный Статус

| Требование | Статус | Доказательство |
|---|---|---|
| Локальная точка восстановления до изменений | Готово | `restore-2026-05-31_before_mvp_updates_003050`, branch/tag/local snapshot |
| Серверная точка восстановления | Готово | `/opt/taksklad/restore_points/server_20260530T194938Z`, Postgres backup `taksklad-postgres-20260530T194941Z.sql.gz` |
| Checkpoint после MVP-доработок | Готово | tag `checkpoint-2026-05-31_mvp-telegram-logistics-skladbot`, branch `feature/mvp-telegram-logistics-skladbot` |
| GitHub remote checkpoint | Готово | branch и tags отправлены в GitHub, draft PR `https://github.com/1fear/TakSklad/pull/1` |
| Telegram import логика | Готово технически | Telegram worker, очередь, дата отгрузки, file smoke через реальный `file_id` |
| Telegram update isolation | Готово технически | Ошибка одного update не блокирует следующие Excel-файлы; `tests.test_backend_telegram_import` |
| Telegram входящее сообщение от реального пользователя | Не доказано | Bot API не может создать входящее пользовательское сообщение самому себе |
| Логистический отчёт по дате с координатами | Готово | backend endpoint, unit tests, VDS smoke |
| Очистка страны из адреса при импорте | Готово | Backend importer удаляет `Узбекистан`/`Uzbekistan`/`O'zbekiston`; `tests.test_backend_telegram_import` |
| Геокодирование адреса при Telegram import | Готово технически | Если координат нет, backend importer использует `YANDEX_GEOCODER_API_KEY`; compose пробрасывает env; тесты закрывают fallback |
| КИЗ по исходным файлам | Готово | backend endpoints, tests, VDS smoke |
| SkladBot matching | Готово технически | real-match smoke на существующей заявке `WH-R-190960`, matched=1 |
| Создание новой заявки в SkladBot | Не делалось намеренно | Чтобы не менять WMS/остатки и не создать боевую мусорную заявку |
| Отпикивание в приложении | Частично доказано | Web-frontend VDS smoke: 3 КИЗа, 2 позиции, заказ completed |
| Windows desktop отпикивание | Не доказано | Нужна физическая Windows-приёмка |
| Тесты проекта | Готово | 122 unit tests OK, py_compile OK, frontend build OK, compose config OK, release preflight OK |
| Desktop UI 2.0 contract | Готово технически | `tests/test_desktop_ui_contract.py`: складской экран 2.0, отсутствие legacy-кнопок, палитра и округлённые кнопки |
| Защита Windows-приёмки от старого exe | Готово технически | `windows_backend_acceptance.ps1` требует `build_manifest.json` для `.exe` и проверяет `app_version` не ниже `1.1.17`; строгая проверка доступна через `-ExpectedAppVersion` |
| Release preflight acceptance flow | Готово технически | `tools/release_preflight.py` проверяет Windows helper, test archive builder, acceptance kit и pinned `version.json` |
| VDS acceptance status rollout guard | Готово технически | `deploy/vds/acceptance_status.sh` проверяет manifest template, pinned `version.json`, download URL и safety-флаги |
| VDS acceptance kit sync | Готово | На VDS загружены актуальные acceptance scripts/kit, `acceptance_status.sh` вернул `status=ok` без изменения БД/контейнеров |
| Отчёт о работе | Готово | `отчеты/2026-05-31.md`, `docs/implementation-log.md`, PR body/comments |
| Безопасность релиза | Готово | `version.json` не менялся, Windows release не создавался, push-уведомления не отправлялись |
| Read-only acceptance verifier | Готово | `deploy/vds/verify_acceptance_marker.sh`, проверен на пустом acceptance-маркере и smoke-маркере |
| Wait acceptance verifier | Готово | `deploy/vds/wait_acceptance_marker.sh`, syntax check OK, включён в acceptance kit |
| Acceptance status check | Готово | `deploy/vds/acceptance_status.sh`, VDS status вернул `ok` |
| Acceptance Excel generator | Готово | `tools/generate_acceptance_excel.py`, тест `tests/test_acceptance_excel_generator.py` |
| Acceptance kit для ручной проверки | Готово | `outputs/taksklad_acceptance/README.md`, `acceptance_manifest.json`, стабильный SHA-256 Excel |
| Acceptance kit на VDS | Готово | файлы загружены в `/opt/taksklad/app`, VDS verifier/wait/help/safety проверены |

## Что Доказано

### Recovery

До MVP-доработок создана точка восстановления:

- Git branch: `restore/2026-05-31_before_mvp_updates_003050`;
- Git tag: `restore-2026-05-31_before_mvp_updates_003050`;
- local snapshot: `/Users/anton/Documents/work/_restore_points/TakSklad_2026-05-31_before_mvp_updates_003050`.

После MVP-доработок создан checkpoint:

- Git branch: `feature/mvp-telegram-logistics-skladbot`;
- Git tag: `checkpoint-2026-05-31_mvp-telegram-logistics-skladbot`;
- draft PR: `https://github.com/1fear/TakSklad/pull/1`.

На VDS есть server restore и Postgres backup:

- `/opt/taksklad/restore_points/server_20260530T194938Z`;
- `/opt/taksklad/backups/postgres/taksklad-postgres-20260530T194941Z.sql.gz`.

### Backend И Workers

Реализовано:

- Excel import через backend;
- очередь Telegram-файлов;
- изоляция Telegram updates, чтобы ошибка одной кнопки/отчёта не блокировала следующие Excel-файлы в пачке;
- дата отгрузки от менеджера;
- логистический отчёт по выбранной дате;
- координаты в логистическом отчёте;
- очистка страны из адреса при backend Excel import перед логистикой;
- получение координат по адресу через Яндекс Геокодер при backend Telegram import, если координат нет в Excel;
- КИЗ-отчёт по завершённым исходным файлам;
- SkladBot matching по `3PL отгрузка`, дате выгрузки, клиенту, оплате, нормализованному товару и блокам;
- защита SkladBot worker от лишних API-вызовов без активных заказов;
- обработка `429 Too Many Requests`;
- web-frontend draft на VDS.

### Проверки

Автоматические и smoke-проверки:

- `.venv/bin/python -m unittest discover -s tests` - 122 теста OK;
- `.venv/bin/python -m py_compile backend/app/*.py tests/*.py` - OK;
- `git diff --check` - OK;
- `.venv/bin/python tools/release_preflight.py` - OK, публичный backend health отвечает `status=ok`;
- `npm run build` в `frontend/` - OK;
- `docker compose --env-file deploy/vds/.env.example -f deploy/vds/docker-compose.yml config` - OK;
- VDS `backend-api`, `frontend`, `postgres`, `telegram-worker`, `skladbot-worker` работают;
- `https://api.135.181.245.84.sslip.io/health` вернул `200`;
- frontend без basic-auth вернул `401`;
- Telegram file smoke через реальный `file_id` - OK;
- SkladBot real-match smoke на существующей заявке `WH-R-190960` - OK;
- web-frontend smoke: заказ найден, 3 КИЗа записаны, заказ завершён, smoke-данные очищены.
- desktop UI contract: главный экран 2.0 закреплён как складской экран без legacy-кнопок `Импорт Excel`, `Товары`, `Контроль`.
- повторяемый `deploy/vds/smoke_mvp_chapman.sh` - OK;
- `deploy/vds/verify_acceptance_marker.sh "ACCEPTANCE TELEGRAM 20260531"` - OK, текущий маркер пустой;
- `verify_acceptance_marker.sh` на smoke-маркере до cleanup - OK: `orders=1`, `planned_blocks=3`, `scan_codes=3`, `completed_orders=1`.
- `tools/generate_acceptance_excel.py` пересобрал acceptance Excel, backend parser прочитал `2` строки, `3` блока, сумму `720000`, warnings `[]`.
- `tools/prepare_acceptance_kit.py` пересобрал acceptance kit, manifest и README; SHA-256 Excel стабилен между повторными генерациями.
- `deploy/vds/wait_acceptance_marker.sh` добавлен для ожидания ручного Telegram/Windows результата без изменений в базе.
- `deploy/vds/acceptance_status.sh` добавлен для единой read-only проверки manifest, SHA, `version.json`, Docker services, backend health и acceptance marker.
- Acceptance kit, wait-verifier и status-check загружены на VDS; `acceptance_status.sh` вернул `status=ok`; `version.json` на VDS остался на `1.1.7`, контейнеры и БД не менялись.
- Windows acceptance helper теперь не запускает старый `TakSklad.exe` без `build_manifest.json` из test archive, чтобы приёмка не прошла случайно на версии `1.1.7`; будущая `2.0.0` проходит по минимальной версии `1.1.17`.
- Release preflight теперь проверяет Windows acceptance flow, а acceptance Excel генерируется стабильно byte-for-byte. Текущий SHA-256 acceptance Excel: `204b932a704b39294b513a95964844db1ed74d028e3daff13beef3ab09ec98fd`.
- VDS acceptance status теперь тоже останавливается при rollout-состоянии `version.json` или повреждённом acceptance manifest.
- Актуальный acceptance kit и усиленные read-only scripts синхронизированы на VDS. `acceptance_status.sh` на сервере вернул `status=ok`; marker пустой, контейнеры running, `version.json=1.1.7`. После перехода Windows helper на минимальную версию acceptance kit повторно пересобран и синхронизирован; локально `122` теста OK, `release_preflight.py` OK.
- После backend-геокодинга актуальные `backend/`, `deploy/vds/` и acceptance files доставлены на VDS. Пересобраны только `backend-api`, `telegram-worker`, `skladbot-worker`; Postgres data, frontend, `version.json`, GitHub Release и Windows archive не трогались. `acceptance_status.sh` снова вернул `status=ok`, публичный backend health вернул `200`, локально `126` тестов OK и `release_preflight.py` OK. `YANDEX_GEOCODER_API_KEY` на VDS пока пустой, поэтому реальный геокодинг через Яндекс включится после добавления ключа.
- Telegram hidden admin commands усилены: `setMyCommands` содержит только `/date`, `/logistics`, `/kiz_files`; `/health`, `/imports`, `/logs` остаются скрытыми fallback-командами и при заданном `TELEGRAM_ADMIN_CHAT_IDS` доступны только указанным chat_id. На VDS пересобраны `telegram-worker` и `backend-api`; `acceptance_status.sh` вернул `status=ok`, `version.json` остался `1.1.7`.
- VDS runtime-настройки усилены: `TELEGRAM_ADMIN_CHAT_IDS` теперь задан, `SKLADBOT_WORKER_INTERVAL_SECONDS=60`, clean compose config может использовать `.env.example` через `TAKSKLAD_ENV_FILE` без подмешивания локального `.env`. `backend-api`, `skladbot-worker`, `telegram-worker` running; `acceptance_status.sh` вернул `status=ok`; `version.json` остался `1.1.7`.
- Добавлен машинный `GO/NO-GO` gate `tools/release_go_no_go.py`: релиз 2.0 нельзя считать готовым, пока в `ACCEPTANCE_RESULTS.md` не отмечены Telegram import, SkladBot matching, Windows acceptance, отсутствие критичных дефектов, понятный rollback и неизменённый `version.json`. Незакрытый критичный дефект переводит gate в `no_go`.
- Desktop UX последней позиции исправлен: после полного скана последней позиции склад видит `ЗАВЕРШИТЬ ЗАКАЗ`, а не лишний переход через `Следующая позиция`; печать остаётся только после завершения заказа.
- SkladBot safe partial-match доставлен на VDS точечно: синхронизирован `backend/app/skladbot_worker.py`, пересобран только `skladbot-worker`; `acceptance_status.sh` вернул `status=ok`, marker пустой, `version.json=1.1.7`.
- Backend scan API усилен для параллельной работы двух ПК: повтор того же КИЗа в той же позиции идемпотентен, но дубль в другой позиции возвращает явный конфликт и остаётся в desktop backend queue до разбирательства.
- GO/NO-GO gate теперь сверяет `ACCEPTANCE_RESULTS.md` с `ACCEPTANCE_RESULTS_TEMPLATE.md`, поэтому обязательные пункты ручной приёмки нельзя обойти удалением строк из файла результата.
- SkladBot matching стал строже по типу заявки: возвратные типы не проходят даже при наличии `3PL`/`отгрузка`; адрес вынесен в мягкую диагностику `address_soft_match`.

## Что Не Доказано

### Входящий Telegram-Файл От Пользователя

Проверен реальный Telegram file API: файл был загружен в Telegram, worker скачал его по реальному `file_id` и импортировал.

Не доказан именно сценарий "живой пользователь отправил Excel в чат", потому что бот через Bot API не может сам создать себе входящее пользовательское сообщение. Для закрытия нужен ручной шаг: отправить Excel в Telegram-бот с пользовательского аккаунта.

Для этой проверки подготовлен файл:

`/Users/anton/Documents/work/TakSklad/outputs/taksklad_acceptance/TakSklad_Telegram_Acceptance_2026-05-31.xlsx`

Вся ручная проверка собрана в acceptance kit:

- `/Users/anton/Documents/work/TakSklad/outputs/taksklad_acceptance/README.md`;
- `/Users/anton/Documents/work/TakSklad/outputs/taksklad_acceptance/acceptance_manifest.json`.

Файл можно пересобрать командой:

```bash
cd /Users/anton/Documents/work/TakSklad
.venv/bin/python tools/prepare_acceptance_kit.py
```

Runbook: `docs/manual-acceptance-runbook.md`.

Проверка результата на VDS:

```bash
cd /opt/taksklad/app
./deploy/vds/verify_acceptance_marker.sh "ACCEPTANCE TELEGRAM 20260531" --expect-orders 1
```

### Windows Desktop UI

Проверен web-frontend на VDS. Это доказывает backend/UI-путь VDS, но не доказывает desktop-приложение Windows.

Для закрытия нужен реальный Windows-smoke:

1. Запуск desktop.
2. Обновление списка.
3. Выбор заказа.
4. Сканирование КИЗов.
5. Завершение заказа.
6. Печать.
7. Завершение смены.
8. Проверка сценария "обновление списка во время сканирования".

Чеклист: `docs/windows-backend-acceptance.md`.

Короткий ручной сценарий также вынесен в `docs/manual-acceptance-runbook.md`.

Проверка результата на VDS после Windows-сканов:

```bash
cd /opt/taksklad/app
./deploy/vds/verify_acceptance_marker.sh "ACCEPTANCE TELEGRAM 20260531" --expect-orders 1 --expect-scans 3 --expect-completed
```

### Создание Новой Заявки SkladBot

Новая заявка в SkladBot не создавалась специально. Причина: SkladBot является живой WMS, создание тестовой заявки может повлиять на реальные процессы, мусор в архиве или остатки. Вместо этого использована существующая заявка `WH-R-190960`, на которой проверено сопоставление номера.

## Итог

Цель существенно продвинута и техническая VDS/MVP-часть проверена. Но полное закрытие цели пока нельзя честно поставить, потому что остаются два внешних ручных пункта:

1. Входящий Excel в Telegram от пользовательского аккаунта.
2. Физическая Windows desktop-приёмка.

До этих проверок PR должен оставаться draft, `version.json` не менять, Windows archive не собирать и push-уведомления рабочим ПК не отправлять.
