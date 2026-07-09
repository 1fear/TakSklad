#!/usr/bin/env python3
"""Bounded dry-run-first backfill for normalized import identity columns."""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from contextlib import contextmanager

from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

from backend.app.imports_service import normalize_text, source_import_lookup_key
from backend.app.models import Order, OrderItem


RETURN_MARKERS = {"returned", "return", "возврат"}


def parse_cursor(value):
    value = normalize_text(value)
    return uuid.UUID(value) if value else None


def is_active_order(order):
    raw_payload = order.raw_payload or {}
    return (
        normalize_text(order.status).casefold() != "returned"
        and normalize_text(raw_payload.get("return_status")).casefold() not in RETURN_MARKERS
    )


def expected_order_identity(order):
    raw_payload = order.raw_payload or {}
    resolved = normalize_text(raw_payload.get("order_key")) or normalize_text(order.external_id)
    source = normalize_text(raw_payload.get("source_order_key")) or resolved
    return resolved or None, source or None


def expected_item_identity(item):
    raw_payload = item.raw_payload or {}
    item_key = normalize_text(raw_payload.get("item_key")) or None
    source_import_id = normalize_text(raw_payload.get("source_import_id")) or None
    source_batch_key = normalize_text(raw_payload.get("source_batch_key")) or None
    return item_key, source_import_lookup_key(source_import_id), source_import_id, source_batch_key


def mismatch(conflicts, table, row_id, field, current, expected):
    if current is not None and current != expected:
        conflicts.append({
            "type": "prefilled_mismatch",
            "table": table,
            "id": str(row_id),
            "field": field,
            "current": current,
            "expected": expected,
        })


def scan_rows(session, model, *, after_id, batch_size, max_batches, inspect):
    processed = 0
    candidates = 0
    cursor = after_id
    conflicts = []
    for _batch in range(max_batches):
        statement = select(model).order_by(model.id).limit(batch_size)
        if cursor is not None:
            statement = statement.where(model.id > cursor)
        rows = session.execute(statement).scalars().all()
        if not rows:
            return processed, candidates, cursor, True, conflicts
        for row in rows:
            candidates += inspect(row, conflicts)
        processed += len(rows)
        cursor = rows[-1].id
        if len(rows) < batch_size:
            return processed, candidates, cursor, True, conflicts
        session.expunge_all()
    return processed, candidates, cursor, False, conflicts


def inspect_order(order, conflicts):
    resolved, source = expected_order_identity(order)
    if not resolved:
        conflicts.append({"type": "unresolved_identity", "table": "orders", "id": str(order.id)})
        return 0
    if len(resolved) > 120 or (source and len(source) > 120):
        conflicts.append({"type": "overlength_identity", "table": "orders", "id": str(order.id)})
        return 0
    mismatch(conflicts, "orders", order.id, "import_order_key", order.import_order_key, resolved)
    mismatch(
        conflicts, "orders", order.id, "import_source_order_key", order.import_source_order_key, source,
    )
    return int(order.import_order_key is None or order.import_source_order_key is None)


def inspect_item(item, conflicts):
    item_key, source_key, source_id, batch_key = expected_item_identity(item)
    if not item_key and not source_id:
        conflicts.append({"type": "unresolved_identity", "table": "order_items", "id": str(item.id)})
        return 0
    mismatch(conflicts, "order_items", item.id, "import_item_key", item.import_item_key, item_key)
    mismatch(conflicts, "order_items", item.id, "source_import_key", item.source_import_key, source_key)
    mismatch(conflicts, "order_items", item.id, "source_import_id", item.source_import_id, source_id)
    mismatch(conflicts, "order_items", item.id, "source_batch_key", item.source_batch_key, batch_key)
    return int(
        item.import_item_key is None
        or item.source_import_key is None and source_key is not None
        or item.source_import_id is None and source_id is not None
        or item.source_batch_key is None and batch_key is not None
    )


def duplicate_conflicts(session, order_keys, item_identities):
    active_filter = (
        "lower(o.status) <> 'returned' AND lower(coalesce(o.raw_payload->>'return_status', '')) "
        "NOT IN ('returned', 'return', 'возврат')"
    )
    statements = []
    if order_keys:
        statements.append((
            "duplicate_active_order_key",
            "SELECT coalesce(o.import_order_key, o.raw_payload->>'order_key', o.external_id) AS identity, "
            "array_agg(o.id::text ORDER BY o.id) AS ids, count(*) AS total FROM orders o WHERE "
            + active_filter
            + " AND coalesce(o.import_order_key, o.raw_payload->>'order_key', o.external_id) "
            "= ANY(CAST(:identities AS text[])) GROUP BY identity HAVING count(*) > 1",
            sorted(order_keys),
        ))
    if item_identities:
        statements.append((
            "duplicate_active_item_identity",
            "SELECT CASE WHEN coalesce(oi.source_import_id, oi.raw_payload->>'source_import_id', '') <> '' "
            "THEN 'source:' || coalesce(oi.source_import_id, oi.raw_payload->>'source_import_id') "
            "ELSE 'item:' || coalesce(oi.import_item_key, oi.raw_payload->>'item_key') END AS identity, "
            "array_agg(oi.id::text ORDER BY oi.id) AS ids, count(*) AS total "
            "FROM order_items oi JOIN orders o ON o.id=oi.order_id WHERE "
            + active_filter
            + " AND (CASE WHEN coalesce(oi.source_import_id, oi.raw_payload->>'source_import_id', '') <> '' "
            "THEN 'source:' || coalesce(oi.source_import_id, oi.raw_payload->>'source_import_id') "
            "ELSE 'item:' || coalesce(oi.import_item_key, oi.raw_payload->>'item_key') END) "
            "= ANY(CAST(:identities AS text[])) GROUP BY identity HAVING count(*) > 1",
            sorted(item_identities),
        ))
    conflicts = []
    for conflict_type, statement, identities in statements:
        for row in session.execute(text(statement), {"identities": identities}).mappings():
            conflicts.append({
                "type": conflict_type,
                "identity": row["identity"],
                "ids": list(row["ids"]),
                "total": int(row["total"]),
            })
    return conflicts


def analyze(database_url, *, batch_size, max_batches, after_order_id=None, after_item_id=None):
    engine = create_engine(database_url, pool_pre_ping=True)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        with sessions() as session:
            order_keys = set()
            item_identities = set()

            def inspect_scoped_order(order, conflicts):
                resolved, _source = expected_order_identity(order)
                if resolved and is_active_order(order):
                    order_keys.add(resolved)
                return inspect_order(order, conflicts)

            def inspect_scoped_item(item, conflicts):
                item_key, _source_key, source_id, _batch_key = expected_item_identity(item)
                identity = f"source:{source_id}" if source_id else f"item:{item_key}" if item_key else ""
                if identity:
                    item_identities.add(identity)
                return inspect_item(item, conflicts)

            order_scan = scan_rows(
                session, Order, after_id=parse_cursor(after_order_id), batch_size=batch_size,
                max_batches=max_batches, inspect=inspect_scoped_order,
            )
            item_scan = scan_rows(
                session, OrderItem, after_id=parse_cursor(after_item_id), batch_size=batch_size,
                max_batches=max_batches, inspect=inspect_scoped_item,
            )
            conflicts = order_scan[4] + item_scan[4] + duplicate_conflicts(
                session, order_keys, item_identities,
            )
            session.rollback()
        return {
            "orders_processed": order_scan[0],
            "orders_candidates": order_scan[1],
            "orders_complete": order_scan[3],
            "items_processed": item_scan[0],
            "items_candidates": item_scan[1],
            "items_complete": item_scan[3],
            "next_after_order_id": str(order_scan[2] or ""),
            "next_after_item_id": str(item_scan[2] or ""),
            "conflicts": conflicts,
        }
    finally:
        engine.dispose()


def apply_backfill(
    database_url, *, batch_size, max_batches, after_order_id=None, after_item_id=None,
):
    engine = create_engine(database_url, pool_pre_ping=True)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    updated_orders = 0
    updated_items = 0
    try:
        with sessions() as session:
            order_cursor = parse_cursor(after_order_id)
            orders_complete = False
            for _batch in range(max_batches):
                statement = select(Order).order_by(Order.id).limit(batch_size)
                if order_cursor is not None:
                    statement = statement.where(Order.id > order_cursor)
                rows = session.execute(statement).scalars().all()
                if not rows:
                    orders_complete = True
                    break
                for order in rows:
                    resolved, source = expected_order_identity(order)
                    changed = False
                    if order.import_order_key is None and resolved is not None:
                        order.import_order_key = resolved
                        changed = True
                    if order.import_source_order_key is None and source is not None:
                        order.import_source_order_key = source
                        changed = True
                    updated_orders += int(changed)
                order_cursor = rows[-1].id
                session.commit()
                session.expunge_all()
                if len(rows) < batch_size:
                    orders_complete = True
                    break

            item_cursor = parse_cursor(after_item_id)
            items_complete = False
            for _batch in range(max_batches):
                statement = select(OrderItem).order_by(OrderItem.id).limit(batch_size)
                if item_cursor is not None:
                    statement = statement.where(OrderItem.id > item_cursor)
                rows = session.execute(statement).scalars().all()
                if not rows:
                    items_complete = True
                    break
                for item in rows:
                    item_key, source_key, source_id, batch_key = expected_item_identity(item)
                    changed = False
                    if item.import_item_key is None and item_key is not None:
                        item.import_item_key = item_key
                        changed = True
                    if item.source_import_key is None and source_key is not None:
                        item.source_import_key = source_key
                        changed = True
                    if item.source_import_id is None and source_id is not None:
                        item.source_import_id = source_id
                        changed = True
                    if item.source_batch_key is None and batch_key is not None:
                        item.source_batch_key = batch_key
                        changed = True
                    updated_items += int(changed)
                item_cursor = rows[-1].id
                session.commit()
                session.expunge_all()
                if len(rows) < batch_size:
                    items_complete = True
                    break
        return {
            "updated_orders": updated_orders,
            "updated_items": updated_items,
            "orders_complete": orders_complete,
            "items_complete": items_complete,
            "next_after_order_id": str(order_cursor or ""),
            "next_after_item_id": str(item_cursor or ""),
        }
    finally:
        engine.dispose()


def seed_test_harness(database_url):
    engine = create_engine(database_url, pool_pre_ping=True)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        with sessions() as session:
            active = Order(
                source="synthetic_backfill", external_id="synthetic-legacy-order", payment_type="synthetic",
                client="SYNTHETIC BACKFILL CLIENT", address="SYNTHETIC BACKFILL ADDRESS",
                status="not_completed", raw_payload={"order_key": "synthetic-legacy-order"},
            )
            active.items.append(OrderItem(
                product="SYNTHETIC PRODUCT", quantity_pieces=10, quantity_blocks=1,
                status="not_completed", raw_payload={
                    "item_key": "a" * 64, "source_import_id": "synthetic-import-row-1",
                    "source_batch_key": "synthetic-batch-1",
                },
            ))
            returned = Order(
                source="synthetic_backfill", external_id="synthetic-returned-order", payment_type="synthetic",
                client="SYNTHETIC RETURNED CLIENT", address="SYNTHETIC RETURNED ADDRESS",
                status="returned", raw_payload={
                    "order_key": "synthetic-returned-order", "return_status": "returned",
                },
            )
            returned.items.append(OrderItem(
                product="SYNTHETIC RETURNED PRODUCT", quantity_pieces=10, quantity_blocks=1,
                status="completed", raw_payload={
                    "item_key": "a" * 64, "source_import_id": "synthetic-import-row-1",
                    "source_batch_key": "synthetic-batch-old",
                },
            ))
            session.add_all((active, returned))
            session.commit()
    finally:
        engine.dispose()


@contextmanager
def resolved_database_url(value):
    if value != "test-harness":
        yield value
        return
    from tools.benchmark_backend import disposable_database

    with disposable_database() as (database_url, _runtime):
        seed_test_harness(database_url)
        yield database_url


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--max-batches", type=int, default=10000)
    parser.add_argument("--after-order-id", default="")
    parser.add_argument("--after-item-id", default="")
    return parser.parse_args()


def main():
    args = parse_args()
    if not 1 <= args.batch_size <= 1000:
        raise SystemExit("--batch-size must be between 1 and 1000")
    if not 1 <= args.max_batches <= 10000:
        raise SystemExit("--max-batches must be between 1 and 10000")
    with resolved_database_url(args.database_url) as database_url:
        report = analyze(
            database_url, batch_size=args.batch_size, max_batches=args.max_batches,
            after_order_id=args.after_order_id, after_item_id=args.after_item_id,
        )
        report.update({"mode": "dry_run" if args.dry_run else "apply", "mutations": 0})
        if args.apply:
            if report["conflicts"]:
                report["status"] = "blocked"
                sys.stdout.write(json.dumps(report, ensure_ascii=False, sort_keys=True) + "\n")
                return 1
            applied = apply_backfill(
                database_url, batch_size=args.batch_size, max_batches=args.max_batches,
                after_order_id=args.after_order_id, after_item_id=args.after_item_id,
            )
            report.update(applied)
            report["mutations"] = applied["updated_orders"] + applied["updated_items"]
            verification = analyze(
                database_url, batch_size=args.batch_size, max_batches=args.max_batches,
                after_order_id=args.after_order_id, after_item_id=args.after_item_id,
            )
            report["post_apply_verification"] = {
                "orders_candidates": verification["orders_candidates"],
                "items_candidates": verification["items_candidates"],
                "conflicts": verification["conflicts"],
            }
            if (
                verification["orders_candidates"]
                or verification["items_candidates"]
                or verification["conflicts"]
            ):
                report["status"] = "fail"
                sys.stdout.write(json.dumps(report, ensure_ascii=False, sort_keys=True) + "\n")
                return 1
        report["status"] = "pass" if not report["conflicts"] else "conflicts"
        sys.stdout.write(json.dumps(report, ensure_ascii=False, sort_keys=True) + "\n")
        return 0 if args.dry_run or report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
