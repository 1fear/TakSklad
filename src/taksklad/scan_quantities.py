SCAN_TYPE_UNIT = "unit"
SCAN_TYPE_AGGREGATE_BOX = "aggregate_box"
AGGREGATE_BOX_BLOCK_QUANTITY = 50

AGGREGATE_BOX_PRODUCT_PREFIXES = {
    "010400639605401221": "gold",
    "010400639605398521": "brown",
    "010400639605395421": "red",
}

PRODUCT_KEY_MARKERS = {
    "gold": ("gold",),
    "brown": ("brown",),
    "red": ("red",),
}


def scan_code_product_key(code):
    text = normalize_text(code)
    for prefix, product_key in AGGREGATE_BOX_PRODUCT_PREFIXES.items():
        if text.startswith(prefix):
            return product_key
    return ""


def product_key_from_name(product):
    text = normalize_text(product).casefold()
    for product_key, markers in PRODUCT_KEY_MARKERS.items():
        if any(marker in text for marker in markers):
            return product_key
    return ""


def scan_type_for_code(code):
    return SCAN_TYPE_AGGREGATE_BOX if scan_code_product_key(code) else SCAN_TYPE_UNIT


def block_quantity_for_code(code):
    if scan_type_for_code(code) == SCAN_TYPE_AGGREGATE_BOX:
        return AGGREGATE_BOX_BLOCK_QUANTITY
    return 1


def scan_metadata_for_code(code):
    scan_type = scan_type_for_code(code)
    return {
        "code": normalize_text(code),
        "scan_type": scan_type,
        "block_quantity": block_quantity_for_code(code),
        "aggregate_product_key": scan_code_product_key(code) if scan_type == SCAN_TYPE_AGGREGATE_BOX else "",
    }


def scan_entry_block_quantity(entry):
    value = parse_int((entry or {}).get("block_quantity"))
    if value > 0:
        return value
    return block_quantity_for_code((entry or {}).get("code"))


def scan_entries_for_codes(codes):
    return [scan_metadata_for_code(code) for code in codes or []]


def scan_entries_by_code(entries):
    result = {}
    for entry in entries or []:
        code = normalize_text((entry or {}).get("code"))
        if code:
            result[code] = entry
    return result


def scanned_blocks_for_entries(entries):
    return sum(scan_entry_block_quantity(entry) for entry in entries or [])


def scan_entries_for_order_codes(order, codes):
    existing_by_code = scan_entries_by_code((order or {}).get("_existing_scan_entries") or [])
    entries = []
    for code in codes or []:
        entry = existing_by_code.get(normalize_text(code))
        entries.append(entry or scan_metadata_for_code(code))
    return entries


def scanned_blocks_for_order_codes(order, codes):
    return scanned_blocks_for_entries(scan_entries_for_order_codes(order, codes))


def aggregate_product_mismatch(code, product):
    metadata = scan_metadata_for_code(code)
    if metadata["scan_type"] != SCAN_TYPE_AGGREGATE_BOX:
        return False
    product_key = product_key_from_name(product)
    return not product_key or product_key != metadata["aggregate_product_key"]


def normalize_text(value):
    return str(value or "").strip()


def parse_int(value):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0
