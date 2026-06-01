# TakSklad Acceptance Kit

Назначение: ручная проверка Telegram import и Windows desktop acceptance после публикации 2.0.0 manifest. Обновления через `version.json` разрешены, но принудительное обновление `mandatory=true` не включается до ручного GO.

## Состав

- `TakSklad_Telegram_Acceptance_2026-05-31.xlsx` - Excel для отправки в Telegram-бот.
- `acceptance_manifest.json` - контрольные значения, checksum и команды проверки.
- `ACCEPTANCE_RESULTS.md` - фактический статус приёмки; обновлять по результатам проверок.
- `ACCEPTANCE_RESULTS_TEMPLATE.md` - шаблон фиксации результата ручной приёмки.
- `README.md` - короткая инструкция.

## Контрольные Значения

- Маркер: `ACCEPTANCE TELEGRAM 20260531`
- Дата отгрузки: `31.05.2026`
- Заказов: `1`
- Строк Excel: `2`
- Позиций: `2`
- План блоков: `3`
- Сумма: `720000`
- Координаты: `41.311081, 69.240562`
- SHA-256 Excel: `204b932a704b39294b513a95964844db1ed74d028e3daff13beef3ab09ec98fd`

## Telegram Проверка

Перед ручными проверками локально запустить preflight:

```bash
cd /Users/anton/Documents/work/TakSklad
.venv/bin/python tools/release_preflight.py
```

Он проверяет публичный backend health, `version.json`, acceptance kit и отсутствие tracked runtime/secret-файлов.

Перед ручной проверкой можно посмотреть общий VDS status:

```bash
cd /opt/taksklad/app
./deploy/vds/acceptance_status.sh
```

Обычный `acceptance_status.sh` проверяет здоровье VDS, Telegram menu, Google Sheets ↔ backend sync, покрытие SkladBot-номерами и показывает блок `release_go_no_go`.
До ручной приёмки в нём должен быть `status=no_go`.
Отдельно проверить Google Sheets ↔ backend sync можно так:

```bash
cd /opt/taksklad/app
./deploy/vds/verify_google_backend_sync.sh
```

Отдельно проверить покрытие активных заказов номерами SkladBot можно так:

```bash
cd /opt/taksklad/app
./deploy/vds/verify_skladbot_coverage.sh
```

Для релизного gate использовать строгий режим:

```bash
cd /opt/taksklad/app
./deploy/vds/acceptance_status.sh --require-go
```

Он должен падать до тех пор, пока `ACCEPTANCE_RESULTS.md` не заполнен как `GO`.

1. В Telegram открыть `SkladKis_bot` от разрешённого пользовательского аккаунта.
2. Нажать `Дата отгрузки`.
3. Отправить `31.05.2026`.
4. Отправить `TakSklad_Telegram_Acceptance_2026-05-31.xlsx` как документ.
5. После ответа бота проверить VDS:

```bash
cd /opt/taksklad/app
./deploy/vds/verify_acceptance_marker.sh "ACCEPTANCE TELEGRAM 20260531" --expect-orders 1
```

Или дождаться результата автоматически:

```bash
cd /opt/taksklad/app
./deploy/vds/wait_acceptance_marker.sh "ACCEPTANCE TELEGRAM 20260531" --expect-orders 1 --timeout 300 --interval 10
```

Проверить общий статус VDS:

```bash
cd /opt/taksklad/app
./deploy/vds/acceptance_status.sh --expect-orders 1
```

## Windows Проверка

На Windows собрать свежий test archive:

```powershell
.\tools\build_windows_test_archive.ps1 -InstallDependencies
```

Распаковать архив из `outputs\windows_test_build`. Следующие PowerShell-команды выполнять уже из корня распакованного test archive.

Проверить связь с VDS:

```powershell
.\tools\windows_backend_acceptance.ps1 -CheckOnly -Token "<service-token>"
```

Запустить тестовую копию:

```powershell
.\tools\windows_backend_acceptance.ps1 -Token "<service-token>" -AppPath ".\TakSklad\TakSklad.exe"
```

Если запуск из исходников:

```powershell
.\tools\windows_backend_acceptance.ps1 -Token "<service-token>" -AppPath ".\main.py"
```

Если в папке рядом есть exe, но нужно принудительно запустить исходники:

```powershell
.\tools\windows_backend_acceptance.ps1 -Token "<service-token>" -UsePython
```

Helper использует `https://api.taksklad.uz`, проверяет, что `APP_VERSION` не ниже `2.0.0` и `APP_BUILD_LABEL = MVP 2.0`, и предпочитает `.venv\Scripts\python.exe`. Для exe helper требует `build_manifest.json` из свежего test archive и сверяет `app_version` + `app_build_label`; старый ярлык `1.1.7` без manifest будет остановлен до запуска.

Сканировать тестовые КИЗы:

- `WIN-KIZ-ACCEPT-001`
- `WIN-KIZ-ACCEPT-002`
- `WIN-KIZ-ACCEPT-003`

После завершения заказа проверить VDS:

```bash
cd /opt/taksklad/app
./deploy/vds/verify_acceptance_marker.sh "ACCEPTANCE TELEGRAM 20260531" --expect-orders 1 --expect-scans 3 --expect-completed
```

Или дождаться результата автоматически:

```bash
cd /opt/taksklad/app
./deploy/vds/wait_acceptance_marker.sh "ACCEPTANCE TELEGRAM 20260531" --expect-orders 1 --expect-scans 3 --expect-completed --timeout 300 --interval 10
```

Проверить общий статус VDS:

```bash
cd /opt/taksklad/app
./deploy/vds/acceptance_status.sh --expect-orders 1 --expect-scans 3 --expect-completed
```

## Очистка Тестовых Данных

Dry-run:

```bash
cd /opt/taksklad/app
./deploy/vds/cleanup_acceptance_marker.sh "ACCEPTANCE TELEGRAM 20260531"
```

Удаление:

```bash
cd /opt/taksklad/app
./deploy/vds/cleanup_acceptance_marker.sh "ACCEPTANCE TELEGRAM 20260531" --apply
```

## Чего Не Делать

- Не включать `mandatory=true` до ручного GO.
- Не публиковать новый Windows release поверх 2.0.0 без повторной проверки.
- Не создавать реальную заявку SkladBot без отдельного подтверждения.
