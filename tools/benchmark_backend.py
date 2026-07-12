#!/usr/bin/env python3
"""Deterministic disposable-PostgreSQL performance evidence for TakSklad."""

from __future__ import annotations

import argparse
import gc
import hashlib
import json
import math
import os
import platform
import random
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import psycopg
from psycopg.types.json import Jsonb
from sqlalchemy import create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker


ROOT = Path(__file__).resolve().parents[1]
PROFILES_PATH = ROOT / "performance" / "backend_profiles.json"
BUDGETS_PATH = ROOT / "performance" / "backend_budgets.json"
EVIDENCE_DIR = ROOT / ".release-state" / "performance"
POSTGRES_IMAGE = os.environ.get("TAKSKLAD_POSTGRES_TEST_IMAGE", "postgres:16-alpine")
SYNTHETIC_DATE = date(2026, 1, 15)
NAMESPACE = uuid.UUID("c6ee3541-ec80-4cc4-a541-07cb03306d8a")
TABLES = (
    "orders",
    "order_items",
    "scan_codes",
    "kiz_codes",
    "kiz_movements",
    "pending_events",
    "imports",
    "import_files",
    "audit_log",
)
SAMPLE_QUIESCENCE_SECONDS = 0.005
POST_SEED_SETTLE_SECONDS = 1.0
FRESH_RUN_COOLDOWN_SECONDS = 5.0
WORKLOAD_COOLDOWN_SECONDS = 3.0
MAX_LOAD_PER_CPU = 0.35
QUIESCENCE_TIMEOUT_SECONDS = 180
WORKLOAD_MAINTENANCE = {
    "scan_db": {"interval": 10, "tables": ("audit_log", "kiz_movements", "scan_codes", "kiz_codes", "order_items", "pending_events")},
    "complete_db": {"interval": 1, "tables": ("audit_log", "order_items", "orders", "pending_events")},
    "return_db": {"interval": 10, "tables": ("audit_log", "pending_events", "kiz_movements", "orders")},
    "queue_claim_50": {"interval": 1, "tables": ("pending_events",)},
}


def load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def sha256_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def benchmark_contract_hashes():
    paths = {
        "runner": Path(__file__),
        "profiles": PROFILES_PATH,
        "budgets": BUDGETS_PATH,
        "event_leases": ROOT / "backend/app/event_leases.py",
        "imports_service": ROOT / "backend/app/imports_service.py",
        "kiz_movements_service": ROOT / "backend/app/kiz_movements_service.py",
        "models": ROOT / "backend/app/models.py",
        "orders_service": ROOT / "backend/app/orders_service.py",
        "outbox_service": ROOT / "backend/app/outbox_service.py",
    }
    return {name: sha256_file(path) for name, path in paths.items()}


def baseline_compatibility_failures(approved):
    expected = benchmark_contract_hashes()
    actual = ((approved.get("host") or {}).get("working_tree_source_hashes") or {})
    return [
        f"approved baseline contract hash mismatch: {name}"
        for name, digest in expected.items()
        if actual.get(name) != digest
    ]


def deterministic_uuid(seed, kind, index):
    return uuid.uuid5(NAMESPACE, f"SYNTHETIC-TAKSKLAD-PERF:{seed}:{kind}:{index}")


def psycopg_url(database_url):
    return make_url(database_url).set(drivername="postgresql").render_as_string(hide_password=False)


def run_command(arguments, *, env=None):
    completed = subprocess.run(
        arguments,
        cwd=ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"command failed ({completed.returncode}): {' '.join(arguments)}\n{completed.stdout[-4000:]}")
    return completed.stdout


def ensure_foreground_task_policy():
    if platform.system() != "Darwin":
        return "not_applicable"
    completed = subprocess.run(
        ["taskpolicy", "-B", "-t", "0", "-l", "0", "-p", str(os.getpid())],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    if completed.returncode:
        raise RuntimeError("failed to remove background QoS from Darwin benchmark process")
    return "foreground"


def wait_for_benchmark_quiescence():
    cpu_count = max(1, int(os.cpu_count() or 1))
    deadline = time.monotonic() + QUIESCENCE_TIMEOUT_SECONDS
    waited = 0
    while True:
        load_1m = float(os.getloadavg()[0])
        load_per_cpu = load_1m / cpu_count
        if load_per_cpu <= MAX_LOAD_PER_CPU:
            return {
                "waited_seconds": waited,
                "load_1m": round(load_1m, 3),
                "load_per_cpu": round(load_per_cpu, 4),
                "max_load_per_cpu": MAX_LOAD_PER_CPU,
            }
        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"benchmark host did not quiesce: load_per_cpu={load_per_cpu:.4f} "
                f"limit={MAX_LOAD_PER_CPU:.4f}"
            )
        time.sleep(1.0)
        waited += 1


@contextmanager
def disposable_database():
    configured = os.environ.get("TAKSKLAD_TEST_DATABASE_URL", "").strip()
    if configured:
        yield configured, {"container": "provided", "image": POSTGRES_IMAGE}
        return

    container = f"taksklad-phase6-pg-{os.getpid()}-{random.Random(20260710).randrange(10000, 99999)}"
    password = "synthetic-phase6-only"
    try:
        run_command([
            "docker", "run", "--detach", "--rm",
            "--name", container,
            "--tmpfs", "/var/lib/postgresql/data:rw,nosuid,nodev,size=2g",
            "--env", f"POSTGRES_PASSWORD={password}",
            "--env", "POSTGRES_DB=postgres",
            "--publish", "127.0.0.1::5432",
            POSTGRES_IMAGE,
        ])
        ready = False
        for _attempt in range(120):
            probe = subprocess.run(
                ["docker", "exec", container, "pg_isready", "-U", "postgres", "-d", "postgres"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            if probe.returncode == 0:
                ready = True
                break
            time.sleep(0.25)
        if not ready:
            raise RuntimeError("disposable PostgreSQL did not become ready")
        port_line = run_command(["docker", "port", container, "5432/tcp"]).strip().splitlines()[0]
        port = port_line.rsplit(":", 1)[-1]
        database_url = f"postgresql+psycopg://postgres:{password}@127.0.0.1:{port}/postgres"
        migration_env = os.environ.copy()
        migration_env.update({
            "DATABASE_URL": database_url,
            "TAKSKLAD_ENV": "test",
            "TAKSKLAD_API_TOKEN": "synthetic-only-test-token",
        })
        run_command([sys.executable, "-m", "alembic", "-c", "backend/alembic.ini", "upgrade", "head"], env=migration_env)
        yield database_url, {"container": container, "image": POSTGRES_IMAGE}
    finally:
        subprocess.run(
            ["docker", "rm", "-f", container],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )


def batches(values, size=1000):
    for start in range(0, len(values), size):
        yield values[start:start + size]


def insert_many(connection, statement, values):
    for batch in batches(values):
        with connection.cursor() as cursor:
            cursor.executemany(statement, batch)


def expected_counts(profile):
    items = profile["orders"] * profile["items_per_order"]
    scans = items * profile["scans_per_item"]
    return {
        "orders": profile["orders"],
        "order_items": items,
        "scan_codes": scans,
        "kiz_codes": scans,
        "kiz_movements": scans,
        "pending_events": profile["pending_events"],
        "imports": profile["imports"],
        "import_files": profile["imports"],
        "audit_log": 0,
    }


def seed_profile(database_url, profile_name):
    config = load_json(PROFILES_PATH)
    profile = dict(config["profiles"][profile_name])
    seed = int(profile["seed"])
    orders_count = int(profile["orders"])
    items_per_order = int(profile["items_per_order"])
    scans_per_item = int(profile["scans_per_item"])
    fixed_time = datetime(2026, 1, 15, 8, 0, tzinfo=timezone.utc)

    orders = []
    items = []
    scans = []
    kiz_codes = []
    movements = []
    for order_index in range(orders_count):
        order_id = deterministic_uuid(seed, "order", order_index)
        scan_end = int(profile["scan_targets"])
        complete_end = scan_end + int(profile["complete_targets"])
        return_end = complete_end + int(profile["return_targets"])
        if order_index < complete_end:
            order_status = "not_completed"
        elif order_index < return_end:
            order_status = "completed"
        elif order_index % 5 == 0:
            order_status = "completed"
        elif order_index % 17 == 0:
            order_status = "returned"
        else:
            order_status = "not_completed"
        orders.append((
            order_id,
            "synthetic_performance",
            f"SYNTHETIC-ORDER-{order_index:08d}",
            SYNTHETIC_DATE,
            "Synthetic terminal" if order_index % 2 == 0 else "Synthetic transfer",
            f"SYNTHETIC CLIENT {order_index:08d}",
            f"SYNTHETIC ADDRESS {order_index:08d}",
            f"SYNTHETIC REP {order_index % 20:03d}",
            order_status,
            Jsonb({
                "synthetic": True,
                "profile": profile_name,
                "coordinates": "0.0000, 0.0000",
                "skladbot_request_number": f"SYNTHETIC-REQ-{order_index:08d}",
            }),
            fixed_time + timedelta(seconds=order_index),
            fixed_time + timedelta(seconds=order_index),
        ))
        for item_offset in range(items_per_order):
            item_index = order_index * items_per_order + item_offset
            item_id = deterministic_uuid(seed, "item", item_index)
            completed = order_status in {"completed", "returned"}
            scanned_blocks = scans_per_item
            requires_kiz = not (scan_end <= order_index < complete_end)
            items.append((
                item_id,
                order_id,
                f"SYNTHETIC PRODUCT {item_offset:03d}",
                40,
                scans_per_item if completed else scans_per_item + 2,
                20,
                scanned_blocks,
                requires_kiz,
                "completed" if completed else "not_completed",
                Jsonb({"synthetic": True, "line_total": 1000 + item_index}),
                fixed_time + timedelta(seconds=item_index),
                fixed_time + timedelta(seconds=item_index),
            ))
            for scan_offset in range(scans_per_item):
                scan_index = item_index * scans_per_item + scan_offset
                scan_id = deterministic_uuid(seed, "scan", scan_index)
                kiz_id = deterministic_uuid(seed, "kiz", scan_index)
                code = f"SYNTHETIC-KIZ-{scan_index:014d}"
                scanned_at = fixed_time + timedelta(milliseconds=scan_index)
                scans.append((
                    scan_id, item_id, code, "synthetic", "SYNTHETIC-PC", "synthetic-benchmark",
                    scanned_at, Jsonb({"synthetic": True, "scan_type": "unit", "block_quantity": 1}),
                ))
                kiz_codes.append((kiz_id, code, scanned_at, scanned_at))
                movements.append((
                    deterministic_uuid(seed, "movement", scan_index), kiz_id, "outbound", order_id, item_id,
                    scan_id, None, "synthetic", "synthetic-benchmark", "SYNTHETIC-PC", scanned_at,
                    Jsonb({"synthetic": True}),
                ))

    pending_events = []
    for index in range(int(profile["pending_events"])):
        created_at = fixed_time + timedelta(milliseconds=index)
        pending_events.append((
            deterministic_uuid(seed, "event", index),
            "synthetic_benchmark",
            f"SYNTHETIC-EVENT-{profile_name}-{index:08d}",
            "pending" if index % 5 else "failed",
            0,
            Jsonb({"synthetic": True, "sequence": index}),
            "" if index % 5 else "synthetic retry",
            created_at,
            None,
            None,
            None,
            created_at,
            created_at,
        ))

    imports = []
    import_files = []
    for index in range(int(profile["imports"])):
        import_id = deterministic_uuid(seed, "import", index)
        imports.append((
            import_id, "synthetic_performance", "completed", 1000, 1000,
            Jsonb({"synthetic": True, "source_batch_key": f"SYNTHETIC-BATCH-{index:06d}"}),
            fixed_time + timedelta(seconds=index),
        ))
        import_files.append((
            deterministic_uuid(seed, "import-file", index), import_id,
            f"synthetic-order-file-{index:06d}.xlsx",
            hashlib.sha256(f"synthetic-file-{seed}-{index}".encode()).hexdigest(),
            1024 + index,
            fixed_time + timedelta(seconds=index),
        ))

    with psycopg.connect(psycopg_url(database_url)) as connection:
        for table in reversed(TABLES):
            connection.execute(f"TRUNCATE TABLE {table} CASCADE")
        insert_many(connection, """
            INSERT INTO orders
            (id, source, external_id, order_date, payment_type, client, address, representative, status, raw_payload, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, orders)
        insert_many(connection, """
            INSERT INTO order_items
            (id, order_id, product, quantity_pieces, quantity_blocks, pieces_per_block, scanned_blocks, requires_kiz, status, raw_payload, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, items)
        insert_many(connection, """
            INSERT INTO scan_codes
            (id, order_item_id, code, source, workstation_id, scanned_by, scanned_at, raw_payload)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """, scans)
        insert_many(connection, "INSERT INTO kiz_codes (id, code, first_seen_at, updated_at) VALUES (%s, %s, %s, %s)", kiz_codes)
        insert_many(connection, """
            INSERT INTO kiz_movements
            (id, kiz_id, movement_type, order_id, order_item_id, scan_code_id, return_reference, source, actor, workstation_id, occurred_at, raw_payload)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, movements)
        insert_many(connection, """
            INSERT INTO pending_events
            (id, event_type, idempotency_key, status, attempts, payload, last_error, available_at, lease_owner, lease_expires_at, completed_at, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, pending_events)
        insert_many(connection, """
            INSERT INTO imports (id, source, status, rows_total, rows_imported, raw_payload, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        """, imports)
        insert_many(connection, """
            INSERT INTO import_files (id, import_id, filename, sha256, size_bytes, created_at)
            VALUES (%s, %s, %s, %s, %s, %s)
        """, import_files)
        connection.commit()
        connection.execute("ANALYZE")
        connection.commit()

        actual = {
            table: connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in TABLES
        }
        hot = {
            "scan_targets": int(profile["scan_targets"]),
            "complete_targets": int(profile["complete_targets"]),
            "return_targets": int(profile["return_targets"]),
            "active_orders": connection.execute("SELECT count(*) FROM orders WHERE status = 'not_completed'").fetchone()[0],
            "completed_or_returned_orders": connection.execute(
                "SELECT count(*) FROM orders WHERE status IN ('completed', 'returned')"
            ).fetchone()[0],
            "report_date_orders": connection.execute(
                "SELECT count(*) FROM orders WHERE order_date = %s", (SYNTHETIC_DATE,)
            ).fetchone()[0],
            "claimable_events": connection.execute(
                "SELECT count(*) FROM pending_events WHERE event_type = 'synthetic_benchmark' AND status IN ('pending', 'failed')"
            ).fetchone()[0],
            "active_page_size": 50,
            "admin_page_size": 50,
            "import_rows": 1000,
            "queue_claim_batch": 50,
        }
        pg_version = connection.execute("SHOW server_version").fetchone()[0]

    expected = expected_counts(profile)
    if actual != expected:
        raise AssertionError(f"seed count mismatch expected={expected} actual={actual}")
    manifest = {
        "schema": 1,
        "profile": profile_name,
        "seed": seed,
        "synthetic_namespace": config["synthetic_contract"]["namespace"],
        "contains_real_data": False,
        "production_scale_status": config["synthetic_contract"]["production_scale_status"],
        "production_scale_reason": config["synthetic_contract"]["production_scale_reason"],
        "table_counts": actual,
        "hot_working_set": hot,
        "postgres_version": pg_version,
        "profiles_sha256": sha256_file(PROFILES_PATH),
    }
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    path = EVIDENCE_DIR / f"dataset-{profile_name}.json"
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest, path


def percentile(values, pct):
    ordered = sorted(values)
    rank = max(1, math.ceil(len(ordered) * pct / 100))
    return ordered[min(len(ordered), rank) - 1]


def summarize(values_ms):
    return {
        "p50_ms": round(percentile(values_ms, 50), 3),
        "p95_ms": round(percentile(values_ms, 95), 3),
        "p99_ms": round(percentile(values_ms, 99), 3),
        "max_ms": round(max(values_ms), 3),
    }


def import_rows():
    return [
        {
            "Дата отгрузки": "15.01.2026",
            "Тип оплаты": "Synthetic terminal",
            "Клиент": f"SYNTHETIC IMPORT CLIENT {index:04d}",
            "Адрес": f"SYNTHETIC IMPORT ADDRESS {index:04d}",
            "Торговый представитель": "SYNTHETIC IMPORT REP",
            "Товары": f"SYNTHETIC IMPORT PRODUCT {index % 10:02d}",
            "Кол-во ШТ": 40,
            "Кол-во блок": 2,
            "_pieces_per_block": 20,
            "ID заказа": f"SYNTHETIC-BENCH-IMPORT-ORDER-{index:04d}",
            "ID импорта": f"SYNTHETIC-BENCH-IMPORT-ROW-{index:04d}",
            "Источник файла": "synthetic-benchmark-import.xlsx",
            "Строка файла": index + 2,
        }
        for index in range(1000)
    ]


def workload_context(database_url, profile):
    seed = profile["seed"]
    return {
        "database_url": database_url,
        "seed": seed,
        "scan_item": deterministic_uuid(seed, "item", 0),
        "complete_order": deterministic_uuid(seed, "order", int(profile["scan_targets"])),
        "return_order": deterministic_uuid(
            seed, "order", int(profile["scan_targets"]) + int(profile["complete_targets"])
        ),
        "return_items": [
            {
                "item_id": str(deterministic_uuid(
                    seed,
                    "item",
                    (int(profile["scan_targets"]) + int(profile["complete_targets"]))
                    * int(profile["items_per_order"])
                    + offset,
                )),
                "product": f"SYNTHETIC PRODUCT {offset:03d}",
                "quantity_blocks": int(profile["scans_per_item"]),
                "quantity_pieces": 40,
            }
            for offset in range(int(profile["items_per_order"]))
        ],
        "imports": import_rows(),
        "base_scanned_blocks": int(profile["scans_per_item"]),
    }


def cleanup_sql(context, statements):
    owned_connection = context.get("_benchmark_cleanup_connection")

    def execute(connection):
        rowcounts = []
        for statement, parameters in statements:
            rowcounts.append(connection.execute(statement, parameters).rowcount)
        connection.commit()
        return rowcounts

    if owned_connection is not None:
        return execute(owned_connection)
    with psycopg.connect(psycopg_url(context["database_url"])) as connection:
        return execute(connection)


def benchmark_vacuum(context, table_name):
    if table_name not in TABLES:
        raise ValueError("benchmark vacuum table is not allowlisted")
    connection = context.get("_benchmark_cleanup_connection")
    if connection is None:
        return False
    previous_autocommit = connection.autocommit
    connection.autocommit = True
    try:
        connection.execute(f"VACUUM (ANALYZE) {table_name}")
    finally:
        connection.autocommit = previous_autocommit
    return True


def prepare_workload_maintenance(context, workload_name):
    maintenance = WORKLOAD_MAINTENANCE.get(workload_name)
    if maintenance is None:
        return None
    connection = context["_benchmark_cleanup_connection"]
    previous_autocommit = connection.autocommit
    connection.autocommit = True
    try:
        for table_name in maintenance["tables"]:
            connection.execute(f"ALTER TABLE {table_name} SET (autovacuum_enabled=false)")
            connection.execute(f"VACUUM (ANALYZE) {table_name}")
    finally:
        connection.autocommit = previous_autocommit
    return maintenance


def prepare_profile_benchmark(database_url):
    with psycopg.connect(psycopg_url(database_url), autocommit=True) as connection:
        connection.execute("CHECKPOINT")
        for table_name in TABLES:
            connection.execute(f"VACUUM (ANALYZE) {table_name}")
    time.sleep(POST_SEED_SETTLE_SECONDS)


@contextmanager
def isolated_gc_sample():
    time.sleep(SAMPLE_QUIESCENCE_SECONDS)
    gc.collect()
    was_enabled = gc.isenabled()
    if was_enabled:
        gc.disable()
    try:
        yield
    finally:
        if was_enabled:
            gc.enable()


def scan_db(db, context, iteration):
    from backend.app.orders_service import create_scan
    from backend.app.schemas import ScanCreate

    code = f"SYNTHETIC-BENCH-SCAN-{iteration:08d}"
    result = create_scan(db, ScanCreate(
        order_item_id=str(context["scan_item"]), code=code,
        workstation_id="SYNTHETIC-PC", scanned_by="synthetic-benchmark",
        raw_payload={"synthetic": True},
    ))

    def cleanup():
        rowcounts = cleanup_sql(context, [
            ("DELETE FROM audit_log WHERE action='scan_code_created' AND payload->>'code'=%s", (code,)),
            ("DELETE FROM audit_log WHERE action='google_sheets_scan_export' AND entity_id=%s",
             (str(context["scan_item"]),)),
            ("DELETE FROM pending_events WHERE event_type='google_sheets_export' "
             "AND action='google_sheets_scan_export' AND aggregate_id=%s",
             (str(context["scan_item"]),)),
            ("DELETE FROM kiz_movements WHERE scan_code_id IN (SELECT id FROM scan_codes WHERE code=%s)", (code,)),
            ("DELETE FROM scan_codes WHERE code=%s", (code,)),
            ("DELETE FROM kiz_codes WHERE code=%s", (code,)),
            ("UPDATE order_items SET scanned_blocks=%s, status='not_completed' WHERE id=%s",
             (context["base_scanned_blocks"], context["scan_item"])),
        ])
        expected = [1, 1, 1, 1, 1, 1, 1]
        if rowcounts != expected:
            raise AssertionError(f"scan benchmark cleanup mismatch expected={expected} actual={rowcounts}")
    return 1, cleanup


def complete_db(db, context, _iteration):
    from backend.app.orders_service import complete_order

    result = complete_order(db, context["complete_order"])

    def cleanup():
        rowcounts = cleanup_sql(context, [
            ("DELETE FROM audit_log WHERE action='order_completed' AND entity_id=%s", (str(context["complete_order"]),)),
            ("DELETE FROM audit_log WHERE action='google_sheets_archive_export' AND entity_id=%s",
             (str(context["complete_order"]),)),
            ("DELETE FROM pending_events WHERE event_type='google_sheets_export' "
             "AND action='google_sheets_archive_export' AND aggregate_id=%s",
             (str(context["complete_order"]),)),
            ("UPDATE order_items SET status='not_completed' WHERE order_id=%s", (context["complete_order"],)),
            ("UPDATE orders SET status='not_completed' WHERE id=%s", (context["complete_order"],)),
        ])
        expected = [1, 1, 1, len(context["return_items"]), 1]
        if rowcounts != expected:
            raise AssertionError(f"complete benchmark cleanup mismatch expected={expected} actual={rowcounts}")
    return len(result.items), cleanup


def return_db(db, context, iteration):
    from backend.app.orders_service import mark_order_returned

    result = mark_order_returned(
        db, context["return_order"], return_reference=f"SYNTHETIC-RETURN-{iteration:08d}",
        returned_by="synthetic-benchmark", confirmed_items=context["return_items"],
    )

    def cleanup():
        rowcounts = cleanup_sql(context, [
            ("DELETE FROM audit_log WHERE action IN ("
             "'order_returned','skladbot_return_request_create_queued',"
             "'google_sheets_archive_export','google_sheets_return_export') AND entity_id=%s",
             (str(context["return_order"]),)),
            ("DELETE FROM pending_events WHERE event_type <> 'synthetic_benchmark'", ()),
            ("DELETE FROM kiz_movements WHERE movement_type='return' AND order_id=%s", (context["return_order"],)),
            ("UPDATE orders SET status='completed', raw_payload=raw_payload - ARRAY["
             "'return_status','returned_at','return_reference','returned_by','skladbot_return_confirmed_items',"
             "'skladbot_return_request_status','skladbot_return_create_event_id',"
             "'skladbot_return_create_idempotency_key'] "
             "WHERE id=%s", (context["return_order"],)),
        ])
        expected_movements = sum(int(item["quantity_blocks"]) for item in context["return_items"])
        expected = [4, 3, expected_movements, 1]
        if rowcounts != expected:
            raise AssertionError(f"return benchmark cleanup mismatch expected={expected} actual={rowcounts}")
    return len(result.items), cleanup


def active_orders_first_page(db, _context, _iteration):
    from backend.app.orders_service import list_active_orders

    result = list_active_orders(db)
    return len(result[:50]), None


def admin_filtered_page(db, _context, _iteration):
    from backend.app.admin_service import build_admin_table

    result = build_admin_table(
        db, limit=50, offset=0, activity_limit=30,
        status_bucket="active", shipment_date=SYNTHETIC_DATE.isoformat(),
    )
    return len(result.rows), None


def day_report(db, _context, _iteration):
    from backend.app.reports_service import build_day_report

    result = build_day_report(db, SYNTHETIC_DATE.isoformat())
    return len(result.orders), None


def dashboard_report(db, _context, _iteration):
    from backend.app.reports_service import build_dashboard_day_summary

    build_dashboard_day_summary(db, SYNTHETIC_DATE.isoformat())
    return 1, None


def import_1000(db, context, _iteration):
    from backend.app.imports_service import create_import
    from backend.app.schemas import ImportCreate

    payload = ImportCreate(
        source="synthetic_benchmark_import",
        filename="synthetic-benchmark-import.xlsx",
        sha256=hashlib.sha256(b"synthetic-benchmark-import").hexdigest(),
        rows=context["imports"],
    )
    google_result = {"status": "synthetic_stub", "imported": 0, "duplicates": 0, "updated": 0, "error": ""}
    skladbot_result = {
        "status": "synthetic_stub", "ready": 0, "blocked": 0,
        "already_linked": 0, "linked_mismatch": 0, "event_id": "",
    }
    with (
        patch("backend.app.imports_service.export_import_records_to_google_sheets", return_value=google_result),
        patch("backend.app.imports_service.create_skladbot_dry_run_for_import", return_value=skladbot_result),
    ):
        result = create_import(db, payload)

    def cleanup():
        cleanup_sql(context, [
            ("DELETE FROM audit_log WHERE action='orders_imported' AND entity_id IN "
             "(SELECT id::text FROM imports WHERE source='synthetic_benchmark_import')", ()),
            ("DELETE FROM pending_events WHERE event_type <> 'synthetic_benchmark'", ()),
            ("DELETE FROM incidents WHERE entity_type='import' AND entity_id IN "
             "(SELECT id::text FROM imports WHERE source='synthetic_benchmark_import')", ()),
            ("DELETE FROM order_items WHERE order_id IN "
             "(SELECT id FROM orders WHERE source='synthetic_benchmark_import')", ()),
            ("DELETE FROM orders WHERE source='synthetic_benchmark_import'", ()),
            ("DELETE FROM import_files WHERE import_id IN "
             "(SELECT id FROM imports WHERE source='synthetic_benchmark_import')", ()),
            ("DELETE FROM imports WHERE source='synthetic_benchmark_import'", ()),
            ("DELETE FROM client_points WHERE client_name LIKE 'SYNTHETIC IMPORT CLIENT %%'", ()),
        ])
    return int(result.rows_imported), cleanup


def queue_claim_50(db, _context, iteration):
    from backend.app.event_leases import claim_event_leases

    owner = f"synthetic-benchmark-{iteration:08d}"
    result = claim_event_leases(
        db, event_types=("synthetic_benchmark",), owner=owner, limit=50,
    )

    def cleanup():
        rowcounts = cleanup_sql(_context, [
            ("UPDATE pending_events SET "
             "status=CASE WHEN mod((payload->>'sequence')::integer, 5)=0 THEN 'failed' ELSE 'pending' END, "
             "attempts=0, "
             "last_error=CASE WHEN mod((payload->>'sequence')::integer, 5)=0 "
             "THEN 'synthetic retry' ELSE '' END, "
             "available_at=created_at, lease_owner=NULL, lease_expires_at=NULL, completed_at=NULL, "
             "updated_at=created_at WHERE event_type='synthetic_benchmark' AND lease_owner=%s", (owner,)),
        ])
        if rowcounts != [len(result)]:
            raise AssertionError(
                f"queue benchmark cleanup mismatch expected={[len(result)]} actual={rowcounts}"
            )
    return len(result), cleanup


WORKLOADS = {
    # Measure the latency-sensitive queue claim before the intentionally heavy
    # 1000-row import so every fresh run has the same uncontaminated start.
    "queue_claim_50": queue_claim_50,
    "complete_db": complete_db,
    "scan_db": scan_db,
    "return_db": return_db,
    "active_orders_first_page": active_orders_first_page,
    "admin_filtered_page": admin_filtered_page,
    "day_report": day_report,
    "dashboard_report": dashboard_report,
    "import_1000": import_1000,
}


def measure_workload(database_url, workload_name, context, iterations, warmup=10):
    function = WORKLOADS[workload_name]
    durations = []
    query_counts = []
    rows_returned = []
    engine = create_engine(database_url, pool_pre_ping=True)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    counter = {"enabled": False, "value": 0}

    @event.listens_for(engine, "before_cursor_execute")
    def count_query(_connection, _cursor, _statement, _parameters, _context, _executemany):
        if counter["enabled"]:
            counter["value"] += 1

    try:
        with psycopg.connect(psycopg_url(database_url)) as cleanup_connection:
            context["_benchmark_cleanup_connection"] = cleanup_connection
            maintenance = prepare_workload_maintenance(context, workload_name)
            for iteration in range(-warmup, iterations):
                cleanup = None
                with sessions() as db:
                    counter["value"] = 0
                    counter["enabled"] = True
                    with isolated_gc_sample():
                        started = time.perf_counter_ns()
                        try:
                            row_count, cleanup = function(db, context, iteration)
                        finally:
                            elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
                            counter["enabled"] = False
                if cleanup is not None:
                    cleanup()
                sample_ordinal = iteration + warmup + 1
                if maintenance and sample_ordinal % int(maintenance["interval"]) == 0:
                    for table_name in maintenance["tables"]:
                        if not benchmark_vacuum(context, table_name):
                            raise AssertionError("benchmark maintenance requires cleanup connection")
                if iteration >= 0:
                    durations.append(elapsed_ms)
                    query_counts.append(counter["value"])
                    rows_returned.append(row_count)
    finally:
        context.pop("_benchmark_cleanup_connection", None)
        engine.dispose()
    return {
        "iterations": iterations,
        "warmup_iterations": warmup,
        "durations_ms": [round(value, 3) for value in durations],
        "maintenance": {
            "exact_cleanup": True,
            "persistent_cleanup_connection": True,
            "cyclic_gc_during_sample": "disabled_after_pre_sample_collection",
            "pre_sample_quiescence_seconds": SAMPLE_QUIESCENCE_SECONDS,
            "autovacuum_during_samples": "disabled" if maintenance else "unchanged",
            "vacuum_analyze_outside_timer": bool(maintenance),
            "vacuum_interval_samples": int(maintenance["interval"]) if maintenance else 0,
            "vacuum_tables": list(maintenance["tables"]) if maintenance else [],
        },
        **summarize(durations),
        "query_count": {
            "min": min(query_counts),
            "median": percentile(query_counts, 50),
            "max": max(query_counts),
        },
        "rows_returned": {
            "min": min(rows_returned),
            "median": percentile(rows_returned, 50),
            "max": max(rows_returned),
        },
    }


def measure_profile_workloads(database_url, context, iterations):
    results = {}
    for workload_name in WORKLOADS:
        time.sleep(WORKLOAD_COOLDOWN_SECONDS)
        quiescence = wait_for_benchmark_quiescence()
        result = measure_workload(database_url, workload_name, context, iterations)
        result.setdefault("maintenance", {})["pre_workload_cooldown_seconds"] = WORKLOAD_COOLDOWN_SECONDS
        result["maintenance"]["pre_workload_quiescence"] = quiescence
        results[workload_name] = result
    return results


EXPLAIN_STATEMENTS = {
    "scan_db": (
        "SELECT order_id FROM order_items WHERE id = %s FOR UPDATE",
        lambda ctx: (ctx["scan_item"],),
    ),
    "complete_db": (
        "UPDATE orders SET status = 'completed', updated_at = now() WHERE id = %s RETURNING id",
        lambda ctx: (ctx["complete_order"],),
    ),
    "return_db": (
        "UPDATE orders SET status='returned', updated_at=now() WHERE id=%s AND status='completed' RETURNING id",
        lambda ctx: (ctx["return_order"],),
    ),
    "active_orders_first_page": (
        "SELECT id, order_date, client, status FROM orders WHERE status NOT IN "
        "('completed', 'done', 'closed', 'returned', 'archived_no_kiz', 'cancelled') "
        "ORDER BY order_date, created_at, id LIMIT 50",
        lambda _ctx: (),
    ),
    "admin_filtered_page": (
        "SELECT o.id, oi.id, o.client, oi.product, oi.status FROM orders o JOIN order_items oi ON oi.order_id = o.id "
        "WHERE o.order_date = %s AND oi.status = 'not_completed' "
        "ORDER BY o.order_date, o.created_at, oi.created_at, oi.id LIMIT 50",
        lambda _ctx: (SYNTHETIC_DATE,),
    ),
    "day_report": (
        "SELECT o.payment_type, o.status, count(DISTINCT o.id), count(DISTINCT oi.id), count(sc.id) "
        "FROM orders o JOIN order_items oi ON oi.order_id = o.id LEFT JOIN scan_codes sc ON sc.order_item_id = oi.id "
        "WHERE o.order_date = %s GROUP BY o.payment_type, o.status",
        lambda _ctx: (SYNTHETIC_DATE,),
    ),
    "dashboard_report": (
        "SELECT o.id, oi.id FROM orders o JOIN order_items oi ON oi.order_id=o.id "
        "WHERE o.status NOT IN ('returned','archived_no_kiz','cancelled') AND o.order_date=%s",
        lambda _ctx: (SYNTHETIC_DATE,),
    ),
    "import_1000": (
        "SELECT o.external_id, oi.raw_payload->>'item_key' FROM orders o "
        "JOIN order_items oi ON oi.order_id=o.id",
        lambda _ctx: (),
    ),
    "queue_claim_50": (
        "WITH candidates AS (SELECT id FROM pending_events WHERE event_type = 'synthetic_benchmark' "
        "AND status IN ('pending','failed') AND available_at <= now() ORDER BY available_at, created_at, id "
        "LIMIT 50 FOR UPDATE SKIP LOCKED) UPDATE pending_events pe SET status='processing', attempts=attempts+1, "
        "lease_owner='synthetic-explain', lease_expires_at=now()+interval '30 minutes', updated_at=now() "
        "FROM candidates c WHERE pe.id=c.id RETURNING pe.id",
        lambda _ctx: (),
    ),
}


def plan_accounting(node):
    rows = float(node.get("Actual Rows") or 0) * float(node.get("Actual Loops") or 1)
    buffers = 0
    for key, value in node.items():
        if key.endswith("Blocks") and isinstance(value, (int, float)):
            buffers += value
    for child in node.get("Plans") or []:
        child_rows, child_buffers = plan_accounting(child)
        rows += child_rows
        buffers += child_buffers
    return int(rows), int(buffers)


def plan_paths(node):
    node_types = []
    indexes = []

    def visit(current):
        node_type = str(current.get("Node Type") or "")
        index_name = str(current.get("Index Name") or "")
        if node_type and node_type not in node_types:
            node_types.append(node_type)
        if index_name and index_name not in indexes:
            indexes.append(index_name)
        for child in current.get("Plans") or []:
            visit(child)

    visit(node)
    return node_types, indexes


def capture_explain(database_url, profile_name, context):
    plans = {}
    summaries = {}
    with psycopg.connect(psycopg_url(database_url)) as connection:
        for workload, (statement, params_factory) in EXPLAIN_STATEMENTS.items():
            try:
                row = connection.execute(
                    "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + statement,
                    params_factory(context),
                ).fetchone()
                payload = row[0]
                root = payload[0]["Plan"]
                rows_examined, buffers_examined = plan_accounting(root)
                node_types, indexes = plan_paths(root)
                plans[workload] = payload
                summaries[workload] = {
                    "execution_time_ms": round(float(payload[0].get("Execution Time") or 0), 3),
                    "planning_time_ms": round(float(payload[0].get("Planning Time") or 0), 3),
                    "rows_examined": rows_examined,
                    "buffers_examined": buffers_examined,
                    "node_types": node_types,
                    "indexes": indexes,
                }
            finally:
                connection.rollback()
    evidence = {
        "schema": 1,
        "profile": profile_name,
        "synthetic_only": True,
        "explain": "ANALYZE, BUFFERS, FORMAT JSON",
        "write_plans_disposable_and_rolled_back": True,
        "summaries": summaries,
        "plans": plans,
    }
    path = EVIDENCE_DIR / f"explain-{profile_name}.json"
    path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return evidence, path


def host_manifest(database_url, runtime):
    with psycopg.connect(psycopg_url(database_url)) as connection:
        pg_version = connection.execute("SHOW server_version").fetchone()[0]
    try:
        cpu = subprocess.run(
            ["sysctl", "-n", "machdep.cpu.brand_string"], text=True, capture_output=True, check=False
        ).stdout.strip() or platform.processor()
        ram_bytes = int(subprocess.run(
            ["sysctl", "-n", "hw.memsize"], text=True, capture_output=True, check=False
        ).stdout.strip() or 0)
    except (OSError, ValueError):
        cpu = platform.processor()
        ram_bytes = 0
    commit = run_command(["git", "rev-parse", "HEAD"]).strip()
    return {
        "os": platform.system(),
        "os_release": platform.release(),
        "architecture": platform.machine(),
        "cpu": cpu,
        "cpu_count": os.cpu_count(),
        "ram_bytes": ram_bytes,
        "python": platform.python_version(),
        "postgres": pg_version,
        "postgres_image": runtime["image"],
        "commit": commit,
        "working_tree_source_hashes": benchmark_contract_hashes(),
    }


def assertion_failures(results, budgets, approved=None):
    failures = []
    for workload, limits in budgets["workloads"].items():
        metrics = results[workload]
        for metric, limit in limits.items():
            if float(metrics[metric]) > float(limit):
                failures.append(f"{workload}.{metric}={metrics[metric]} exceeds absolute budget {limit}")
    if approved:
        limit_factor = 1 + float(budgets["regression_limit_percent"]) / 100
        for workload, metrics in results.items():
            previous = (approved.get("results") or {}).get(workload) or {}
            for metric in ("p95_ms", "p99_ms"):
                if metric in previous and float(metrics[metric]) > float(previous[metric]) * limit_factor:
                    failures.append(
                        f"{workload}.{metric}={metrics[metric]} exceeds approved {previous[metric]} by more than "
                        f"{budgets['regression_limit_percent']}%"
                    )
    return failures


def aggregate_regression_failures(runs, budgets, approved):
    failures = []
    medians = {}
    limit_factor = 1 + float(budgets["regression_limit_percent"]) / 100
    for workload in WORKLOADS:
        medians[workload] = {}
        previous = (approved.get("results") or {}).get(workload) or {}
        for metric in ("p95_ms", "p99_ms"):
            values = [float(run["results"][workload][metric]) for run in runs]
            median = float(percentile(values, 50))
            medians[workload][metric] = round(median, 3)
            if metric in previous and median > float(previous[metric]) * limit_factor:
                failures.append(
                    f"aggregate median {workload}.{metric}={median:.3f} exceeds approved "
                    f"{previous[metric]} by more than {budgets['regression_limit_percent']}%"
                )
    return failures, medians


def aggregate_baseline_results(run_results):
    aggregated = {}
    for workload in WORKLOADS:
        first = run_results[0][workload]
        metrics = {
            "iterations": first["iterations"],
            "warmup_iterations": first["warmup_iterations"],
        }
        for metric in ("p50_ms", "p95_ms", "p99_ms", "max_ms"):
            metrics[metric] = round(
                float(percentile([run[workload][metric] for run in run_results], 50)),
                3,
            )
        for metric in ("query_count", "rows_returned"):
            metrics[metric] = {
                key: int(percentile([run[workload][metric][key] for run in run_results], 50))
                for key in ("min", "median", "max")
            }
        aggregated[workload] = metrics
    return aggregated


def run_baseline(profile_name, iterations, repeat):
    if iterations < 100:
        raise ValueError("baseline requires at least 100 measured iterations")
    if repeat < 3:
        raise ValueError("baseline requires at least three independent runs")
    profiles = load_json(PROFILES_PATH)
    budgets = load_json(BUDGETS_PATH)
    task_policy = ensure_foreground_task_policy()
    measured_runs = []
    run_evidence = []
    for run_number in range(1, repeat + 1):
        time.sleep(FRESH_RUN_COOLDOWN_SECONDS)
        quiescence = wait_for_benchmark_quiescence()
        with disposable_database() as (database_url, runtime):
            dataset, dataset_path = seed_profile(database_url, profile_name)
            prepare_profile_benchmark(database_url)
            profile = profiles["profiles"][profile_name]
            context = workload_context(database_url, profile)
            run_results = measure_profile_workloads(database_url, context, iterations)
            explain, explain_path = capture_explain(database_url, profile_name, context)
            host = host_manifest(database_url, runtime)
        measured_runs.append(run_results)
        run_evidence.append({
            "run": run_number,
            "quiescence": quiescence,
            "results": run_results,
        })
    results = aggregate_baseline_results(measured_runs)
    baseline_path = EVIDENCE_DIR / "backend-baseline-approved.json"
    approved = load_json(baseline_path) if baseline_path.exists() else None
    compatibility_failures = baseline_compatibility_failures(approved) if approved else []
    failures = assertion_failures(
        results,
        budgets,
        approved=None if compatibility_failures else approved,
    )
    evidence = {
        "schema": 1,
        "mode": "baseline",
        "profile": profile_name,
        "seed": profile["seed"],
        "dataset_manifest": str(dataset_path.relative_to(ROOT)),
        "explain_evidence": str(explain_path.relative_to(ROOT)),
        "host": host,
        "task_policy": task_policy,
        "repeat": repeat,
        "aggregation": "nearest-rank median across independent runs",
        "runs": run_evidence,
        "results": results,
        "explain_summaries": explain["summaries"],
        "budgets": budgets,
        "assertions": {"status": "pass" if not failures else "fail", "failures": failures},
        "baseline_compatibility": {
            "approved_compatible": bool(approved) and not compatibility_failures,
            "failures": compatibility_failures,
            "reapproval_required": bool(approved) and bool(compatibility_failures),
        },
    }
    result_path = EVIDENCE_DIR / "backend-baseline-reference.json"
    result_path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if approved is None and not failures:
        baseline_path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    summary = {
        "status": evidence["assertions"]["status"],
        "profile": profile_name,
        "iterations": iterations,
        "repeat": repeat,
        "dataset_counts": dataset["table_counts"],
        "results": {name: {key: value for key, value in metrics.items() if key in ("p50_ms", "p95_ms", "p99_ms", "query_count")}
                    for name, metrics in results.items()},
        "evidence": str(result_path.relative_to(ROOT)),
        "failures": failures,
    }
    sys.stdout.write(json.dumps(summary, ensure_ascii=False, sort_keys=True) + "\n")
    return 0 if not failures else 1


def approve_baseline(reason):
    reason = str(reason or "").strip()
    if len(reason) < 12:
        raise ValueError("baseline approval reason must be at least 12 characters")
    candidate_path = EVIDENCE_DIR / "backend-baseline-reference.json"
    approved_path = EVIDENCE_DIR / "backend-baseline-approved.json"
    if not candidate_path.is_file():
        raise RuntimeError("baseline candidate is missing; run baseline first")
    candidate = load_json(candidate_path)
    errors = []
    if candidate.get("mode") != "baseline" or candidate.get("profile") != "reference":
        errors.append("candidate is not a reference baseline")
    if (candidate.get("assertions") or {}).get("status") != "pass":
        errors.append("candidate assertions did not pass")
    errors.extend(baseline_compatibility_failures(candidate))
    errors.extend(assertion_failures(candidate.get("results") or {}, load_json(BUDGETS_PATH)))
    current_commit = run_command(["git", "rev-parse", "HEAD"]).strip()
    if (candidate.get("host") or {}).get("commit") != current_commit:
        errors.append("candidate commit does not match HEAD")
    if errors:
        raise RuntimeError("baseline candidate cannot be approved: " + "; ".join(errors))

    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    previous_sha256 = sha256_file(approved_path) if approved_path.is_file() else None
    if approved_path.is_file():
        history_dir = EVIDENCE_DIR / "approved-baseline-history"
        history_dir.mkdir(parents=True, exist_ok=True)
        history_path = history_dir / f"{previous_sha256}.json"
        if not history_path.exists():
            history_path.write_bytes(approved_path.read_bytes())
    candidate_sha256 = sha256_file(candidate_path)
    temporary = approved_path.with_suffix(".json.partial")
    temporary.write_bytes(candidate_path.read_bytes())
    os.replace(temporary, approved_path)
    approval = {
        "schema": 1,
        "approved_commit": current_commit,
        "approved_sha256": candidate_sha256,
        "previous_sha256": previous_sha256,
        "reason": reason,
        "contract_hashes": benchmark_contract_hashes(),
    }
    approval_path = EVIDENCE_DIR / "baseline-approval.json"
    approval_path.write_text(
        json.dumps(approval, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    sys.stdout.write(json.dumps({
        "status": "approved",
        "commit": current_commit,
        "approved_sha256": candidate_sha256,
        "previous_sha256": previous_sha256,
        "evidence": str(approval_path.relative_to(ROOT)),
    }, ensure_ascii=False, sort_keys=True) + "\n")
    return 0


def run_seed(profile_name):
    with disposable_database() as (database_url, _runtime):
        manifest, path = seed_profile(database_url, profile_name)
    sys.stdout.write(json.dumps({
        "status": "pass",
        "profile": profile_name,
        "seed": manifest["seed"],
        "table_counts": manifest["table_counts"],
        "hot_working_set": manifest["hot_working_set"],
        "production_scale_status": manifest["production_scale_status"],
        "evidence": str(path.relative_to(ROOT)),
    }, ensure_ascii=False, sort_keys=True) + "\n")
    return 0


def run_explain(profile_name):
    profiles = load_json(PROFILES_PATH)
    with disposable_database() as (database_url, _runtime):
        seed_profile(database_url, profile_name)
        context = workload_context(database_url, profiles["profiles"][profile_name])
        evidence, path = capture_explain(database_url, profile_name, context)
    sys.stdout.write(json.dumps({
        "status": "pass",
        "profile": profile_name,
        "format": "json",
        "summaries": evidence["summaries"],
        "evidence": str(path.relative_to(ROOT)),
    }, ensure_ascii=False, sort_keys=True) + "\n")
    return 0


def run_compare(workload):
    workload_name = {"import": "import_1000"}[workload]
    profiles = load_json(PROFILES_PATH)
    budgets = load_json(BUDGETS_PATH)
    approved_path = EVIDENCE_DIR / "backend-baseline-approved.json"
    if not approved_path.exists():
        raise RuntimeError("approved Phase 6 baseline is missing; run baseline first")
    approved = load_json(approved_path)
    profile = profiles["profiles"]["reference"]
    with disposable_database() as (database_url, runtime):
        seed_profile(database_url, "reference")
        context = workload_context(database_url, profile)
        result = measure_workload(database_url, workload_name, context, 100)
        host = host_manifest(database_url, runtime)
    previous = approved["results"][workload_name]
    delta = {
        metric: round(float(result[metric]) - float(previous[metric]), 3)
        for metric in ("p50_ms", "p95_ms", "p99_ms")
    }
    delta_percent = {
        metric: round(delta[metric] / float(previous[metric]) * 100, 3)
        for metric in delta
    }
    failures = assertion_failures(
        {name: result if name == workload_name else approved["results"][name]
         for name in budgets["workloads"]},
        budgets,
        approved=approved,
    )
    evidence = {
        "schema": 1,
        "mode": "compare",
        "workload": workload_name,
        "profile": "reference",
        "iterations": 100,
        "host": host,
        "approved": {key: previous[key] for key in ("p50_ms", "p95_ms", "p99_ms", "query_count")},
        "current": result,
        "delta_ms": delta,
        "delta_percent": delta_percent,
        "absolute_budget": budgets["workloads"][workload_name],
        "remaining_bottleneck": (
            f"{result['query_count']['median']} SQL statements per 1000-row import"
            if result["query_count"]["median"] > 1000 else "none"
        ),
        "status": "pass" if not failures else "fail",
        "failures": failures,
    }
    path = EVIDENCE_DIR / f"compare-{workload}.json"
    path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    sys.stdout.write(json.dumps({
        "status": evidence["status"],
        "workload": workload_name,
        "approved": evidence["approved"],
        "current": {key: result[key] for key in ("p50_ms", "p95_ms", "p99_ms", "query_count")},
        "delta_percent": delta_percent,
        "remaining_bottleneck": evidence["remaining_bottleneck"],
        "evidence": str(path.relative_to(ROOT)),
        "failures": failures,
    }, ensure_ascii=False, sort_keys=True) + "\n")
    return 0 if not failures else 1


def compare_metric_snapshot(metrics):
    return {
        key: metrics[key]
        for key in ("p50_ms", "p95_ms", "p99_ms", "query_count", "rows_returned")
    }


def run_profile_compare(profile_name, repeat, assert_budgets):
    if repeat < 1:
        raise ValueError("compare repeat must be at least 1")
    profiles = load_json(PROFILES_PATH)
    budgets = load_json(BUDGETS_PATH)
    approved_path = EVIDENCE_DIR / "backend-baseline-approved.json"
    if not approved_path.exists():
        raise RuntimeError("approved Phase 6 baseline is missing; run baseline first")
    approved = load_json(approved_path)
    compatibility_failures = baseline_compatibility_failures(approved)
    if compatibility_failures:
        raise RuntimeError(
            "approved performance baseline is incompatible; run and approve a fresh baseline: "
            + "; ".join(compatibility_failures)
        )
    task_policy = ensure_foreground_task_policy()
    profile = profiles["profiles"][profile_name]
    runs = []
    failures = []
    for run_number in range(1, repeat + 1):
        time.sleep(FRESH_RUN_COOLDOWN_SECONDS)
        run_quiescence = wait_for_benchmark_quiescence()
        with disposable_database() as (database_url, runtime):
            dataset, dataset_path = seed_profile(database_url, profile_name)
            prepare_profile_benchmark(database_url)
            context = workload_context(database_url, profile)
            results = measure_profile_workloads(database_url, context, 100)
            host = host_manifest(database_url, runtime)
        run_failures = assertion_failures(results, budgets)
        failures.extend(f"run {run_number}: {failure}" for failure in run_failures)
        runs.append({
            "run": run_number,
            "dataset_manifest": str(dataset_path.relative_to(ROOT)),
            "dataset_counts": dataset["table_counts"],
            "host": host,
            "fresh_run_cooldown_seconds": FRESH_RUN_COOLDOWN_SECONDS,
            "fresh_run_quiescence": run_quiescence,
            "results": results,
            "failures": run_failures,
        })

    aggregate_failures, aggregate_medians = aggregate_regression_failures(runs, budgets, approved)
    failures.extend(aggregate_failures)
    summaries = {}
    for workload in WORKLOADS:
        previous = approved["results"][workload]
        workload_runs = [compare_metric_snapshot(run["results"][workload]) for run in runs]
        summaries[workload] = {
            "approved": compare_metric_snapshot(previous),
            "runs": workload_runs,
            "delta_percent": [
                {
                    metric: round(
                        (float(metrics[metric]) - float(previous[metric]))
                        / float(previous[metric]) * 100,
                        3,
                    )
                    for metric in ("p50_ms", "p95_ms", "p99_ms")
                }
                for metrics in workload_runs
            ],
        }
    enforced_failures = failures if assert_budgets else []
    evidence = {
        "schema": 1,
        "mode": "profile_compare",
        "profile": profile_name,
        "repeat": repeat,
        "iterations_per_workload_per_run": 100,
        "assert_budgets": bool(assert_budgets),
        "task_policy": task_policy,
        "approved_evidence": str(approved_path.relative_to(ROOT)),
        "aggregate_regression": {
            "method": "nearest-rank median across three independent run percentiles",
            "regression_limit_percent": budgets["regression_limit_percent"],
            "medians": aggregate_medians,
            "failures": aggregate_failures,
        },
        "summaries": summaries,
        "runs": runs,
        "status": "pass" if not enforced_failures else "fail",
        "failures": enforced_failures,
    }
    path = EVIDENCE_DIR / f"compare-{profile_name}.json"
    path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    sys.stdout.write(json.dumps({
        "status": evidence["status"],
        "profile": profile_name,
        "repeat": repeat,
        "iterations_per_workload_per_run": 100,
        "summaries": summaries,
        "evidence": str(path.relative_to(ROOT)),
        "failures": enforced_failures,
    }, ensure_ascii=False, sort_keys=True) + "\n")
    return 0 if not enforced_failures else 1


def validate_manifest():
    profiles = load_json(PROFILES_PATH)
    budgets = load_json(BUDGETS_PATH)
    errors = []
    contract = profiles.get("synthetic_contract") or {}
    if contract.get("contains_real_data") is not False or not str(contract.get("namespace") or "").startswith("SYNTHETIC"):
        errors.append("synthetic profile contract is not fail closed")
    for name, profile in (profiles.get("profiles") or {}).items():
        counts = expected_counts(profile)
        positive_tables = ("orders", "order_items", "scan_codes", "pending_events", "imports", "import_files")
        if not all(int(counts[table]) > 0 for table in positive_tables):
            errors.append(f"profile {name} has non-positive required cardinality")
        targets = sum(int(profile[key]) for key in ("scan_targets", "complete_targets", "return_targets"))
        if targets > int(profile["orders"]):
            errors.append(f"profile {name} target hot sets exceed order cardinality")
        if counts["audit_log"] != 0:
            errors.append(f"profile {name} must not seed audit rows")
    if float(budgets.get("regression_limit_percent") or 0) != 10:
        errors.append("regression limit must be exactly 10%")
    required = (
        EVIDENCE_DIR / "dataset-reference.json",
        EVIDENCE_DIR / "backend-baseline-reference.json",
        EVIDENCE_DIR / "explain-stress.json",
    )
    for path in required:
        if not path.exists():
            errors.append(f"missing evidence: {path.relative_to(ROOT)}")
    if (EVIDENCE_DIR / "backend-baseline-reference.json").exists():
        baseline = load_json(EVIDENCE_DIR / "backend-baseline-reference.json")
        if set((baseline.get("results") or {}).keys()) != set(WORKLOADS):
            errors.append("baseline workload set mismatch")
        for workload, result in (baseline.get("results") or {}).items():
            if int(result.get("iterations") or 0) < 100:
                errors.append(f"{workload} has fewer than 100 measured iterations")
            for key in ("p50_ms", "p95_ms", "p99_ms", "query_count", "rows_returned"):
                if key not in result:
                    errors.append(f"{workload} missing {key}")
        errors.extend(assertion_failures(baseline.get("results") or {}, budgets))
    status = "pass" if not errors else "fail"
    sys.stdout.write(json.dumps({
        "status": status,
        "profiles": sorted((profiles.get("profiles") or {}).keys()),
        "regression_limit_percent": budgets.get("regression_limit_percent"),
        "evidence": [str(path.relative_to(ROOT)) for path in required],
        "errors": errors,
    }, ensure_ascii=False, sort_keys=True) + "\n")
    return 0 if not errors else 1


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    seed_parser = subparsers.add_parser("seed")
    seed_parser.add_argument("--profile", choices=("small", "reference", "stress"), required=True)
    baseline_parser = subparsers.add_parser("baseline")
    baseline_parser.add_argument("--profile", choices=("small", "reference", "stress"), required=True)
    baseline_parser.add_argument("--iterations", type=int, default=100)
    baseline_parser.add_argument("--repeat", type=int, default=3)
    explain_parser = subparsers.add_parser("explain")
    explain_parser.add_argument("--profile", choices=("small", "reference", "stress"), required=True)
    explain_parser.add_argument("--format", choices=("json",), default="json")
    compare_parser = subparsers.add_parser("compare")
    compare_parser.add_argument("--workload", choices=("import",))
    compare_parser.add_argument("--profile", choices=("reference",))
    compare_parser.add_argument("--repeat", type=int, default=1)
    compare_parser.add_argument("--assert-budgets", action="store_true")
    approve_parser = subparsers.add_parser("approve-baseline")
    approve_parser.add_argument("--reason", required=True)
    subparsers.add_parser("validate-manifest")
    return parser.parse_args()


def main():
    args = parse_args()
    if args.command == "seed":
        return run_seed(args.profile)
    if args.command == "baseline":
        return run_baseline(args.profile, args.iterations, args.repeat)
    if args.command == "explain":
        return run_explain(args.profile)
    if args.command == "compare":
        if args.profile:
            if args.workload:
                raise ValueError("compare accepts either --profile or --workload, not both")
            return run_profile_compare(args.profile, args.repeat, args.assert_budgets)
        if not args.workload:
            raise ValueError("compare requires --profile or --workload")
        return run_compare(args.workload)
    if args.command == "approve-baseline":
        return approve_baseline(args.reason)
    return validate_manifest()


if __name__ == "__main__":
    raise SystemExit(main())
