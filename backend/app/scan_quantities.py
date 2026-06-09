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
        "scan_type": scan_type,
        "block_quantity": block_quantity_for_code(code),
        "aggregate_product_key": scan_code_product_key(code) if scan_type == SCAN_TYPE_AGGREGATE_BOX else "",
    }


def scan_block_quantity(scan):
    if isinstance(scan, str):
        return block_quantity_for_code(scan)
    raw_payload = scan.get("raw_payload") if isinstance(scan, dict) else getattr(scan, "raw_payload", None)
    raw_payload = raw_payload or {}
    value = parse_int(raw_payload.get("block_quantity"))
    if value > 0:
        return value
    code = scan.get("code", "") if isinstance(scan, dict) else getattr(scan, "code", "")
    return block_quantity_for_code(code)


def scanned_blocks_for_scans(scans):
    return sum(scan_block_quantity(scan) for scan in scans or [])


def normalize_text(value):
    return str(value or "").strip()


def parse_int(value):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0
