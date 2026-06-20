import os
import sys

from .config import APP_DIR
from .scan_quantities import product_key_from_name


PRODUCT_IMAGE_ASSETS = {
    "brown:op": {
        "filename": "brown_op.png",
        "gtin": "4006396053978",
    },
    "brown:ssl": {
        "filename": "brown_ssl.png",
        "gtin": "4006396054067",
    },
    "gold:ssl": {
        "filename": "gold_ssl.png",
        "gtin": "4006396054005",
    },
    "green:op": {
        "filename": "green_op.png",
        "gtin": "4006396104441",
    },
    "red:op": {
        "filename": "red_op.png",
        "gtin": "4006396053947",
    },
    "red:ssl": {
        "filename": "red_ssl.png",
        "gtin": "4006396054036",
    },
}


def product_image_roots():
    roots = [
        os.path.join(APP_DIR, "assets", "product_images"),
    ]
    bundle_root = getattr(sys, "_MEIPASS", "")
    if bundle_root:
        roots.append(os.path.join(bundle_root, "assets", "product_images"))
    project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir, os.pardir))
    roots.append(os.path.join(project_root, "assets", "product_images"))

    result = []
    seen = set()
    for root in roots:
        normalized = os.path.abspath(root)
        if normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return result


def product_image_asset(product):
    return PRODUCT_IMAGE_ASSETS.get(product_key_from_name(product), {})


def product_image_path(product):
    asset = product_image_asset(product)
    filename = asset.get("filename")
    if not filename:
        return ""
    for root in product_image_roots():
        path = os.path.join(root, filename)
        if os.path.exists(path):
            return path
    return ""


def product_image_gtin(product):
    return product_image_asset(product).get("gtin", "")
