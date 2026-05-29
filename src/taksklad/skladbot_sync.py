import logging
from datetime import datetime

from .config import (
    ORDER_DATE_COLUMN,
    SKLADBOT_CHECKED_AT_COLUMN,
    SKLADBOT_REQUEST_ID_COLUMN,
    SKLADBOT_REQUEST_NUMBER_COLUMN,
    SKLADBOT_STATUS_COLUMN,
    SKLADBOT_STATUS_FOUND,
    SKLADBOT_STATUS_MULTIPLE,
    SKLADBOT_STATUS_NOT_FOUND,
)
from .orders import get_order_date_header_index, get_order_date_value, get_plan_blocks, is_order_active
from .skladbot import (
    fetch_candidate_requests,
    load_skladbot_settings,
    request_matches_order_group,
    skladbot_is_configured,
)
from .utils import (
    column_index_to_letter,
    get_cell,
    get_header_index,
    make_hash,
    normalize_lookup_text,
    normalize_text,
    parse_date_to_standard,
    parse_int_value,
)


def build_order_sync_key(record):
    return make_hash({
        "date": parse_date_to_standard(get_order_date_value(record)),
        "client": normalize_lookup_text(record.get("Клиент")),
        "payment": normalize_lookup_text(record.get("Тип оплаты")),
        "address": normalize_lookup_text(record.get("Адрес")),
    })


def product_sync_key(product_name):
    return normalize_lookup_text(product_name)


def build_order_groups_from_rows(all_rows):
    if not all_rows:
        return []

    header_idx = get_header_index(all_rows[0])
    date_idx = get_order_date_header_index(header_idx)
    groups = {}

    for row_number, row in enumerate(all_rows[1:], start=2):
        if not any(normalize_text(cell) for cell in row):
            continue

        record = {column: get_cell(row, idx) for column, idx in header_idx.items() if column}
        record[ORDER_DATE_COLUMN] = get_cell(row, date_idx)

        if not is_order_active(record):
            continue
        if normalize_text(record.get(SKLADBOT_REQUEST_NUMBER_COLUMN)):
            continue

        date_value = parse_date_to_standard(get_order_date_value(record))
        client = normalize_text(record.get("Клиент"))
        payment = normalize_text(record.get("Тип оплаты"))
        address = normalize_text(record.get("Адрес"))
        product_name = normalize_text(record.get("Товары"))
        blocks = get_plan_blocks(record)

        if not date_value or not client or not payment or not address or not product_name or blocks <= 0:
            continue

        key = build_order_sync_key(record)
        group = groups.setdefault(key, {
            "date": date_value,
            "client": client,
            "payment": payment,
            "address": address,
            "row_numbers": [],
            "products_by_key": {},
        })
        group["row_numbers"].append(row_number)
        product_key = product_sync_key(product_name)
        product = group["products_by_key"].setdefault(product_key, {
            "name": product_name,
            "blocks": 0,
        })
        product["blocks"] += blocks

    result = []
    for group in groups.values():
        group["products"] = list(group.pop("products_by_key").values())
        result.append(group)
    return result


def get_required_column_indices(header_idx):
    columns = {}
    for column in (
        SKLADBOT_REQUEST_NUMBER_COLUMN,
        SKLADBOT_REQUEST_ID_COLUMN,
        SKLADBOT_STATUS_COLUMN,
        SKLADBOT_CHECKED_AT_COLUMN,
    ):
        idx = header_idx.get(column)
        if idx is None:
            return {}
        columns[column] = idx
    return columns


def build_row_updates(row_numbers, columns, values):
    updates = []
    for row_number in row_numbers:
        for column, value in values.items():
            idx = columns.get(column)
            if idx is None:
                continue
            col = column_index_to_letter(idx)
            updates.append({
                "range": f"{col}{row_number}",
                "values": [[value]],
            })
    return updates


def match_group_to_requests(group, requests):
    return [
        request
        for request in requests
        if request_matches_order_group(group, request)
    ]


def sync_skladbot_request_numbers(sheet, candidate_requests=None, settings=None, now=None, dry_run=False):
    settings = settings or load_skladbot_settings()
    configured = skladbot_is_configured(settings)
    if candidate_requests is None and not configured:
        return {
            "enabled": False,
            "updated": 0,
            "matched": 0,
            "not_found": 0,
            "multiple": 0,
            "errors": 0,
            "message": "SkladBot API не настроен",
        }

    all_rows = sheet.get_all_values()
    if not all_rows:
        return {"enabled": configured, "updated": 0, "matched": 0, "not_found": 0, "multiple": 0, "errors": 0}

    header_idx = get_header_index(all_rows[0])
    columns = get_required_column_indices(header_idx)
    if not columns:
        return {
            "enabled": configured,
            "updated": 0,
            "matched": 0,
            "not_found": 0,
            "multiple": 0,
            "errors": 1,
            "message": "В data нет колонок SkladBot",
        }

    groups = build_order_groups_from_rows(all_rows)
    if not groups:
        return {"enabled": True, "updated": 0, "matched": 0, "not_found": 0, "multiple": 0, "errors": 0}

    checked_at = (now or datetime.now()).strftime("%d.%m.%Y %H:%M:%S")

    try:
        requests = candidate_requests if candidate_requests is not None else fetch_candidate_requests(settings=settings)
    except Exception as exc:
        logging.exception("SkladBot: не удалось получить заявки")
        return {
            "enabled": True,
            "updated": 0,
            "matched": 0,
            "not_found": 0,
            "multiple": 0,
            "errors": len(groups),
            "message": str(exc),
        }

    updates = []
    result = {
        "enabled": True,
        "updated": 0,
        "matched": 0,
        "not_found": 0,
        "multiple": 0,
        "errors": 0,
    }

    not_found_examples = []
    for group in groups:
        matches = match_group_to_requests(group, requests)
        if len(matches) == 1:
            request = matches[0]
            row_values = {
                SKLADBOT_REQUEST_NUMBER_COLUMN: request.get("number", ""),
                SKLADBOT_REQUEST_ID_COLUMN: request.get("id", ""),
                SKLADBOT_STATUS_COLUMN: SKLADBOT_STATUS_FOUND,
                SKLADBOT_CHECKED_AT_COLUMN: checked_at,
            }
            result["matched"] += 1
        elif len(matches) > 1:
            row_values = {
                SKLADBOT_REQUEST_NUMBER_COLUMN: "",
                SKLADBOT_REQUEST_ID_COLUMN: "",
                SKLADBOT_STATUS_COLUMN: SKLADBOT_STATUS_MULTIPLE,
                SKLADBOT_CHECKED_AT_COLUMN: checked_at,
            }
            result["multiple"] += 1
        else:
            row_values = {
                SKLADBOT_REQUEST_NUMBER_COLUMN: "",
                SKLADBOT_REQUEST_ID_COLUMN: "",
                SKLADBOT_STATUS_COLUMN: SKLADBOT_STATUS_NOT_FOUND,
                SKLADBOT_CHECKED_AT_COLUMN: checked_at,
            }
            result["not_found"] += 1
            if len(not_found_examples) < 5:
                not_found_examples.append(
                    f"{group.get('date')} | {group.get('client')[:40]} | {len(group.get('products', []))} тов."
                )

        updates.extend(build_row_updates(group["row_numbers"], columns, row_values))

    if updates and not dry_run:
        sheet.batch_update(updates, value_input_option="USER_ENTERED")
    result["updated"] = 0 if dry_run else len(updates)
    result["would_update"] = len(updates) if dry_run else 0

    # Диагностический итог. Без него нельзя понять из лога, что синк отработал:
    # запрос заявок логируется, а результат сопоставления — нет.
    logging.info(
        "SkladBot sync: dry_run=%s, групп=%s, заявок-кандидатов=%s, matched=%s, not_found=%s, multiple=%s, ячеек обновлено=%s, ячеек к обновлению=%s",
        dry_run,
        len(groups),
        len(requests),
        result["matched"],
        result["not_found"],
        result["multiple"],
        result["updated"],
        result["would_update"],
    )
    if not_found_examples:
        logging.info(
            "SkladBot sync: примеры not_found (до 5): %s",
            " || ".join(not_found_examples),
        )
    return result
