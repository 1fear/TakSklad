#!/usr/bin/env python3
"""Склейка задвоенных позиций заказа: один товар одного заказа = одна позиция.

Импорт до исправления заводил отдельную позицию на каждую строку файла, поэтому
один и тот же товар одного заказа мог занять две строки. Количество при этом не
терялось, но склад видел две одинаковые позиции, а SkladBot одну суммарную.

Инструмент только объединяет уже существующие позиции внутри одного заказа. Он
не читает файлы, не обращается к SkladBot, Telegram или Smartup и не меняет
количество: сумма блоков и штук до и после совпадает, иначе прогон падает.

Stdout содержит только счётчики и идентификаторы. Before-image каждой склейки
уходит в AuditLog.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

if "__file__" in globals():  # при запуске из stdin пути репозитория нет
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select
from sqlalchemy.orm import selectinload

try:  # репозиторий
    from backend.app.db import SessionLocal
    from backend.app.models import AuditLog, KizMovement, Order, OrderItem, ScanCode
except ModuleNotFoundError:  # образ backend, где пакет смонтирован как app
    from app.db import SessionLocal
    from app.models import AuditLog, KizMovement, Order, OrderItem, ScanCode


def normalize_text(value) -> str:
    """Локальная копия: инструмент обязан запускаться и на прежней сборке backend."""
    if value is None:
        return ""
    return str(value).strip()


def item_source_import_ids(item):
    """Все «ID импорта», поглощённые позицией, включая ранее склеенные строки."""
    raw_payload = item.raw_payload or {}
    values = [
        normalize_text(item.source_import_id),
        normalize_text(raw_payload.get("source_import_id")),
    ]
    values.extend(normalize_text(value) for value in (raw_payload.get("source_import_ids") or []))
    collected = []
    for value in values:
        if value and value not in collected:
            collected.append(value)
    return collected


APPROVAL = "MERGE-DUPLICATE-POSITIONS-2026-07-31"
REPAIR_ACTION = "order_positions_merged"
BATCH_ACTION = "order_positions_merge"


class RepairBlocked(RuntimeError):
    def __init__(self, code: str):
        self.code = str(code)
        super().__init__(self.code)


def product_group_key(item) -> str:
    return normalize_text(item.product).casefold()


def source_row_of(item) -> str:
    return normalize_text((item.raw_payload or {}).get("source_row"))


def sort_key_for_keeper(item):
    """Хранителем становится позиция со сканами, затем самая ранняя по файлу."""
    source_row = source_row_of(item)
    try:
        row_number = int(source_row)
    except (TypeError, ValueError):
        row_number = 10**9
    return (
        -int(item.scanned_blocks or 0),
        item.created_at or 0,
        row_number,
        str(item.id),
    )


def load_duplicate_groups(db, only_open: bool = False):
    statement = (
        select(Order)
        .options(selectinload(Order.items))
        .order_by(Order.order_date.asc(), Order.created_at.asc())
    )
    groups = []
    for order in db.execute(statement).scalars():
        if only_open and normalize_text(order.status) == "completed":
            continue
        by_product = {}
        for item in order.items or []:
            by_product.setdefault(product_group_key(item), []).append(item)
        for product_key, items in by_product.items():
            if len(items) < 2 or not product_key:
                continue
            ordered = sorted(items, key=sort_key_for_keeper)
            groups.append((order, ordered[0], tuple(ordered[1:])))
    return groups


def describe_group(order, keeper, absorbed) -> dict:
    return {
        "order_id": str(order.id),
        "order_date": order.order_date.isoformat() if order.order_date else "",
        "order_status": normalize_text(order.status),
        "product": keeper.product,
        "keeper_item_id": str(keeper.id),
        "absorbed_item_ids": [str(item.id) for item in absorbed],
        "source_rows": [source_row_of(item) for item in (keeper, *absorbed)],
        "blocks_before": [int(item.quantity_blocks or 0) for item in (keeper, *absorbed)],
        "pieces_before": [int(item.quantity_pieces or 0) for item in (keeper, *absorbed)],
        "scanned_before": [int(item.scanned_blocks or 0) for item in (keeper, *absorbed)],
        "status_before": [normalize_text(item.status) for item in (keeper, *absorbed)],
        "blocks_after": sum(int(item.quantity_blocks or 0) for item in (keeper, *absorbed)),
        "pieces_after": sum(int(item.quantity_pieces or 0) for item in (keeper, *absorbed)),
        "scanned_after": sum(int(item.scanned_blocks or 0) for item in (keeper, *absorbed)),
    }


def merge_group(db, order, keeper, absorbed) -> dict:
    before = describe_group(order, keeper, absorbed)

    raw_payload = dict(keeper.raw_payload or {})
    source_import_ids = list(raw_payload.get("source_import_ids") or item_source_import_ids(keeper))
    merged_source_rows = list(raw_payload.get("merged_source_rows") or [])
    moved_scan_codes = 0
    moved_kiz_movements = 0

    for item in absorbed:
        keeper.quantity_pieces = int(keeper.quantity_pieces or 0) + int(item.quantity_pieces or 0)
        keeper.quantity_blocks = int(keeper.quantity_blocks or 0) + int(item.quantity_blocks or 0)
        keeper.scanned_blocks = int(keeper.scanned_blocks or 0) + int(item.scanned_blocks or 0)
        if not keeper.pieces_per_block and item.pieces_per_block:
            keeper.pieces_per_block = item.pieces_per_block

        item_payload = item.raw_payload or {}
        for value in item_source_import_ids(item):
            if value not in source_import_ids:
                source_import_ids.append(value)
        merged_source_rows.append({
            "source_import_id": normalize_text(item.source_import_id)
            or normalize_text(item_payload.get("source_import_id")),
            "source_order_id": normalize_text(item_payload.get("source_order_id")),
            "source_file": item_payload.get("source_file"),
            "source_row": source_row_of(item),
            "quantity_pieces": int(item.quantity_pieces or 0),
            "quantity_blocks": int(item.quantity_blocks or 0),
            "merged_by": BATCH_ACTION,
            "raw_row": item_payload.get("raw_row"),
        })
        for field in ("imported_line_total", "line_total", "calculated_line_total"):
            raw_payload[field] = (raw_payload.get(field) or 0) + (item_payload.get(field) or 0)

        for scan_code in db.execute(
            select(ScanCode).where(ScanCode.order_item_id == item.id)
        ).scalars():
            scan_code.order_item_id = keeper.id
            moved_scan_codes += 1
        for movement in db.execute(
            select(KizMovement).where(KizMovement.order_item_id == item.id)
        ).scalars():
            movement.order_item_id = keeper.id
            moved_kiz_movements += 1

        db.delete(item)

    raw_payload["source_import_ids"] = source_import_ids
    raw_payload["merged_source_rows"] = merged_source_rows
    keeper.raw_payload = raw_payload

    # Закрытый заказ остаётся закрытым: склейка не переоткрывает отгруженное.
    if normalize_text(order.status) != "completed":
        if int(keeper.scanned_blocks or 0) >= int(keeper.quantity_blocks or 0):
            keeper.status = "completed"
        else:
            keeper.status = "not_completed"

    after = {
        "blocks": int(keeper.quantity_blocks or 0),
        "pieces": int(keeper.quantity_pieces or 0),
        "scanned": int(keeper.scanned_blocks or 0),
        "status": normalize_text(keeper.status),
        "moved_scan_codes": moved_scan_codes,
        "moved_kiz_movements": moved_kiz_movements,
    }
    if after["blocks"] != before["blocks_after"] or after["pieces"] != before["pieces_after"]:
        raise RepairBlocked("quantity_drift")
    if after["scanned"] != before["scanned_after"]:
        raise RepairBlocked("scanned_drift")

    db.add(AuditLog(
        action=REPAIR_ACTION,
        entity_type="order_item",
        entity_id=str(keeper.id),
        payload={"before": before, "after": after},
    ))
    return {**before, **{f"after_{key}": value for key, value in after.items()}}


def run(apply_changes: bool, only_open: bool) -> dict:
    with SessionLocal() as db:
        groups = load_duplicate_groups(db, only_open=only_open)
        if not apply_changes:
            report = [describe_group(*group) for group in groups]
            return {"mode": "plan", "groups": len(report), "entries": report}

        entries = [merge_group(db, *group) for group in groups]
        db.add(AuditLog(
            action=BATCH_ACTION,
            entity_type="order_item",
            entity_id=BATCH_ACTION,
            payload={
                "groups": len(entries),
                "orders": sorted({entry["order_id"] for entry in entries}),
                "approval": APPROVAL,
            },
        ))
        db.commit()
        return {"mode": "apply", "groups": len(entries), "entries": entries}


def main() -> int:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--plan", action="store_true")
    mode.add_argument("--apply", action="store_true")
    parser.add_argument("--approval", default="")
    parser.add_argument("--only-open", action="store_true", help="только незакрытые заказы")
    args = parser.parse_args()

    if args.apply and args.approval != APPROVAL:
        print(json.dumps({"status": "blocked", "code": "approval_required"}, ensure_ascii=False))
        return 2

    try:
        result = run(apply_changes=bool(args.apply), only_open=bool(args.only_open))
    except RepairBlocked as exc:
        print(json.dumps({"status": "blocked", "code": exc.code}, ensure_ascii=False))
        return 3

    print(json.dumps({"status": "ok", **result}, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
