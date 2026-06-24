SCAN_TYPE_UNIT = "unit"
SCAN_TYPE_AGGREGATE_BOX = "aggregate_box"
AGGREGATE_BOX_BLOCK_QUANTITY = 50

AGGREGATE_BOX_PRODUCT_PREFIXES = {
    "0104006396054012": "gold:ssl",
    "0104006396053985": "brown:op",
    "0104006396053954": "red:op",
    "0104006396054074": "brown:ssl",
    "0104006396054043": "red:ssl",
    "0104006396104448": "green:op",
    "0104006396104458": "green:op",
}

UNIT_PRODUCT_PREFIXES = {
    "0104006396054005": "gold:ssl",
    "0104006396053978": "brown:op",
    "0104006396053947": "red:op",
    "0104006396054067": "brown:ssl",
    "0104006396054036": "red:ssl",
    "0104006396104441": "green:op",
}

PRODUCT_COLORS = ("brown", "red", "gold", "green")
PRODUCT_FORMATS = ("op", "ssl")


def aggregate_box_product_key(code):
    text = normalize_text(code)
    for prefix, product_key in AGGREGATE_BOX_PRODUCT_PREFIXES.items():
        if text.startswith(prefix):
            return product_key
    return ""


def unit_product_key(code):
    text = normalize_text(code)
    for prefix, product_key in UNIT_PRODUCT_PREFIXES.items():
        if text.startswith(prefix):
            return product_key
    return ""


def scan_code_product_key(code):
    return aggregate_box_product_key(code) or unit_product_key(code)


def product_key_from_name(product):
    text = normalize_text(product).casefold()
    tokens = text.replace("`", " ").replace('"', " ").replace("'", " ").split()
    compact = "".join(tokens)
    color = next((item for item in PRODUCT_COLORS if item in tokens or item in compact), "")
    product_format = next((
        item
        for item in PRODUCT_FORMATS
        if item in tokens or (color and f"{color}{item}" in compact)
    ), "")
    if color and product_format:
        return f"{color}:{product_format}"
    return ""


def scan_type_for_code(code):
    return SCAN_TYPE_AGGREGATE_BOX if aggregate_box_product_key(code) else SCAN_TYPE_UNIT


def block_quantity_for_code(code):
    if scan_type_for_code(code) == SCAN_TYPE_AGGREGATE_BOX:
        return AGGREGATE_BOX_BLOCK_QUANTITY
    return 1


def scan_metadata_for_code(code):
    scan_type = scan_type_for_code(code)
    return {
        "scan_type": scan_type,
        "block_quantity": block_quantity_for_code(code),
        "product_key": scan_code_product_key(code),
        "aggregate_product_key": aggregate_box_product_key(code) if scan_type == SCAN_TYPE_AGGREGATE_BOX else "",
    }


def scan_product_mismatch(code, product):
    product_key = product_key_from_name(product)
    if not product_key:
        return False
    code_product_key = scan_code_product_key(code)
    return not code_product_key or product_key != code_product_key


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
