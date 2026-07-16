import json
import logging
import os
import re
from datetime import datetime

from .catalog import get_product_rule
from .config import (
    BACKUP_DIR,
    LEGACY_ORDER_DATE_COLUMN,
    ORDER_DATE_COLUMN,
    REQUIRED_COLUMNS,
    REPORTS_DIR,
    SKLADBOT_REQUEST_NUMBER_COLUMN,
    STATUS_COLUMN,
)
from .orders import (
    get_order_date_header_index,
    get_order_date_value,
    get_plan_blocks,
    order_group_key,
)
from .scan_quantities import (
    block_quantity_for_code,
    scanned_blocks_for_order_codes,
)
from .spreadsheet_safety import force_workbook_text_literals
from .utils import (
    get_cell,
    make_hash,
    normalize_lookup_text,
    normalize_payment_type,
    normalize_text,
    parse_date_to_standard,
    parse_int_value,
    split_codes,
)


def empty_day_report_rows():
    return {"terminal": [], "transfer": [], "unknown": []}


def validate_sheet_header(header):
    """Legacy row parser helper kept without importing the Google client."""

    from .utils import get_header_index

    header_idx = get_header_index(header)
    if ORDER_DATE_COLUMN not in header_idx and LEGACY_ORDER_DATE_COLUMN in header_idx:
        header_idx[ORDER_DATE_COLUMN] = header_idx[LEGACY_ORDER_DATE_COLUMN]
    missing = [column for column in REQUIRED_COLUMNS if column not in header_idx]
    return header_idx, missing

def add_day_report_code(report_rows, code_row, seen_codes):
    code = normalize_text(code_row.get("Код") or code_row.get("КИЗ"))
    if not code or code in seen_codes:
        return
    payment_group = normalize_payment_type(code_row.get("Тип оплаты"))
    if payment_group not in report_rows:
        payment_group = "unknown"
    code_row["Код"] = code
    report_rows[payment_group].append(code_row)
    seen_codes.add(code)

def build_day_report_rows_from_scan_backup(report_date=None):
    report_rows = empty_day_report_rows()
    report_date = parse_report_date(report_date)
    backup_path = scan_backup_path_for_date(report_date)
    if not os.path.exists(backup_path):
        return report_rows

    seen_codes = set()
    rows_by_code = {}
    try:
        with open(backup_path, "r", encoding="utf-8") as backup_file:
            for line in backup_file:
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    logging.warning("Некорректная строка backup сканов: %s", line[:200])
                    continue

                action = normalize_text(item.get("action"))
                timestamp = normalize_text(item.get("timestamp"))
                product_name = item.get("product", "")
                pieces_per_block = get_product_rule(product_name)["pieces_per_block"]
                base_row = {
                    "Дата/время скана": timestamp,
                    "Дата отгрузки": item.get("date", ""),
                    "Клиент": item.get("client", ""),
                    "Торговый представитель": item.get("representative", ""),
                    "Адрес": item.get("address", ""),
                    "Товар": product_name,
                    "Тип оплаты": item.get("payment_type", ""),
                    "Номер заявки SkladBot": item.get("skladbot_request_number", ""),
                    "Кол-во ШТ в блоке": pieces_per_block,
                    "Кол-во блок": 1,
                    "Итого ШТ": pieces_per_block,
                    "Источник": action,
                }

                if action == "undo_scan":
                    code = normalize_text(item.get("code"))
                    if code:
                        rows_by_code.pop(code, None)
                    continue

                codes = []
                if action == "scan":
                    codes = [normalize_text(item.get("code"))]
                elif action in ("position_saved", "position_queued", "pending_save_synced", "address_finished"):
                    codes = [normalize_text(code) for code in item.get("codes", [])]

                for code in codes:
                    if code:
                        block_quantity = block_quantity_for_code(code)
                        row = dict(
                            base_row,
                            Код=code,
                            **{
                                "Кол-во блок": block_quantity,
                                "Итого ШТ": pieces_per_block * block_quantity,
                            },
                        )
                        rows_by_code.setdefault(code, row)
    except Exception:
        logging.exception("Не удалось прочитать backup сканов для дневного отчета")
        return report_rows

    for row in rows_by_code.values():
        add_day_report_code(report_rows, row, seen_codes)
    return report_rows

def parse_report_date(value=None):
    if value is None:
        return datetime.now().date()
    if hasattr(value, "date") and hasattr(value, "hour"):
        return value.date()
    if hasattr(value, "strftime") and not isinstance(value, str):
        return value
    text = normalize_text(value)
    if not text:
        return datetime.now().date()
    for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y", "%d.%m.%y"):
        try:
            return datetime.strptime(text.split()[0], fmt).date()
        except ValueError:
            continue
    parsed = parse_date_to_standard(text)
    if parsed:
        try:
            return datetime.strptime(parsed, "%d.%m.%Y").date()
        except ValueError:
            pass
    return datetime.now().date()

def report_date_key(report_date=None):
    return parse_report_date(report_date).strftime("%Y-%m-%d")

def report_date_display(report_date=None):
    return parse_report_date(report_date).strftime("%d.%m.%Y")

def scan_backup_path_for_date(report_date=None):
    return os.path.join(BACKUP_DIR, f"scan_backup_{report_date_display(report_date)}.jsonl")

def skladbot_number_sort_key(value):
    text = normalize_text(value)
    match = re.search(r"(\d+)$", text)
    return parse_int_value(match.group(1)) if match else 0

def unpack_order_group_key(group_key):
    if len(group_key) == 4:
        return group_key
    client, payment_type, address = group_key
    return "", client, payment_type, address

def order_group_display_sort_key(group_key):
    request_number, client, payment_type, address = unpack_order_group_key(group_key)
    return (
        0 if request_number else 1,
        skladbot_number_sort_key(request_number),
        normalize_lookup_text(request_number),
        normalize_lookup_text(client),
        normalize_lookup_text(payment_type),
        normalize_lookup_text(address),
    )

def truncate_middle(text, max_length):
    text = normalize_text(text)
    if len(text) <= max_length:
        return text
    if max_length <= 3:
        return text[:max_length]
    head = max_length // 2
    tail = max_length - head - 3
    return text[:head] + "..." + text[-tail:]

def create_day_report_excel(filename=None, report_date=None):
    report_date = parse_report_date(report_date)
    report_rows = build_day_report_rows_from_scan_backup(report_date)
    report_source = "scan_backup"
    terminal_rows = report_rows["terminal"]
    transfer_rows = report_rows["transfer"]
    unknown_rows = report_rows["unknown"]
    total_report_rows = len(terminal_rows) + len(transfer_rows) + len(unknown_rows)

    result = {
        "empty": total_report_rows == 0,
        "filename": filename,
        "total_report_rows": total_report_rows,
        "terminal_count": len(terminal_rows),
        "transfer_count": len(transfer_rows),
        "unknown_count": len(unknown_rows),
        "report_date": report_date_key(report_date),
        "report_date_display": report_date_display(report_date),
        "source": report_source,
    }
    if total_report_rows == 0:
        return result

    os.makedirs(REPORTS_DIR, exist_ok=True)
    if not filename:
        filename = os.path.join(
            REPORTS_DIR,
            f"scan_report_{report_date_display(report_date)}_{datetime.now().strftime('%H%M%S')}.xlsx",
        )
        result["filename"] = filename

    write_day_report_workbook(filename, terminal_rows, transfer_rows, unknown_rows)
    return result


def create_shift_report_excels_by_order_date(scan_date=None):
    scan_date = parse_report_date(scan_date)
    report_rows = build_day_report_rows_from_scan_backup(scan_date)
    report_source = "scan_backup"

    total_report_rows = sum(len(rows) for rows in report_rows.values())
    result = {
        "empty": total_report_rows == 0,
        "filename": None,
        "total_report_rows": total_report_rows,
        "terminal_count": len(report_rows["terminal"]),
        "transfer_count": len(report_rows["transfer"]),
        "unknown_count": len(report_rows["unknown"]),
        "report_date": report_date_key(scan_date),
        "report_date_display": report_date_display(scan_date),
        "source": report_source,
        "reports": [],
    }
    if total_report_rows == 0:
        return result

    registry = load_shift_report_registry()
    registry_changed = False
    shipment_dates = sorted({
        normalize_text(row.get("Дата отгрузки")) or report_date_display(scan_date)
        for rows in report_rows.values()
        for row in rows
    }, key=date_sort_for_display)

    for shipment_date in shipment_dates:
        grouped = filter_report_rows_by_shipment_date(report_rows, shipment_date)
        terminal_rows = grouped["terminal"]
        transfer_rows = grouped["transfer"]
        unknown_rows = grouped["unknown"]
        file_total = len(terminal_rows) + len(transfer_rows) + len(unknown_rows)
        if file_total == 0:
            continue
        content_hash = shift_report_content_hash(grouped)
        existing_report = find_shift_report_registry_entry(registry, shipment_date, content_hash)
        if existing_report:
            filename = existing_report["filename"]
            part_number = existing_report["part_number"]
            already_exists = True
        else:
            filename, part_number = next_shift_report_filename(shipment_date)
            write_day_report_workbook(filename, terminal_rows, transfer_rows, unknown_rows)
            registry.append({
                "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "report_date": report_date_key(scan_date),
                "report_date_display": report_date_display(scan_date),
                "shipment_date": parse_date_to_standard(shipment_date) or shipment_date,
                "shipment_date_display": parse_date_to_standard(shipment_date) or shipment_date,
                "part_number": part_number,
                "filename": filename,
                "content_hash": content_hash,
                "total_report_rows": file_total,
            })
            registry_changed = True
            already_exists = False
        result["reports"].append({
            "empty": False,
            "filename": filename,
            "total_report_rows": file_total,
            "terminal_count": len(terminal_rows),
            "transfer_count": len(transfer_rows),
            "unknown_count": len(unknown_rows),
            "report_date": report_date_key(scan_date),
            "report_date_display": report_date_display(scan_date),
            "shipment_date": parse_date_to_standard(shipment_date) or shipment_date,
            "shipment_date_display": parse_date_to_standard(shipment_date) or shipment_date,
            "part_number": part_number,
            "already_exists": already_exists,
            "content_hash": content_hash,
            "source": report_source,
        })

    if registry_changed:
        save_shift_report_registry(registry)

    return result


def write_day_report_workbook(filename, terminal_rows, transfer_rows, unknown_rows):
    import pandas as pd

    with pd.ExcelWriter(filename, engine="openpyxl") as writer:
        if terminal_rows:
            pd.DataFrame(terminal_rows).to_excel(writer, sheet_name="Терминал", index=False)
        else:
            pd.DataFrame({"Сообщение": ["Нет данных"]}).to_excel(writer, sheet_name="Терминал", index=False)

        if transfer_rows:
            pd.DataFrame(transfer_rows).to_excel(writer, sheet_name="Перечисление", index=False)
        else:
            pd.DataFrame({"Сообщение": ["Нет данных"]}).to_excel(writer, sheet_name="Перечисление", index=False)

        if unknown_rows:
            pd.DataFrame(unknown_rows).to_excel(writer, sheet_name="Не распознано", index=False)
        force_workbook_text_literals(writer.book)


def filter_report_rows_by_shipment_date(report_rows, shipment_date):
    result = empty_day_report_rows()
    target = parse_date_to_standard(shipment_date) or shipment_date
    for group, rows in report_rows.items():
        for row in rows:
            row_date = parse_date_to_standard(row.get("Дата отгрузки")) or row.get("Дата отгрузки")
            if row_date == target:
                result[group].append(row)
    return result


def date_sort_for_display(value):
    text = parse_date_to_standard(value) or normalize_text(value)
    try:
        return datetime.strptime(text, "%d.%m.%Y")
    except ValueError:
        return datetime.max


def next_shift_report_filename(shipment_date):
    os.makedirs(REPORTS_DIR, exist_ok=True)
    display = parse_date_to_standard(shipment_date) or normalize_text(shipment_date) or "без_даты"
    safe_date = display.replace(".", "_")
    existing = [
        name
        for name in os.listdir(REPORTS_DIR)
        if name.startswith(f"scan_report_{safe_date}_ч") and name.endswith(".xlsx")
    ]
    part_number = len(existing) + 1
    filename = os.path.join(REPORTS_DIR, f"scan_report_{safe_date}_ч{part_number}.xlsx")
    return filename, part_number


def shift_report_registry_path():
    return os.path.join(REPORTS_DIR, "shift_report_registry.json")


def load_shift_report_registry():
    path = shift_report_registry_path()
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as file_obj:
            data = json.load(file_obj)
    except Exception:
        logging.exception("Не удалось прочитать реестр КИЗ-отчётов смены")
        return []
    return data if isinstance(data, list) else []


def save_shift_report_registry(registry):
    os.makedirs(REPORTS_DIR, exist_ok=True)
    path = shift_report_registry_path()
    tmp_path = path + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as file_obj:
            json.dump(registry, file_obj, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)
    except Exception:
        logging.exception("Не удалось сохранить реестр КИЗ-отчётов смены")
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass


def shift_report_content_hash(report_rows):
    payload = []
    for group, rows in sorted(report_rows.items()):
        for row in rows:
            payload.append({
                "group": group,
                "code": normalize_text(row.get("Код")),
                "date": parse_date_to_standard(row.get("Дата отгрузки")) or normalize_text(row.get("Дата отгрузки")),
                "client": normalize_lookup_text(row.get("Клиент")),
                "address": normalize_lookup_text(row.get("Адрес")),
                "product": normalize_lookup_text(row.get("Товар")),
                "payment_type": normalize_payment_type(row.get("Тип оплаты")),
                "skladbot_request_number": normalize_text(row.get("Номер заявки SkladBot")),
            })
    payload.sort(key=lambda item: (
        item["group"],
        item["date"],
        item["skladbot_request_number"],
        item["client"],
        item["address"],
        item["product"],
        item["code"],
    ))
    return make_hash(payload)


def find_shift_report_registry_entry(registry, shipment_date, content_hash):
    target_date = parse_date_to_standard(shipment_date) or normalize_text(shipment_date)
    for entry in reversed(registry):
        entry_date = parse_date_to_standard(entry.get("shipment_date_display") or entry.get("shipment_date"))
        if not entry_date:
            entry_date = normalize_text(entry.get("shipment_date_display") or entry.get("shipment_date"))
        filename = normalize_text(entry.get("filename"))
        if (
            entry_date == target_date
            and normalize_text(entry.get("content_hash")) == content_hash
            and filename
            and os.path.exists(filename)
        ):
            return {
                "filename": filename,
                "part_number": parse_int_value(entry.get("part_number")) or 1,
            }
    return None
