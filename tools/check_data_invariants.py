#!/usr/bin/env python3
"""Count-only PostgreSQL invariant preflight; never repairs warehouse data."""

from __future__ import annotations

import argparse
import json
import sys
from contextlib import contextmanager

import psycopg
from sqlalchemy.engine import make_url


INVARIANTS = (
    ("order_item_negative_quantities", "SELECT count(*) FROM order_items WHERE quantity_pieces < 0 OR quantity_blocks < 0 OR scanned_blocks < 0"),
    ("order_item_nonpositive_pieces_per_block", "SELECT count(*) FROM order_items WHERE pieces_per_block IS NOT NULL AND pieces_per_block <= 0"),
    ("order_item_scanned_exceeds_plan", "SELECT count(*) FROM order_items WHERE scanned_blocks > quantity_blocks"),
    ("unsupported_order_status", "SELECT count(*) FROM orders WHERE status NOT IN ('not_completed','completed','done','closed','returned','archived_no_kiz','cancelled')"),
    ("unsupported_item_status", "SELECT count(*) FROM order_items WHERE status NOT IN ('not_completed','completed','done','closed','returned','removed_from_google_sheet','archived_no_kiz','cancelled')"),
    ("unsupported_import_status", "SELECT count(*) FROM imports WHERE status NOT IN ('created','completed','completed_with_errors','failed')"),
    ("invalid_import_row_counts", "SELECT count(*) FROM imports WHERE rows_total < 0 OR rows_imported < 0 OR rows_imported > rows_total"),
    ("unsupported_event_status", "SELECT count(*) FROM pending_events WHERE status NOT IN ('pending','failed','error','processing','completed','blocked','dead','cancelled','active','waiting_shipment_date','waiting_date_choice')"),
    ("negative_event_attempts", "SELECT count(*) FROM pending_events WHERE attempts < 0"),
    ("source_identity_pair_mismatch", "SELECT count(*) FROM order_items WHERE (source_import_id IS NULL) <> (source_import_key IS NULL)"),
    ("blank_materialized_identity", "SELECT (SELECT count(*) FROM orders WHERE btrim(coalesce(import_order_key,''))='' AND import_order_key IS NOT NULL OR btrim(coalesce(import_source_order_key,''))='' AND import_source_order_key IS NOT NULL) + (SELECT count(*) FROM order_items WHERE btrim(coalesce(import_item_key,''))='' AND import_item_key IS NOT NULL OR btrim(coalesce(source_import_key,''))='' AND source_import_key IS NOT NULL)"),
    ("duplicate_active_order_identity", "SELECT count(*) FROM (SELECT import_order_key FROM orders WHERE import_order_key IS NOT NULL AND lower(status)<>'returned' AND lower(coalesce(raw_payload->>'return_status','')) NOT IN ('returned','return','возврат') GROUP BY import_order_key HAVING count(*)>1) conflicts"),
    ("duplicate_order_source_identity", "SELECT count(*) FROM (SELECT order_id, source_import_key FROM order_items WHERE source_import_key IS NOT NULL GROUP BY order_id, source_import_key HAVING count(*)>1) conflicts"),
    ("duplicate_order_item_fallback_identity", "SELECT count(*) FROM (SELECT order_id, import_item_key FROM order_items WHERE source_import_key IS NULL AND import_item_key IS NOT NULL GROUP BY order_id, import_item_key HAVING count(*)>1) conflicts"),
)

TABLES = ("orders", "order_items", "imports", "import_files", "pending_events")


def psycopg_url(database_url):
    return make_url(database_url).set(drivername="postgresql").render_as_string(hide_password=False)


def table_counts(connection):
    return {table: int(connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]) for table in TABLES}


def run_preflight(database_url):
    with psycopg.connect(psycopg_url(database_url)) as connection:
        connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        read_only = connection.execute("SHOW transaction_read_only").fetchone()[0]
        before = table_counts(connection)
        counts = {name: int(connection.execute(statement).fetchone()[0]) for name, statement in INVARIANTS}
        after = table_counts(connection)
        connection.rollback()
    violations = sum(counts.values())
    return {
        "status": "pass" if violations == 0 else "violations",
        "transaction_read_only": read_only,
        "invariants": counts,
        "violation_classes": sum(value > 0 for value in counts.values()),
        "violations": violations,
        "table_counts_before": before,
        "table_counts_after": after,
        "zero_mutation": before == after and read_only == "on",
        "automatic_repairs": 0,
    }


@contextmanager
def resolved_database(value):
    if value != "test-harness":
        yield value
        return
    from tools.benchmark_backend import disposable_database, seed_profile

    with disposable_database() as (database_url, _runtime):
        seed_profile(database_url, "reference")
        yield database_url


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--read-only", action="store_true")
    mode.add_argument("--apply-gate", action="store_true")
    return parser.parse_args()


def main():
    args = parse_args()
    with resolved_database(args.database_url) as database_url:
        report = run_preflight(database_url)
    report["mode"] = "apply_gate" if args.apply_gate else "read_only"
    report["ddl_executed"] = False
    sys.stdout.write(json.dumps(report, ensure_ascii=False, sort_keys=True) + "\n")
    return 0 if report["status"] == "pass" and report["zero_mutation"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
