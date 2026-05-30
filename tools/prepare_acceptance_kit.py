#!/usr/bin/env python3
import argparse
import hashlib
import json
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
TEST_KIZ_CODES = [
    "WIN-KIZ-ACCEPT-001",
    "WIN-KIZ-ACCEPT-002",
    "WIN-KIZ-ACCEPT-003",
]


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
            "telegram_verify": './deploy/vds/verify_acceptance_marker.sh "ACCEPTANCE TELEGRAM 20260531" --expect-orders 1',
            "windows_check_only": '.\\tools\\windows_backend_acceptance.ps1 -CheckOnly -Token "<service-token>"',
            "windows_launch_exe": '.\\tools\\windows_backend_acceptance.ps1 -Token "<service-token>" -AppPath ".\\TakSklad.exe"',
            "windows_launch_source": '.\\tools\\windows_backend_acceptance.ps1 -Token "<service-token>" -AppPath ".\\main.py"',
            "windows_verify": './deploy/vds/verify_acceptance_marker.sh "ACCEPTANCE TELEGRAM 20260531" --expect-orders 1 --expect-scans 3 --expect-completed',
            "cleanup_dry_run": './deploy/vds/cleanup_acceptance_marker.sh "ACCEPTANCE TELEGRAM 20260531"',
            "cleanup_apply": './deploy/vds/cleanup_acceptance_marker.sh "ACCEPTANCE TELEGRAM 20260531" --apply',
        },
        "safety": {
            "no_version_json_change": True,
            "no_windows_release_archive": True,
            "no_github_release": True,
            "no_push_notifications": True,
            "no_real_skladbot_request_creation": True,
            "contains_secrets": False,
        },
    }


def build_readme(manifest):
    kiz_codes = "\n".join(f"- `{code}`" for code in manifest["test_kiz_codes"])
    return f"""# TakSklad Acceptance Kit

Назначение: ручная проверка Telegram import и Windows desktop acceptance без релиза, без изменения `version.json` и без push-уведомлений рабочим ПК.

## Состав

- `{manifest["excel_file"]}` - Excel для отправки в Telegram-бот.
- `{MANIFEST_NAME}` - контрольные значения, checksum и команды проверки.
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

1. В Telegram открыть `SkladKis_bot` от разрешённого пользовательского аккаунта.
2. Нажать `Дата отгрузки`.
3. Отправить `{manifest["shipment_date"]}`.
4. Отправить `{manifest["excel_file"]}` как документ.
5. После ответа бота проверить VDS:

```bash
cd /opt/taksklad/app
{manifest["commands"]["telegram_verify"]}
```

## Windows Проверка

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

Сканировать тестовые КИЗы:

{kiz_codes}

После завершения заказа проверить VDS:

```bash
cd /opt/taksklad/app
{manifest["commands"]["windows_verify"]}
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

- Не менять `version.json`.
- Не создавать Windows release archive.
- Не создавать GitHub Release.
- Не отправлять push-уведомления.
- Не создавать реальную заявку SkladBot без отдельного подтверждения.
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
