#!/usr/bin/env python3
import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.generate_acceptance_excel import DEFAULT_MARKER, DEFAULT_SHIPMENT_DATE, save_acceptance_excel


DEFAULT_OUTPUT_DIR = Path("outputs/taksklad_acceptance")
EXCEL_NAME = "TakSklad_Telegram_Acceptance_2026-05-31.xlsx"
MANIFEST_NAME = "acceptance_manifest.json"
README_NAME = "README.md"
RESULT_TEMPLATE_NAME = "ACCEPTANCE_RESULTS_TEMPLATE.md"
RESULT_FILE_NAME = "ACCEPTANCE_RESULTS.md"
TEST_KIZ_CODES = [
    "WIN-KIZ-ACCEPT-001",
    "WIN-KIZ-ACCEPT-002",
    "WIN-KIZ-ACCEPT-003",
]


def current_app_version():
    config_path = PROJECT_ROOT / "src" / "taksklad" / "config.py"
    text = config_path.read_text(encoding="utf-8")
    match = re.search(r'APP_VERSION\s*=\s*"([^"]+)"', text)
    if not match:
        raise RuntimeError(f"APP_VERSION not found in {config_path}")
    return match.group(1)


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_manifest(output_dir=DEFAULT_OUTPUT_DIR, marker=DEFAULT_MARKER, shipment_date=DEFAULT_SHIPMENT_DATE):
    output_dir = Path(output_dir)
    excel_path = output_dir / EXCEL_NAME
    return {
        "kit": "taksklad_acceptance",
        "marker": marker,
        "shipment_date": shipment_date,
        "excel_file": EXCEL_NAME,
        "result_template": RESULT_TEMPLATE_NAME,
        "result_file": RESULT_FILE_NAME,
        "app_version": current_app_version(),
        "excel_sha256": sha256_file(excel_path),
        "excel_bytes": excel_path.stat().st_size,
        "expected": {
            "orders": 1,
            "rows": 2,
            "items": 2,
            "planned_blocks": 3,
            "scan_codes": 3,
            "total_sum": 720000,
            "coordinates": ["41.311081, 69.240562"],
        },
        "test_kiz_codes": TEST_KIZ_CODES,
        "commands": {
            "regenerate": ".venv/bin/python tools/prepare_acceptance_kit.py",
            "local_preflight": ".venv/bin/python tools/release_preflight.py",
            "vds_status": './deploy/vds/acceptance_status.sh',
            "skladbot_coverage": './deploy/vds/verify_skladbot_coverage.sh',
            "telegram_verify": './deploy/vds/verify_acceptance_marker.sh "ACCEPTANCE TELEGRAM 20260531" --expect-orders 1',
            "telegram_wait": './deploy/vds/wait_acceptance_marker.sh "ACCEPTANCE TELEGRAM 20260531" --expect-orders 1 --timeout 300 --interval 10',
            "telegram_status": './deploy/vds/acceptance_status.sh --expect-orders 1',
            "windows_build_test_archive": '.\\tools\\build_windows_test_archive.ps1 -InstallDependencies',
            "windows_check_only": '.\\tools\\windows_backend_acceptance.ps1 -CheckOnly -Token "<service-token>"',
            "windows_launch_exe": '.\\tools\\windows_backend_acceptance.ps1 -Token "<service-token>" -AppPath ".\\TakSklad\\TakSklad.exe"',
            "windows_launch_source": '.\\tools\\windows_backend_acceptance.ps1 -Token "<service-token>" -AppPath ".\\main.py"',
            "windows_launch_source_auto": '.\\tools\\windows_backend_acceptance.ps1 -Token "<service-token>" -UsePython',
            "windows_verify": './deploy/vds/verify_acceptance_marker.sh "ACCEPTANCE TELEGRAM 20260531" --expect-orders 1 --expect-scans 3 --expect-completed',
            "windows_wait": './deploy/vds/wait_acceptance_marker.sh "ACCEPTANCE TELEGRAM 20260531" --expect-orders 1 --expect-scans 3 --expect-completed --timeout 300 --interval 10',
            "windows_status": './deploy/vds/acceptance_status.sh --expect-orders 1 --expect-scans 3 --expect-completed',
            "cleanup_dry_run": './deploy/vds/cleanup_acceptance_marker.sh "ACCEPTANCE TELEGRAM 20260531"',
            "cleanup_apply": './deploy/vds/cleanup_acceptance_marker.sh "ACCEPTANCE TELEGRAM 20260531" --apply',
        },
        "safety": {
            "version_json_staged_rollout": True,
            "github_release_published": True,
            "push_notifications_allowed": True,
            "mandatory_update_enabled": True,
            "no_real_skladbot_request_creation": True,
            "contains_secrets": False,
        },
    }


def build_readme(manifest):
    kiz_codes = "\n".join(f"- `{code}`" for code in manifest["test_kiz_codes"])
    app_version = manifest["app_version"]
    return f"""# TakSklad Acceptance Kit

Назначение: ручная проверка Telegram import и Windows desktop acceptance после публикации {app_version} manifest. Обновления через `version.json` разрешены; текущая линия {app_version} переведена в forced rollout.

## Состав

- `{manifest["excel_file"]}` - Excel для отправки в Telegram-бот.
- `{MANIFEST_NAME}` - контрольные значения, checksum и команды проверки.
- `{RESULT_FILE_NAME}` - фактический статус приёмки; обновлять по результатам проверок.
- `{RESULT_TEMPLATE_NAME}` - шаблон фиксации результата ручной приёмки.
- `{README_NAME}` - короткая инструкция.

## Контрольные Значения

- Маркер: `{manifest["marker"]}`
- Дата отгрузки: `{manifest["shipment_date"]}`
- Заказов: `{manifest["expected"]["orders"]}`
- Строк Excel: `{manifest["expected"]["rows"]}`
- Позиций: `{manifest["expected"]["items"]}`
- План блоков: `{manifest["expected"]["planned_blocks"]}`
- Сумма: `{manifest["expected"]["total_sum"]}`
- Координаты: `{", ".join(manifest["expected"]["coordinates"])}`
- SHA-256 Excel: `{manifest["excel_sha256"]}`

## Telegram Проверка

Перед ручными проверками локально запустить preflight:

```bash
cd /Users/anton/Documents/work/TakSklad
{manifest["commands"]["local_preflight"]}
```

Он проверяет публичный backend health, `version.json`, acceptance kit и отсутствие tracked runtime/secret-файлов.

Перед ручной проверкой можно посмотреть общий VDS status:

```bash
cd /opt/taksklad/app
{manifest["commands"]["vds_status"]}
```

Обычный `acceptance_status.sh` проверяет здоровье VDS, Telegram menu, PostgreSQL workers, покрытие SkladBot-номерами и показывает блок `release_go_no_go`.
До ручной приёмки в нём должен быть `status=no_go`.

Отдельно проверить покрытие активных заказов номерами SkladBot можно так:

```bash
cd /opt/taksklad/app
{manifest["commands"]["skladbot_coverage"]}
```

Для релизного gate использовать строгий режим:

```bash
cd /opt/taksklad/app
./deploy/vds/acceptance_status.sh --require-go
```

Он должен падать до тех пор, пока `ACCEPTANCE_RESULTS.md` не заполнен как `GO`.

1. В Telegram открыть `SkladKis_bot` от разрешённого пользовательского аккаунта.
2. Нажать `Дата отгрузки`.
3. Отправить `{manifest["shipment_date"]}`.
4. Отправить `{manifest["excel_file"]}` как документ.
5. После ответа бота проверить VDS:

```bash
cd /opt/taksklad/app
{manifest["commands"]["telegram_verify"]}
```

Или дождаться результата автоматически:

```bash
cd /opt/taksklad/app
{manifest["commands"]["telegram_wait"]}
```

Проверить общий статус VDS:

```bash
cd /opt/taksklad/app
{manifest["commands"]["telegram_status"]}
```

## Windows Проверка

На Windows собрать свежий test archive:

```powershell
{manifest["commands"]["windows_build_test_archive"]}
```

Распаковать архив из `outputs\\windows_test_build`. Следующие PowerShell-команды выполнять уже из корня распакованного test archive.

Проверить связь с VDS:

```powershell
{manifest["commands"]["windows_check_only"]}
```

Запустить тестовую копию:

```powershell
{manifest["commands"]["windows_launch_exe"]}
```

Если запуск из исходников:

```powershell
{manifest["commands"]["windows_launch_source"]}
```

Если в папке рядом есть exe, но нужно принудительно запустить исходники:

```powershell
{manifest["commands"]["windows_launch_source_auto"]}
```

Helper использует `https://api.taksklad.uz`, проверяет, что `APP_VERSION` не ниже `2.0.0` и `APP_BUILD_LABEL = MVP 2.0`, и предпочитает `.venv\\Scripts\\python.exe`. Для exe helper требует `build_manifest.json` из свежего test archive и сверяет `app_version` + `app_build_label`; старый ярлык `1.1.7` без manifest будет остановлен до запуска.

Сканировать тестовые КИЗы:

{kiz_codes}

После завершения заказа проверить VDS:

```bash
cd /opt/taksklad/app
{manifest["commands"]["windows_verify"]}
```

Или дождаться результата автоматически:

```bash
cd /opt/taksklad/app
{manifest["commands"]["windows_wait"]}
```

Проверить общий статус VDS:

```bash
cd /opt/taksklad/app
{manifest["commands"]["windows_status"]}
```

## Очистка Тестовых Данных

Dry-run:

```bash
cd /opt/taksklad/app
{manifest["commands"]["cleanup_dry_run"]}
```

Удаление:

```bash
cd /opt/taksklad/app
{manifest["commands"]["cleanup_apply"]}
```

## Чего Не Делать

- Не откатывать `mandatory=true` без отдельного решения Антона.
- Не публиковать новый Windows release поверх {app_version} без повторной проверки.
- Не создавать реальную заявку SkladBot без отдельного подтверждения.
"""


def build_initial_result_file(manifest):
    kiz_codes = "\n".join(f"- [ ] `{code}`" for code in manifest["test_kiz_codes"])
    app_version = manifest["app_version"]
    return f"""# TakSklad 2.0 Acceptance Results

Дата проверки:

Проверяющий:

Среда:

- VDS: `https://api.taksklad.uz`
- Desktop source/build:
- Windows ПК:
- Сканер:
- Принтер:

Маркер проверки: `{manifest["marker"]}`

Файл Telegram import: `{manifest["excel_file"]}`

SHA-256 Excel: `{manifest["excel_sha256"]}`

## 1. Preflight

- [ ] `.venv/bin/python tools/release_preflight.py` вернул `status=ok`.
- [ ] `version.json` указывает на `{app_version}`, `mandatory=true`, ссылки и SHA заполнены.
- [ ] В Git нет tracked runtime/secret-файлов.

## 2. Telegram Import

- [ ] В Telegram нажата кнопка `Дата отгрузки`.
- [ ] Отправлена дата `{manifest["shipment_date"]}`.
- [ ] Отправлен Excel-файл как документ.
- [ ] Бот ответил без ошибки.
- [ ] `verify_acceptance_marker.sh` вернул `orders=1`.
- [ ] Логистический отчёт по дате выгружается.
- [ ] `Выгрузка КИЗов` не показывает незавершённые файлы.

## 3. SkladBot Matching

- [ ] Менеджер создал живую заявку `3PL отгрузка`.
- [ ] Диагностика нашла ровно одно совпадение.
- [ ] Дата отгрузки/выгрузки совпала.
- [ ] Клиент совпал после нормализации.
- [ ] Тип оплаты совпал.
- [ ] Товары совпали по цвету/формату.
- [ ] Количество совпало в блоках.
- [ ] Адрес использован только как мягкий признак.

## 4. Windows Desktop Acceptance

- [ ] Собран свежий test archive через `tools\\build_windows_test_archive.ps1`.
- [ ] Запуск выполнен из test archive, не из старого ярлыка `1.1.7`.
- [ ] `windows_backend_acceptance.ps1 -CheckOnly` прошёл.
- [ ] Desktop открылся без зависания.
- [ ] Список заказов обновился из backend.
- [ ] На экране статистики видно `Backend: online, список из VDS`.
- [ ] Найден заказ `{manifest["marker"]}`.
- [ ] Во время сканирования обновление списка не блокирует ввод.
- [ ] Отсканированы тестовые КИЗы:

{kiz_codes}

- [ ] Дубль КИЗа не принят.
- [ ] Завершение недосканированного заказа запрещено.
- [ ] Завершение досканированного заказа прошло.
- [ ] После завершения заказа появилось окно печати.
- [ ] Печать не открывает браузер.
- [ ] Размеры этикеток доступны: `100x100`, `100x150`, `75x50`, `58x40`.
- [ ] `Enter` подтверждает печать, `Esc` отменяет.
- [ ] Завершение смены сформировало КИЗ-отчёт.
- [ ] Окно `Возвраты` открывается.
- [ ] По ШК/номеру завершённой заявки находится архивный заказ.
- [ ] `Принять возврат` переводит заказ в возврат и обновляет список `Последние возвраты`.
- [ ] Повторное принятие той же заявки запрещено.

## 5. Cleanup

- [ ] Dry-run cleanup показал только тестовые данные.
- [ ] Cleanup с `--apply` выполнен.
- [ ] Повторная проверка маркера не показывает активные тестовые заказы.

## 6. Defects / Known Issues

| ID | Сценарий | Симптом | Severity | Решение | Статус |
| --- | --- | --- | --- | --- | --- |
| | | | | | |

## 7. Go / No-Go

- [ ] Telegram import принят.
- [ ] SkladBot matching принят.
- [ ] Windows desktop acceptance принят.
- [x] Критичных дефектов нет.
- [x] Rollback понятен.
- [x] `version.json` проверен и `mandatory=true`.

Итог:

- [ ] GO к подготовке release 2.0.
- [x] NO-GO, релиз откладывается.

Комментарий:

```text
Файл создан автоматически как стартовый NO-GO. Заполнять по факту ручных проверок.
```
"""


def build_result_template(manifest):
    kiz_codes = "\n".join(f"- [ ] `{code}`" for code in manifest["test_kiz_codes"])
    app_version = manifest["app_version"]
    return f"""# TakSklad 2.0 Acceptance Results

Дата проверки:

Проверяющий:

Среда:

- VDS: `https://api.taksklad.uz`
- Desktop source/build:
- Windows ПК:
- Сканер:
- Принтер:

Маркер проверки: `{manifest["marker"]}`

Файл Telegram import: `{manifest["excel_file"]}`

SHA-256 Excel: `{manifest["excel_sha256"]}`

## 1. Preflight

- [ ] `.venv/bin/python tools/release_preflight.py` вернул `status=ok`.
- [ ] `version.json` указывает на `{app_version}`, `mandatory=true`, ссылки и SHA заполнены.
- [ ] В Git нет tracked runtime/secret-файлов.

Заметки:

```text

```

## 2. Telegram Import

- [ ] В Telegram нажата кнопка `Дата отгрузки`.
- [ ] Отправлена дата `{manifest["shipment_date"]}`.
- [ ] Отправлен Excel-файл как документ.
- [ ] Бот ответил без ошибки.
- [ ] `verify_acceptance_marker.sh` вернул `orders=1`.
- [ ] Логистический отчёт по дате выгружается.
- [ ] `Выгрузка КИЗов` не показывает незавершённые файлы.

Команда проверки:

```bash
cd /opt/taksklad/app
{manifest["commands"]["telegram_verify"]}
```

Фактический результат:

```text

```

## 3. SkladBot Matching

- [ ] Менеджер создал живую заявку `3PL отгрузка`.
- [ ] Диагностика нашла ровно одно совпадение.
- [ ] Дата отгрузки/выгрузки совпала.
- [ ] Клиент совпал после нормализации.
- [ ] Тип оплаты совпал.
- [ ] Товары совпали по цвету/формату.
- [ ] Количество совпало в блоках.
- [ ] Адрес использован только как мягкий признак.

Команда диагностики:

```bash
cd /opt/taksklad/app
./deploy/vds/diagnose_skladbot_match.sh --marker "{manifest["marker"]}" --limit 5 --request-limit 20
```

Фактический результат:

```text

```

## 4. Windows Desktop Acceptance

- [ ] Собран свежий test archive через `tools\\build_windows_test_archive.ps1`.
- [ ] Запуск выполнен из test archive, не из старого ярлыка `1.1.7`.
- [ ] `windows_backend_acceptance.ps1 -CheckOnly` прошёл.
- [ ] Desktop открылся без зависания.
- [ ] Список заказов обновился из backend.
- [ ] На экране статистики видно `Backend: online, список из VDS`.
- [ ] Найден заказ `{manifest["marker"]}`.
- [ ] Во время сканирования обновление списка не блокирует ввод.
- [ ] Отсканированы тестовые КИЗы:

{kiz_codes}

- [ ] Дубль КИЗа не принят.
- [ ] Завершение недосканированного заказа запрещено.
- [ ] Завершение досканированного заказа прошло.
- [ ] После завершения заказа появилось окно печати.
- [ ] Печать не открывает браузер.
- [ ] Размеры этикеток доступны: `100x100`, `100x150`, `75x50`, `58x40`.
- [ ] `Enter` подтверждает печать, `Esc` отменяет.
- [ ] Завершение смены сформировало КИЗ-отчёт.
- [ ] Окно `Возвраты` открывается.
- [ ] По ШК/номеру завершённой заявки находится архивный заказ.
- [ ] `Принять возврат` переводит заказ в возврат и обновляет список `Последние возвраты`.
- [ ] Повторное принятие той же заявки запрещено.

Команда проверки backend после Windows:

```bash
cd /opt/taksklad/app
{manifest["commands"]["windows_verify"]}
```

Фактический результат:

```text

```

## 5. Cleanup

- [ ] Dry-run cleanup показал только тестовые данные.
- [ ] Cleanup с `--apply` выполнен.
- [ ] Повторная проверка маркера не показывает активные тестовые заказы.

Команды:

```bash
cd /opt/taksklad/app
{manifest["commands"]["cleanup_dry_run"]}
{manifest["commands"]["cleanup_apply"]}
```

Фактический результат:

```text

```

## 6. Defects / Known Issues

| ID | Сценарий | Симптом | Severity | Решение | Статус |
| --- | --- | --- | --- | --- | --- |
| | | | | | |

## 7. Go / No-Go

- [ ] Telegram import принят.
- [ ] SkladBot matching принят.
- [ ] Windows desktop acceptance принят.
- [ ] Критичных дефектов нет.
- [ ] Rollback понятен.
- [ ] `version.json` проверен и `mandatory=true`.

Итог:

- [ ] GO к подготовке release 2.0.
- [ ] NO-GO, релиз откладывается.

Комментарий:

```text

```

Машинная проверка заполненного результата:

```bash
cd /Users/anton/Documents/work/TakSklad
# Заполнить существующий ACCEPTANCE_RESULTS.md фактическими результатами.
.venv/bin/python tools/release_go_no_go.py --results outputs/taksklad_acceptance/ACCEPTANCE_RESULTS.md
```
"""


def prepare_acceptance_kit(output_dir=DEFAULT_OUTPUT_DIR, marker=DEFAULT_MARKER, shipment_date=DEFAULT_SHIPMENT_DATE):
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    excel_path = output_dir / EXCEL_NAME
    save_acceptance_excel(excel_path, marker=marker, shipment_date=shipment_date)
    manifest = build_manifest(output_dir=output_dir, marker=marker, shipment_date=shipment_date)
    (output_dir / MANIFEST_NAME).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (output_dir / README_NAME).write_text(build_readme(manifest), encoding="utf-8")
    (output_dir / RESULT_TEMPLATE_NAME).write_text(build_result_template(manifest), encoding="utf-8")
    result_file_path = output_dir / RESULT_FILE_NAME
    if not result_file_path.exists():
        result_file_path.write_text(build_initial_result_file(manifest), encoding="utf-8")
    return output_dir


def parse_args():
    parser = argparse.ArgumentParser(description="Prepare TakSklad acceptance kit.")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR), help="Output directory.")
    parser.add_argument("--marker", default=DEFAULT_MARKER, help="Acceptance client marker.")
    parser.add_argument("--shipment-date", default=DEFAULT_SHIPMENT_DATE, help="Shipment date in DD.MM.YYYY format.")
    return parser.parse_args()


def main():
    args = parse_args()
    output_dir = prepare_acceptance_kit(
        output_dir=args.output_dir,
        marker=args.marker,
        shipment_date=args.shipment_date,
    )
    print(output_dir)


if __name__ == "__main__":
    main()
