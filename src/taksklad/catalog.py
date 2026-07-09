from .config import DEFAULT_PIECES_PER_BLOCK
from .storage import load_data_section, mutate_data_section, save_data_section
from .utils import normalize_lookup_text, normalize_text, parse_int_value


def load_product_catalog():
    catalog = load_data_section("product_catalog", {})
    return catalog if isinstance(catalog, dict) else {}


def save_product_catalog(catalog):
    return save_data_section("product_catalog", catalog)


def merge_product_catalog_defaults(defaults):
    defaults = defaults if isinstance(defaults, dict) else {}

    def merge(catalog):
        catalog = catalog if isinstance(catalog, dict) else {}
        for key, rule in defaults.items():
            if key and key not in catalog:
                catalog[key] = rule
        return catalog

    return mutate_data_section("product_catalog", merge, default={})


def upsert_product_rule(old_key, new_key, rule):
    def upsert(catalog):
        catalog = catalog if isinstance(catalog, dict) else {}
        if old_key and old_key != new_key:
            catalog.pop(old_key, None)
        if new_key:
            catalog[new_key] = dict(rule)
        return catalog

    return mutate_data_section("product_catalog", upsert, default={})


def delete_product_rule(key):
    def delete(catalog):
        catalog = catalog if isinstance(catalog, dict) else {}
        catalog.pop(key, None)
        return catalog

    return mutate_data_section("product_catalog", delete, default={})


def product_catalog_key(product_name):
    return normalize_lookup_text(product_name)


def get_product_rule(product_name, catalog=None, create=False):
    catalog = catalog if catalog is not None else load_product_catalog()
    key = product_catalog_key(product_name)
    if not key:
        return {
            "name": "",
            "pieces_per_block": DEFAULT_PIECES_PER_BLOCK,
            "requires_kiz": True,
        }
    if key not in catalog and create:
        catalog[key] = {
            "name": normalize_text(product_name),
            "pieces_per_block": DEFAULT_PIECES_PER_BLOCK,
            "requires_kiz": True,
        }
    rule = catalog.get(key, {})
    pieces = parse_int_value(rule.get("pieces_per_block")) or DEFAULT_PIECES_PER_BLOCK
    return {
        "name": rule.get("name") or normalize_text(product_name),
        "pieces_per_block": max(1, pieces),
        "requires_kiz": bool(rule.get("requires_kiz", True)),
    }


def calculate_blocks(quantity, product_name, catalog, warnings=None):
    qty = parse_int_value(quantity)
    rule = get_product_rule(product_name, catalog=catalog, create=True)
    pieces_per_block = rule["pieces_per_block"]
    blocks = (qty + pieces_per_block - 1) // pieces_per_block if qty > 0 else 0
    if warnings is not None and qty > 0 and qty % pieces_per_block != 0:
        warnings.append(
            f"'{product_name}': количество {qty} не делится на {pieces_per_block}, "
            f"план округлён до {blocks} блок."
        )
    return blocks, pieces_per_block
