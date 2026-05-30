#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${TAKSKLAD_ENV_FILE:-$SCRIPT_DIR/.env}"
COMPOSE_FILE="$SCRIPT_DIR/docker-compose.yml"
MARKER="${SMOKE_MARKER:-SMOKE_MVP_CHAPMAN_$(date -u +%Y%m%dT%H%M%SZ)}"
SHIPMENT_DATE="${SMOKE_SHIPMENT_DATE:-$(date -u +%Y-%m-%d)}"
CLEANUP_DONE="0"

usage() {
  cat >&2 <<'EOF'
Usage:
  smoke_mvp_chapman.sh

Optional env:
  TAKSKLAD_ENV_FILE=/path/to/.env
  SMOKE_MARKER=SMOKE_MVP_CHAPMAN_custom_marker
  SMOKE_SHIPMENT_DATE=YYYY-MM-DD

The marker must contain SMOKE_MVP. Smoke data is removed automatically.
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

case "$MARKER" in
  *SMOKE_MVP*) ;;
  *)
    echo "Refusing unsafe marker: $MARKER" >&2
    echo "Marker must contain SMOKE_MVP." >&2
    exit 2
    ;;
esac

if [[ ! "$SHIPMENT_DATE" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]]; then
  echo "SMOKE_SHIPMENT_DATE must be YYYY-MM-DD." >&2
  exit 2
fi

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Env file not found: $ENV_FILE" >&2
  exit 1
fi

cleanup() {
  if [[ "$CLEANUP_DONE" == "1" ]]; then
    return
  fi
  echo "Running cleanup for $MARKER..." >&2
  "$SCRIPT_DIR/cleanup_acceptance_marker.sh" "$MARKER" --apply >&2 || true
}

trap cleanup EXIT

echo "Pre-clean dry-run for $MARKER..."
"$SCRIPT_DIR/cleanup_acceptance_marker.sh" "$MARKER"

echo "Running MVP smoke for $MARKER / $SHIPMENT_DATE..."
docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" exec -T \
  -e SMOKE_MARKER="$MARKER" \
  -e SMOKE_SHIPMENT_DATE="$SHIPMENT_DATE" \
  backend-api python - <<'PY'
import json
import os
from io import BytesIO
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

import openpyxl


BASE_URL = "http://127.0.0.1:8000"
TOKEN = os.environ["TAKSKLAD_API_TOKEN"]
MARKER = os.environ["SMOKE_MARKER"]
SHIPMENT_DATE = os.environ["SMOKE_SHIPMENT_DATE"]
SOURCE_FILE = f"{MARKER}.xlsx"
COORDINATES_WITH_ALT = "41.214609,69.223027,15"
COORDINATES_NORMALIZED = "41.214609,69.223027"


def request(method, path, payload=None, expect=(200,), raw=False):
    data = None
    headers = {"Authorization": f"Bearer {TOKEN}"}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = Request(f"{BASE_URL}{path}", data=data, headers=headers, method=method)
    try:
        with urlopen(req, timeout=20) as response:
            body = response.read()
            status = response.status
    except HTTPError as exc:
        body = exc.read()
        status = exc.code
    if status not in expect:
        text = body.decode("utf-8", errors="replace")
        raise AssertionError(f"{method} {path} returned {status}, expected {expect}: {text}")
    if raw:
        return body
    if not body:
        return None
    return json.loads(body.decode("utf-8"))


def assert_equal(actual, expected, label):
    if actual != expected:
        raise AssertionError(f"{label}: expected {expected!r}, got {actual!r}")


rows = [
    {
        "Дата отгрузки": SHIPMENT_DATE,
        "Тип оплаты": "Перечисление",
        "Клиент": MARKER,
        "Адрес": "Ташкент, smoke address",
        "Координаты": COORDINATES_WITH_ALT,
        "Торговый представитель": "+998900000001",
        "Товары": "Chapman Brown OP 20",
        "Кол-во блок": 2,
        "Кол-во ШТ": 20,
        "_pieces_per_block": 10,
        "Цена за блок": 240000,
        "Сумма позиции": 480000,
        "Источник файла": SOURCE_FILE,
        "Строка файла": 2,
    },
    {
        "Дата отгрузки": SHIPMENT_DATE,
        "Тип оплаты": "Перечисление",
        "Клиент": MARKER,
        "Адрес": "Ташкент, smoke address",
        "Координаты": COORDINATES_WITH_ALT,
        "Торговый представитель": "+998900000001",
        "Товары": "Chapman Gold SSL 20",
        "Кол-во блок": 1,
        "Кол-во ШТ": 10,
        "_pieces_per_block": 10,
        "Цена за блок": 240000,
        "Сумма позиции": 240000,
        "Источник файла": SOURCE_FILE,
        "Строка файла": 3,
    },
]

import_result = request("POST", "/api/v1/imports", {
    "source": "smoke",
    "filename": SOURCE_FILE,
    "rows": rows,
}, expect=(201,))
assert_equal(import_result["rows_imported"], 2, "rows_imported")
assert_equal(import_result["orders_created"], 1, "orders_created")
assert_equal(import_result["items_created"], 2, "items_created")
assert_equal(import_result["duplicate_rows"], 0, "duplicate_rows")
assert_equal(import_result["invalid_rows"], 0, "invalid_rows")

logistics_bytes = request(
    "GET",
    f"/api/v1/logistics/report?shipment_date={quote(SHIPMENT_DATE)}",
    expect=(200,),
    raw=True,
)
logistics_wb = openpyxl.load_workbook(BytesIO(logistics_bytes), data_only=True)
logistics_sheet = logistics_wb.active
assert_equal(logistics_sheet.max_row, 3, "logistics rows including header")
for row_number in (2, 3):
    assert_equal(logistics_sheet.cell(row_number, 31).value, COORDINATES_NORMALIZED, "logistics coordinates")
    assert_equal(logistics_sheet.cell(row_number, 32).value, "41.214609", "logistics latitude")
    assert_equal(logistics_sheet.cell(row_number, 33).value, "69.223027", "logistics longitude")

active_orders = request("GET", "/api/v1/orders/active")
order = next((value for value in active_orders if value["client"] == MARKER), None)
if not order:
    raise AssertionError(f"Active order {MARKER} not found")
assert_equal(order["coordinates"], COORDINATES_WITH_ALT, "active order coordinates")
assert_equal(len(order["items"]), 2, "active order items")
assert_equal(sum(item["quantity_blocks"] for item in order["items"]), 3, "planned blocks")
assert_equal(sum(item["line_total"] for item in order["items"]), 720000, "total price")

incomplete_complete = request("POST", f"/api/v1/orders/{order['id']}/complete", expect=(409,))
if incomplete_complete.get("detail", {}).get("message") != "Order has incomplete required items":
    raise AssertionError(f"unexpected incomplete complete response: {incomplete_complete}")

codes = {
    "Chapman Brown OP 20": [f"{MARKER}-KIZ-001", f"{MARKER}-KIZ-002"],
    "Chapman Gold SSL 20": [f"{MARKER}-KIZ-003"],
}
duplicate_checked = False
for item in order["items"]:
    for code in codes[item["product"]]:
        scan = request("POST", "/api/v1/scans", {
            "order_item_id": item["id"],
            "code": code,
            "workstation_id": "vds-smoke",
            "scanned_by": "smoke",
        }, expect=(201,))
        if not duplicate_checked:
            duplicate = request("POST", "/api/v1/scans", {
                "order_item_id": item["id"],
                "code": code,
                "workstation_id": "vds-smoke",
                "scanned_by": "smoke",
            }, expect=(409,))
            assert_equal(duplicate["detail"], "Code already scanned", "duplicate scan response")
            duplicate_checked = True
        if scan["scanned_blocks"] > item["quantity_blocks"]:
            raise AssertionError("scan overflow")

completed_order = request("POST", f"/api/v1/orders/{order['id']}/complete")
assert_equal(completed_order["status"], "completed", "completed order status")
assert_equal(sum(item["scanned_blocks"] for item in completed_order["items"]), 3, "completed scanned blocks")

source_files = request("GET", "/api/v1/reports/kiz/source-files")
source_entry = next((value for value in source_files if value["source_file"] == SOURCE_FILE), None)
if not source_entry:
    raise AssertionError(f"KIZ source file {SOURCE_FILE} not found")
assert_equal(source_entry["planned_blocks"], 3, "source planned blocks")
assert_equal(source_entry["scanned_blocks"], 3, "source scanned blocks")

kiz_bytes = request(
    "GET",
    f"/api/v1/reports/kiz/source-file?source_file={quote(SOURCE_FILE)}",
    expect=(200,),
    raw=True,
)
kiz_wb = openpyxl.load_workbook(BytesIO(kiz_bytes), data_only=True)
if "Сводка" not in kiz_wb.sheetnames:
    raise AssertionError(f"summary sheet missing: {kiz_wb.sheetnames}")
summary = kiz_wb["Сводка"]
assert_equal(summary.cell(2, 3).value, MARKER, "summary client")
assert_equal(summary.cell(2, 7).value, 3, "summary planned blocks")
assert_equal(summary.cell(2, 8).value, 3, "summary scanned blocks")
assert_equal(summary.cell(2, 9).value, 720000, "summary total price")

active_after_complete = request("GET", "/api/v1/orders/active")
if any(value["client"] == MARKER for value in active_after_complete):
    raise AssertionError("completed smoke order is still active")

print(json.dumps({
    "marker": MARKER,
    "shipment_date": SHIPMENT_DATE,
    "import_rows": import_result["rows_imported"],
    "orders_created": import_result["orders_created"],
    "logistics_rows": logistics_sheet.max_row - 1,
    "scans": 3,
    "duplicate_scan": "rejected",
    "order_completed": True,
    "kiz_summary_total": summary.cell(2, 9).value,
}, ensure_ascii=False, sort_keys=True))
PY

echo "Verify marker before cleanup..."
"$SCRIPT_DIR/verify_acceptance_marker.sh" "$MARKER" --expect-orders 1 --expect-scans 3 --expect-completed

echo "Cleanup apply for $MARKER..."
"$SCRIPT_DIR/cleanup_acceptance_marker.sh" "$MARKER" --apply
CLEANUP_DONE="1"

echo "Post-clean dry-run for $MARKER..."
"$SCRIPT_DIR/cleanup_acceptance_marker.sh" "$MARKER"

echo "MVP smoke completed."
