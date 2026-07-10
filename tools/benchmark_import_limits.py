#!/usr/bin/env python3
import argparse
import hashlib
import sys
import tempfile
import time
import tracemalloc
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile

import openpyxl
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.app.excel_importer import excel_file_to_import_payload
from backend.app.input_safety import MAX_IMPORT_CELL_CHARS, MAX_IMPORT_ROWS
from backend.app.imports_service import create_import
from backend.app.models import Base
from backend.app.schemas import ImportCreate


MAX_VALID_COLUMNS = 128
TIME_BUDGET_SECONDS = 75.0
PEAK_MEMORY_BUDGET_MIB = 256.0


def emit(message: str) -> None:
    sys.stdout.write(f"{message}\n")


def deterministic_text(index: int, length: int) -> str:
    pieces = []
    counter = 0
    while sum(map(len, pieces)) < length:
        pieces.append(hashlib.sha256(f"synthetic-{index}-{counter}".encode()).hexdigest())
        counter += 1
    return "".join(pieces)[:length]


def create_maximum_valid_workbook(path: Path) -> None:
    workbook = openpyxl.Workbook(write_only=True)
    sheet = workbook.create_sheet("Заявки")
    headers = [
        "Дата отгрузки",
        "Клиент",
        "Тип оплаты",
        "Товары",
        "Кол-во ШТ",
        "Кол-во блок",
        "Адрес",
    ] + [f"Synthetic bounded column {index}" for index in range(8, MAX_VALID_COLUMNS + 1)]
    sheet.append(headers)
    for index in range(1, MAX_IMPORT_ROWS + 1):
        extra_values = [f"v{index}-{column}" for column in range(8, MAX_VALID_COLUMNS + 1)]
        if index == 1:
            extra_values[-1] = deterministic_text(index, MAX_IMPORT_CELL_CHARS)
        sheet.append([
            "10.07.2026",
            "Synthetic boundary client",
            "Терминал",
            "Synthetic boundary product",
            20,
            2,
            "Самовывоз со склада",
            *extra_values,
        ])
    workbook.save(path)


def archive_metrics(path: Path) -> tuple[int, int, float]:
    with ZipFile(path) as archive:
        compressed = sum(info.compress_size for info in archive.infolist())
        uncompressed = sum(info.file_size for info in archive.infolist())
    ratio = uncompressed / max(1, compressed)
    return compressed, uncompressed, ratio


def run_maximum_valid(assert_budgets: bool) -> int:
    with tempfile.TemporaryDirectory(prefix="taksklad-synthetic-input-limit-") as temp_dir:
        path = Path(temp_dir) / "synthetic-maximum-valid.xlsx"
        create_maximum_valid_workbook(path)
        compressed, uncompressed, ratio = archive_metrics(path)
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

        tracemalloc.start()
        started = time.perf_counter()
        payload = excel_file_to_import_payload(
            path,
            file_name=path.name,
            source="synthetic_input_limit_benchmark",
            shipment_date="10.07.2026",
            force_shipment_date=True,
        )
        import_rows = []
        for row in payload["rows"]:
            bounded_row = dict(row)
            bounded_row["ID импорта"] = ""
            import_rows.append(bounded_row)
        validated = ImportCreate.model_validate({
            "source": payload["source"],
            "filename": payload["filename"],
            "sha256": payload["sha256"],
            "rows": import_rows,
        })
        google_result = {"status": "synthetic_stub", "imported": 0, "duplicates": 0, "updated": 0, "error": ""}
        skladbot_result = {
            "status": "synthetic_stub", "ready": 0, "blocked": 0,
            "already_linked": 0, "linked_mismatch": 0, "event_id": "",
        }
        with (
            SessionLocal() as db,
            patch("backend.app.imports_service.export_import_records_to_google_sheets", return_value=google_result),
            patch("backend.app.imports_service.create_skladbot_dry_run_for_import", return_value=skladbot_result),
        ):
            result = create_import(db, validated)
        elapsed = time.perf_counter() - started
        _current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        engine.dispose()

    peak_mib = peak / (1024 * 1024)
    rows = len(validated.rows)
    emit(
        "maximum-valid: "
        f"rows={rows} columns={MAX_VALID_COLUMNS} cell_chars={MAX_IMPORT_CELL_CHARS} "
        f"compressed_bytes={compressed} uncompressed_bytes={uncompressed} ratio={ratio:.2f} "
        f"elapsed_seconds={elapsed:.3f} peak_memory_mib={peak_mib:.2f} "
        f"import_status={result.status} items_created={result.items_created} duplicates={result.duplicate_rows}"
    )
    emit(
        "maximum-valid-budgets: "
        f"time_seconds<={TIME_BUDGET_SECONDS:.1f} peak_memory_mib<={PEAK_MEMORY_BUDGET_MIB:.1f}"
    )
    failures = []
    if rows != MAX_IMPORT_ROWS:
        failures.append(f"rows={rows}")
    if result.status not in {"completed", "completed_with_errors"} or result.items_created != 1:
        failures.append(f"import_status={result.status}:items={result.items_created}")
    if elapsed > TIME_BUDGET_SECONDS:
        failures.append(f"elapsed_seconds={elapsed:.3f}")
    if peak_mib > PEAK_MEMORY_BUDGET_MIB:
        failures.append(f"peak_memory_mib={peak_mib:.2f}")
    if assert_budgets and failures:
        emit("maximum-valid: FAIL " + " ".join(failures))
        return 1
    emit("maximum-valid: PASS")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Synthetic TakSklad import boundary benchmark")
    parser.add_argument("--profile", choices=("maximum-valid",), required=True)
    parser.add_argument("--assert-budgets", action="store_true")
    args = parser.parse_args()
    return run_maximum_valid(args.assert_budgets)


if __name__ == "__main__":
    raise SystemExit(main())
