# TakSklad Acceptance Kit

Назначение: ручная проверка Telegram import и Windows desktop acceptance без релиза, без изменения `version.json` и без push-уведомлений рабочим ПК.

## Состав

- `TakSklad_Telegram_Acceptance_2026-05-31.xlsx` - Excel для отправки в Telegram-бот.
- `acceptance_manifest.json` - контрольные значения, checksum и команды проверки.
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
- SHA-256 Excel: `4e7bc8540e45e9ce7c3465e138c063aa4168362e25f3c29c626e7c8ba9de8b4c`

## Telegram Проверка

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

## Windows Проверка

Проверить связь с VDS:

```powershell
.\tools\windows_backend_acceptance.ps1 -CheckOnly -Token "<service-token>"
```

Запустить тестовую копию:

```powershell
.\tools\windows_backend_acceptance.ps1 -Token "<service-token>" -AppPath ".\TakSklad.exe"
```

Если запуск из исходников:

```powershell
.\tools\windows_backend_acceptance.ps1 -Token "<service-token>" -AppPath ".\main.py"
```

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

- Не менять `version.json`.
- Не создавать Windows release archive.
- Не создавать GitHub Release.
- Не отправлять push-уведомления.
- Не создавать реальную заявку SkladBot без отдельного подтверждения.
