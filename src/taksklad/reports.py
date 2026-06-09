import json
import logging
import os
import re
from datetime import datetime

from .catalog import get_product_rule
from .config import (
    BACKUP_DIR,
    ORDER_DATE_COLUMN,
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
from .pending_store import load_pending_saves
from .scan_quantities import (
    block_quantity_for_code,
    scanned_blocks_for_order_codes,
)
from .sheets import validate_sheet_header
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

def build_day_report_rows_from_gsheet(sheet, report_date=None):
    all_rows = sheet.get_all_values()
    if not all_rows:
        return empty_day_report_rows()

    header_idx, missing = validate_sheet_header(all_rows[0])
    if missing:
        raise ValueError("В таблице не найдены обязательные колонки: " + ", ".join(missing))

    report_date_str = report_date_display(report_date)
    report_rows = empty_day_report_rows()

    for row in all_rows[1:]:
        if parse_date_to_standard(get_cell(row, get_order_date_header_index(header_idx))) != report_date_str:
            continue

        codes = split_codes(get_cell(row, header_idx.get("Отсканированные коды")))
        if not codes:
            continue

        payment_type = get_cell(row, header_idx.get("Тип оплаты"))
        payment_group = normalize_payment_type(payment_type)
        rows = report_rows[payment_group]

        for code in codes:
            pieces_per_block = get_product_rule(get_cell(row, header_idx.get("Товары")))["pieces_per_block"]
            block_quantity = block_quantity_for_code(code)
            rows.append({
                "Дата/время скана": "",
                "Дата отгрузки": get_cell(row, get_order_date_header_index(header_idx)),
                "Клиент": get_cell(row, header_idx.get("Клиент")),
                "Торговый представитель": get_cell(row, header_idx.get("Торговый представитель")),
                "Адрес": get_cell(row, header_idx.get("Адрес")),
                "Товар": get_cell(row, header_idx.get("Товары")),
                "Тип оплаты": payment_type,
                "Номер заявки SkladBot": get_cell(row, header_idx.get(SKLADBOT_REQUEST_NUMBER_COLUMN)),
                "Кол-во ШТ в блоке": pieces_per_block,
                "Кол-во блок": block_quantity,
                "Итого ШТ": pieces_per_block * block_quantity,
                "Код": code,
                "Источник": "google_sheets",
            })

    return report_rows

def add_pending_saves_to_report_rows(report_rows, report_date=None):
    report_date_str = report_date_key(report_date)
    existing_codes = {
        row.get("Код")
        for rows in report_rows.values()
        for row in rows
        if row.get("Код")
    }

    for item in load_pending_saves():
        order = item.get("order", {})
        created_at = normalize_text(item.get("created_at") or item.get("updated_at"))
        if created_at and report_date_key(created_at) != report_date_str:
            continue

        payment_type = order.get("Тип оплаты", "")
        payment_group = normalize_payment_type(payment_type)
        rows = report_rows[payment_group]
        pieces_per_block = get_product_rule(order.get("Товары"))["pieces_per_block"]

        for code in item.get("codes", []):
            if not code or code in existing_codes:
                continue
            block_quantity = block_quantity_for_code(code)
            rows.append({
                "Дата/время скана": item.get("created_at", ""),
                "Дата отгрузки": get_order_date_value(order),
                "Клиент": order.get("Клиент", ""),
                "Торговый представитель": order.get("Торговый представитель", ""),
                "Адрес": order.get("Адрес", ""),
                "Товар": order.get("Товары", ""),
                "Тип оплаты": payment_type,
                "Номер заявки SkladBot": order.get(SKLADBOT_REQUEST_NUMBER_COLUMN, ""),
                "Кол-во ШТ в блоке": pieces_per_block,
                "Кол-во блок": block_quantity,
                "Итого ШТ": pieces_per_block * block_quantity,
                "Код": code,
                "Источник": "pending_saves",
            })
            existing_codes.add(code)

    return report_rows

def parse_import_day(value):
    text = normalize_text(value)
    if not text:
        return ""
    return parse_date_to_standard(text.split()[0]) or text

def parse_datetime_for_sort(value):
    text = normalize_text(value)
    for fmt in ("%d.%m.%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%d.%m.%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return datetime.min

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

def split_source_files(value):
    text = normalize_text(value)
    if not text:
        return []
    return [part.strip() for part in text.split(",") if part.strip()]

def document_report_key(source_file, import_day):
    return make_hash({
        "source_file": normalize_text(source_file),
        "import_day": normalize_text(import_day),
    })

def pending_codes_for_order(order, pending_saves=None):
    pending_saves = pending_saves if pending_saves is not None else load_pending_saves()
    order_id = normalize_text(order.get("ID заказа"))
    row_number = normalize_text(order.get("_row_number"))
    codes = []
    seen = set()

    for item in pending_saves:
        pending_order = item.get("order", {})
        pending_order_id = normalize_text(pending_order.get("ID заказа"))
        pending_row_number = normalize_text(pending_order.get("_row_number"))
        matches = False
        if order_id and pending_order_id and order_id == pending_order_id:
            matches = True
        elif row_number and pending_row_number and row_number == pending_row_number:
            matches = True

        if not matches:
            continue

        for code in item.get("codes", []):
            code = normalize_text(code)
            if code and code not in seen:
                codes.append(code)
                seen.add(code)

    return codes

def merge_order_codes_with_pending(order, sheet_codes, pending_saves=None):
    codes = list(sheet_codes)
    seen = set(codes)
    for code in pending_codes_for_order(order, pending_saves=pending_saves):
        if code not in seen:
            codes.append(code)
            seen.add(code)
    return codes

def iter_document_orders_from_rows(all_rows, pending_saves=None):
    if not all_rows:
        return []

    header_idx, missing = validate_sheet_header(all_rows[0])
    if missing:
        raise ValueError("В таблице не найдены обязательные колонки: " + ", ".join(missing))

    pending_saves = pending_saves if pending_saves is not None else load_pending_saves()
    rows = []
    for row_number, row in enumerate(all_rows[1:], start=2):
        source_files = split_source_files(get_cell(row, header_idx.get("Источник файла")))
        if not source_files:
            continue

        order = {col_name: get_cell(row, idx) for col_name, idx in header_idx.items()}
        order["_row_number"] = row_number
        sheet_codes = split_codes(order.get("Отсканированные коды"))
        codes = merge_order_codes_with_pending(order, sheet_codes, pending_saves=pending_saves)
        sheet_scanned_blocks = scanned_blocks_for_order_codes(order, sheet_codes)
        scanned_blocks = scanned_blocks_for_order_codes(order, codes)
        plan_blocks = get_plan_blocks(order)
        import_at = order.get("Дата импорта", "")
        rows.append({
            "order": order,
            "row_number": row_number,
            "source_files": source_files,
            "import_at": import_at,
            "import_day": parse_import_day(import_at),
            "plan_blocks": plan_blocks,
            "codes": codes,
            "sheet_codes_count": len(sheet_codes),
            "pending_codes_count": max(0, len(codes) - len(sheet_codes)),
            "sheet_scanned_blocks": sheet_scanned_blocks,
            "scanned_blocks": scanned_blocks,
            "pending_blocks": max(0, scanned_blocks - sheet_scanned_blocks),
        })
    return rows

def build_document_summaries_from_gsheet(sheet, limit=12):
    document_rows = iter_document_orders_from_rows(sheet.get_all_values())
    documents = {}
    for item in document_rows:
        for source_file in item["source_files"]:
            key = document_report_key(source_file, item["import_day"])
            document = documents.setdefault(key, {
                "key": key,
                "source_file": source_file,
                "import_day": item["import_day"],
                "last_import": item["import_at"],
                "positions": 0,
                "completed_positions": 0,
                "plan_blocks": 0,
                "scanned_blocks": 0,
                "pending_blocks": 0,
            })
            document["positions"] += 1
            document["plan_blocks"] += item["plan_blocks"]
            scanned_blocks = item["scanned_blocks"]
            document["scanned_blocks"] += scanned_blocks
            document["pending_blocks"] += item["pending_blocks"]
            if item["plan_blocks"] > 0 and scanned_blocks >= item["plan_blocks"]:
                document["completed_positions"] += 1
            if parse_datetime_for_sort(item["import_at"]) > parse_datetime_for_sort(document["last_import"]):
                document["last_import"] = item["import_at"]

    summaries = sorted(
        documents.values(),
        key=lambda document: parse_datetime_for_sort(document.get("last_import")),
        reverse=True,
    )
    return summaries[:limit] if limit else summaries

def truncate_middle(text, max_length):
    text = normalize_text(text)
    if len(text) <= max_length:
        return text
    if max_length <= 3:
        return text[:max_length]
    head = max_length // 2
    tail = max_length - head - 3
    return text[:head] + "..." + text[-tail:]

def create_document_report_excel(sheet, document_key):
    import pandas as pd

    document_rows = iter_document_orders_from_rows(sheet.get_all_values())
    selected = []
    selected_source = ""
    selected_import_day = ""
    for item in document_rows:
        for source_file in item["source_files"]:
            if document_report_key(source_file, item["import_day"]) != document_key:
                continue
            selected.append((source_file, item))
            selected_source = source_file
            selected_import_day = item["import_day"]

    if not selected:
        return {"empty": True, "document_key": document_key}

    positions = []
    codes_rows = []
    missing_rows = []
    total_plan = 0
    total_scanned = 0
    completed_positions = 0
    pending_count = 0
    last_import = ""

    for source_file, item in selected:
        order = item["order"]
        plan_blocks = item["plan_blocks"]
        codes = item["codes"]
        scanned_count = item["scanned_blocks"]
        remaining = max(0, plan_blocks - scanned_count)
        total_plan += plan_blocks
        total_scanned += scanned_count
        pending_count += item["pending_blocks"]
        if plan_blocks > 0 and scanned_count >= plan_blocks:
            completed_positions += 1
        if parse_datetime_for_sort(item["import_at"]) > parse_datetime_for_sort(last_import):
            last_import = item["import_at"]

        position_row = {
            "Документ": source_file,
            "Дата импорта": item["import_at"],
            "Строка Google Sheets": item["row_number"],
            "Строка файла": order.get("Строка файла", ""),
            "Дата заказа": get_order_date_value(order) or "",
            "Клиент": order.get("Клиент", ""),
            "Тип оплаты": order.get("Тип оплаты", ""),
            "Адрес": order.get("Адрес", ""),
            "Торговый представитель": order.get("Торговый представитель", ""),
            "Товар": order.get("Товары", ""),
            "План КИЗ": plan_blocks,
            "Отсканировано КИЗ": scanned_count,
            "Осталось КИЗ": remaining,
            "КИЗ в локальной очереди": item["pending_blocks"],
            "Статус": "Выполнено" if plan_blocks > 0 and scanned_count >= plan_blocks else "Не выполнено",
        }
        positions.append(position_row)
        if remaining:
            missing_rows.append(position_row.copy())

        for code in codes:
            codes_rows.append({
                "Документ": source_file,
                "Дата импорта": item["import_at"],
                "Строка Google Sheets": item["row_number"],
                "Строка файла": order.get("Строка файла", ""),
                "Клиент": order.get("Клиент", ""),
                "Тип оплаты": order.get("Тип оплаты", ""),
                "Адрес": order.get("Адрес", ""),
                "Товар": order.get("Товары", ""),
                "КИЗ": code,
            })

    completion_percent = round((total_scanned / total_plan) * 100, 1) if total_plan else 0
    summary_rows = [{
        "Документ": selected_source,
        "Дата импорта": last_import or selected_import_day,
        "Позиций": len(positions),
        "Позиций выполнено": completed_positions,
        "План КИЗ": total_plan,
        "Отсканировано КИЗ": total_scanned,
        "Осталось КИЗ": max(0, total_plan - total_scanned),
        "КИЗ в локальной очереди": pending_count,
        "Готовность, %": completion_percent,
    }]

    os.makedirs(REPORTS_DIR, exist_ok=True)
    safe_name = re.sub(r"[^A-Za-zА-Яа-я0-9_.-]+", "_", selected_source)[:40] or "document"
    filename = os.path.join(
        REPORTS_DIR,
        f"document_report_{safe_name}_{datetime.now().strftime('%d.%m.%Y_%H%M%S')}.xlsx",
    )

    with pd.ExcelWriter(filename, engine="openpyxl") as writer:
        pd.DataFrame(summary_rows).to_excel(writer, sheet_name="Сводка", index=False)
        pd.DataFrame(positions).to_excel(writer, sheet_name="Позиции", index=False)
        if codes_rows:
            pd.DataFrame(codes_rows).to_excel(writer, sheet_name="КИЗы", index=False)
        else:
            pd.DataFrame({"Сообщение": ["По документу пока нет отсканированных КИЗов"]}).to_excel(
                writer,
                sheet_name="КИЗы",
                index=False,
            )
        if missing_rows:
            pd.DataFrame(missing_rows).to_excel(writer, sheet_name="Недосканировано", index=False)
        else:
            pd.DataFrame({"Сообщение": ["Все позиции документа выполнены"]}).to_excel(
                writer,
                sheet_name="Недосканировано",
                index=False,
            )

    return {
        "empty": False,
        "filename": filename,
        "source_file": selected_source,
        "import_day": selected_import_day,
        "last_import": last_import,
        "positions": len(positions),
        "completed_positions": completed_positions,
        "plan_blocks": total_plan,
        "scanned_blocks": total_scanned,
        "remaining_blocks": max(0, total_plan - total_scanned),
        "pending_blocks": pending_count,
        "completion_percent": completion_percent,
    }

def create_day_report_excel(sheet=None, filename=None, include_pending=True, report_date=None):
    report_date = parse_report_date(report_date)
    report_rows = build_day_report_rows_from_scan_backup(report_date)
    report_source = "scan_backup"
    if not any(report_rows.values()) and sheet:
        report_rows = build_day_report_rows_from_gsheet(sheet, report_date)
        report_source = "google_sheets"
    if include_pending:
        report_rows = add_pending_saves_to_report_rows(report_rows, report_date)
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


def create_shift_report_excels_by_order_date(sheet=None, include_pending=True, scan_date=None):
    scan_date = parse_report_date(scan_date)
    report_rows = build_day_report_rows_from_scan_backup(scan_date)
    report_source = "scan_backup"
    if not any(report_rows.values()) and sheet:
        report_rows = build_day_report_rows_from_gsheet(sheet, scan_date)
        report_source = "google_sheets"
    if include_pending:
        report_rows = add_pending_saves_to_report_rows(report_rows, scan_date)

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

def build_summary_products_from_gsheet(sheet, group_key):
    all_rows = sheet.get_all_values()
    if not all_rows:
        return []

    header_idx, missing = validate_sheet_header(all_rows[0])
    if missing:
        raise ValueError("В таблице не найдены обязательные колонки: " + ", ".join(missing))

    products = []
    for row in all_rows[1:]:
        row_record = {column: get_cell(row, idx) for column, idx in header_idx.items() if column}
        row_record[ORDER_DATE_COLUMN] = get_cell(row, get_order_date_header_index(header_idx))
        row_group = order_group_key(row_record)
        if row_group != group_key:
            continue

        codes = split_codes(get_cell(row, header_idx.get("Отсканированные коды")))
        if not codes:
            continue

        scanned_blocks = scanned_blocks_for_order_codes(row_record, codes)
        products.append({
            "Дата отгрузки": get_order_date_value(row_record),
            "Клиент": get_cell(row, header_idx.get("Клиент")),
            "Адрес": get_cell(row, header_idx.get("Адрес")),
            "Торговый представитель": get_cell(row, header_idx.get("Торговый представитель")),
            "Товары": get_cell(row, header_idx.get("Товары")),
            "Тип оплаты": get_cell(row, header_idx.get("Тип оплаты")),
            "Кол-во ШТ в блоке": get_product_rule(get_cell(row, header_idx.get("Товары")))["pieces_per_block"],
            "План": parse_int_value(get_cell(row, header_idx.get("Кол-во блок"))),
            "Отсканировано": scanned_blocks,
            "Сумма позиции": parse_int_value(get_cell(row, header_idx.get("Сумма позиции"))),
            "Цена заказа": parse_int_value(get_cell(row, header_idx.get("Сумма позиции"))),
            "Коды": codes,
        })

    return products
