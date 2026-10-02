"""Эталонные ответы программы склада для сверки станции в браузере

Корпус снимается вызовом настоящих функций программы (`src/taksklad`) на фиксированных входах
и пишется в `frontend/src/station/__fixtures__/parity-corpus.json`; он единственный оракул:
тесты станции читают только его, входы каждого случая лежат в нём же
`tests/test_station_parity_corpus.py` требует, чтобы файл совпадал с тем, что программа отвечает сейчас

Запуск из корня репозитория: `PYTHONPATH=. python tools/generate_station_parity_corpus.py [--check]`
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
CORPUS_PATH = ROOT / "frontend" / "src" / "station" / "__fixtures__" / "parity-corpus.json"
TODAY = datetime(2026, 10, 2, 9, 0, 0)
LATE = datetime(2026, 10, 2, 23, 59, 59)

RED = "Chapman RED OP 20"
UNIT_CODE = {
    "red:op": "0104006396053947215" + "ABCDEFGHIJKLMNOP",
    "brown:op": "0104006396053978215" + "ABCDEFGHIJKLMNOP",
    "green:op": "0104006396104441215" + "ABCDEFGHIJKLMNOP",
    "brown:kssl": "0104006396104199215" + "ABCDEFGHIJKLMNOP",
}
BOX_CODE = {
    "red:op": "0104006396053954215" + "A" * 48,
    "green:kssl": "0104006396104236215" + "B" * 48,
}
# a legal 35-character unit code with the GS separator (\x1d) inside: the desktop counts it as two codes
GS_CODE = UNIT_CODE["red:op"][:26] + "\x1d" + UNIT_CODE["red:op"][27:]


def fixed_datetime_class(now):
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now if tz is None else now.replace(tzinfo=tz)

    return FixedDatetime


class FakeVar:
    def __init__(self):
        self.value = ""

    def set(self, value):
        self.value = value


class FakeWidget:
    def __init__(self, value=""):
        self.value = value
        self.options = {}
        self.deleted = False

    def get(self):
        return self.value

    def delete(self, *_args):
        self.value = ""
        self.deleted = True

    def config(self, **kwargs):
        self.options.update(kwargs)

    configure = config

    def focus_set(self):
        pass


# one order position, every field spelled out: the desktop row and the station row are both built from it
ROW_DEFAULTS = {
    "date": "02.10.2026", "payment": "Перечисление", "client": "ООО Тест", "address": "Ташкент, ул. Пример, 1",
    "representative": "Иванов", "request": "WH-R-101", "request_id": "", "order": "order-1", "item": "item-1",
}


def pos(product, plan, codes=(), **fields):
    row = {**ROW_DEFAULTS, "product": product, "plan": plan, "codes": list(codes), **fields}
    row.setdefault("entries", [])
    row.setdefault("total", plan * 240000)
    return row


def desktop_row(spec):
    from taksklad.config import ORDER_DATE_COLUMN, SKLADBOT_REQUEST_ID_COLUMN, SKLADBOT_REQUEST_NUMBER_COLUMN

    return {
        ORDER_DATE_COLUMN: spec["date"],
        "Тип оплаты": spec["payment"],
        "Клиент": spec["client"],
        "Адрес": spec["address"],
        "Торговый представитель": spec["representative"],
        "Товары": spec["product"],
        "Кол-во блок": spec["plan"],
        "Сумма позиции": spec["total"],
        SKLADBOT_REQUEST_NUMBER_COLUMN: spec["request"],
        SKLADBOT_REQUEST_ID_COLUMN: spec["request_id"],
        "_backend_order_id": spec["order"],
        "_backend_order_item_id": spec["item"],
        "_existing_scanned_codes": list(spec["codes"]),
        "_existing_scan_entries": [dict(entry) for entry in spec["entries"]],
    }


def spec_of(row):
    """The inverse of desktop_row, for rows the desktop built itself"""
    from taksklad.config import ORDER_DATE_COLUMN, SKLADBOT_REQUEST_ID_COLUMN, SKLADBOT_REQUEST_NUMBER_COLUMN

    return {
        "date": row[ORDER_DATE_COLUMN], "payment": row["Тип оплаты"], "client": row["Клиент"], "address": row["Адрес"],
        "representative": row["Торговый представитель"], "product": row["Товары"], "plan": row["Кол-во блок"],
        "total": row["Сумма позиции"], "request": row[SKLADBOT_REQUEST_NUMBER_COLUMN], "request_id": row[SKLADBOT_REQUEST_ID_COLUMN],
        "order": row["_backend_order_id"], "item": row["_backend_order_item_id"], "codes": list(row["_existing_scanned_codes"]),
        "entries": [{"code": e["code"], "block_quantity": e["block_quantity"]} for e in row["_existing_scan_entries"]],
    }


# ---- tables, blocklist, palette ----


def tables():
    from taksklad import desktop_scan_rules as rules
    from taksklad import scan_quantities as quantities

    return {
        "unit_prefixes": dict(quantities.UNIT_PRODUCT_PREFIXES),
        "aggregate_prefixes": dict(quantities.AGGREGATE_BOX_PRODUCT_PREFIXES),
        "aggregate_block_quantity": quantities.AGGREGATE_BOX_BLOCK_QUANTITY,
        "product_key_labels": dict(rules.PRODUCT_KEY_LABELS),
    }


def blocklist():
    """SHA-256 of the blocked codes: the browser bundle is public, so the codes themselves never leave this function"""
    from taksklad import kiz_blocklist

    codes = list(kiz_blocklist.BLOCKED_KIZ_CODES)
    reasons = {kiz_blocklist.blocked_kiz_reason(code) for code in codes}
    assert len(reasons) == 1, "a code with its own refusal text needs its own corpus field"
    sample = UNIT_CODE["red:op"]
    return {
        "reason": reasons.pop(),
        "sha256": sorted(hashlib.sha256(code.strip(" \t\r\n").encode("utf-8")).hexdigest() for code in codes),
        "sample": {"text": sample, "sha256": hashlib.sha256(sample.encode("utf-8")).hexdigest()},
    }


def gtin_badge_color():
    """The GTIN badge colour is written inline in app_layout.py, so it is read from the source"""
    source = (ROOT / "src" / "taksklad" / "app_layout.py").read_text(encoding="utf-8")
    match = re.search(r'text="GTIN",\s+bg=FG_TEXT,\s+fg="(#[0-9a-fA-F]{6})"', source)
    assert match, "GTIN badge colour not found in app_layout.py"
    return match.group(1)


def palette():
    from taksklad import app_layout, config, order_list_widgets
    from taksklad.ui_widgets import fade_hex

    desktop = {
        name: value for name, value in vars(config).items()
        if name.isupper() and isinstance(value, str) and re.fullmatch(r"#[0-9a-f]{6}", value)
    }
    widgets = order_list_widgets
    window = {
        "LIST_SURFACE_BG": widgets.LIST_SURFACE_BG,
        "SELECTED_CARD_BG": widgets.SELECTED_CARD_BG,
        "PLACEHOLDER_FG": widgets.PLACEHOLDER_FG,
        "PRODUCT_PHOTO_BG": app_layout.PRODUCT_PHOTO_BG,
        "PRODUCT_PHOTO_SHELL_BG": app_layout.PRODUCT_PHOTO_SHELL_BG,
        "GTIN_BADGE_FG": gtin_badge_color(),
        "SCROLLBAR_BG": widgets.SCROLLBAR_BG,
        "SCROLLBAR_THUMB": widgets.SCROLLBAR_THUMB,
        "SCROLLBAR_THUMB_HOVER": fade_hex(widgets.SCROLLBAR_THUMB, 0.12),
    }
    # the button hover rule (amount 0.10) on every colour, the scrollbar hover, and the edges: bad input, clamping
    samples = [(color, 0.10) for color in desktop.values()] + [(widgets.SCROLLBAR_THUMB, 0.12)]
    samples += [("#abc", 0.10), ("#zzzzzz", 0.10), ("b28224", 0.10), ("#b28224", 2), ("#b28224", -1)]
    return {
        "desktop": desktop,
        "window": window,
        "fade": [{"color": color, "amount": amount, "result": fade_hex(color, amount)} for color, amount in samples],
    }


# ---- text, dates, numbers ----

DATES = [
    "02.10.2026", "03.10.2026", "01.10.2026", "04.10.2026", "2026-10-05", "05/10/2026", "Без даты", "", "'02.10.2026'",
    "02.10.2026 10:00", "1.2.2026", "02.10.26", "69.01.01", "2026.10.02", "30.02.2026", "29.02.2024", "13/10/2026",
    "10/13/2026", "0.10.2026", "32.10.2026", "01.13.2026", "00.00.0000", "02.10.2026x", " 02.10.2026 ", "\x1d02.10.2026",
    "'02.10.2026", '""02.10.2026""', "02.10.2026\t10:00", "2026-10-2", "02.10.69", "02.10.68", "1.1.99", "2026-1-1",
    "0001-01-01", "0000-01-01", "'", "x y", 0, "0", None,
]
INTS = [
    "1_000", "1__0", "_1", "1e3", "1E3", "1e+3", "-1e3", "1e-3", "+5", "-5.9", "5.", ".5", "1,5", "1,234,5", "1.2.3", "--1",
    "1e", "e5", "1 200,7", "inf", "nan", "1e400", "0x10", "  7 ", "1\t000", "-0.5", 2.7, -2.7, -0.2, float("nan"),
    float("inf"), 10**20, "", None, "abc", "12abc",
]
MONEY = [0, 240000, 1234567, "1 200 000", "abc", -5, 999, "1 200,7"]
STRIP = ["\x1dx\x1d", "\ufeffx", "\x85x\x85", "\xa0x\xa0", "\u3000x", "  plain  ", 0, None, "0"]
LOOKUP = ["  Ёлка: ООО*  ", "A\ufeffB  C", "\x1dX\x1d", "WH-R-12 ", "a\xa0b", "ТЕСТ тест", "ТЕСТ\u2028тест"]
# strings that Python orders by code point and JavaScript by UTF-16 unit: an emoji against a full-width letter
SORTED_TEXT = ["abd", "abc", "ab", "", "а", "Я", "я", "\U0001F600", "Ａ", "Ёж", "WH-R-9", "WH-R-10"]


def float_safe(value):
    """JSON has no NaN or Infinity: they travel as {"float": "NaN"} and {"float": "Infinity"}"""
    if isinstance(value, float) and (value != value or abs(value) == float("inf")):
        return {"float": "NaN" if value != value else "Infinity"}
    return value


def text_cases():
    from taksklad import desktop_scan_rules as rules
    from taksklad import utils

    def header_at(now, value):
        with mock.patch.object(rules, "datetime", fixed_datetime_class(now)):
            return rules.format_order_date_header(value)

    epoch = datetime(1970, 1, 1)
    dates = []
    for value in DATES:
        key = rules.date_sort_key(value)
        dates.append({
            "value": value,
            "standard": utils.parse_date_to_standard(value),
            "header": header_at(TODAY, value),
            "header_late": header_at(LATE, value),
            "sort_day": None if key == datetime.max else (key - epoch).days,
        })
    return {
        "dates": dates,
        "ints": [{"value": float_safe(v), "result": utils.parse_int_value(v)} for v in INTS],
        "money": [{"value": v, "text": rules.format_money(v)} for v in MONEY],
        "strip": [{"value": v, "result": utils.normalize_text(v)} for v in STRIP],
        "lookup": [{"value": v, "result": utils.normalize_lookup_text(v)} for v in LOOKUP],
        "sorted": {"values": SORTED_TEXT, "result": sorted(SORTED_TEXT)},
    }


# ---- product keys, labels, mismatch ----

PRODUCT_NAMES = [
    RED, "Chapman Brown OP 20", "Chapman Gold SSL", "Chapman RED SSL 100`20", "Chapman Green OP", "Chapman Brown KSSL",
    "Chapman KSSL Green", "chapman brownop", "Unmapped Product", "", " Мёд ", "Chapman Ｒed OP", "CHAPMAN GREEN KSSL",
    "Chapman `Red` 'op'", "red\x1dop", "red\xa0ssl", "gold  ssl", "Chapman Red SSL OP", "greenkssl", "kssl green",
    "\ufeffred op", "Red\ufeffop", "gold ßl", "GOLD ſsl", "red ﬁ", "Red\x1cop", "red\x85ssl", "kssl\u2003green",
    '"brown"op', "brown\tkssl",
]
MISMATCHES = [
    (UNIT_CODE["red:op"], "Неизвестный товар"), ("0100000000000000021X", ""), (UNIT_CODE["brown:op"], RED),
    ("0104006396099999215XYZ", RED), (UNIT_CODE["red:op"], RED), (UNIT_CODE["red:op"], "Chapman Brown OP"),
    (BOX_CODE["red:op"], RED), (BOX_CODE["red:op"], "Chapman Brown OP"), (BOX_CODE["red:op"], "Неизвестный товар"),
]


def product_cases():
    from taksklad import desktop_scan_rules as rules
    from taksklad import scan_quantities as quantities

    codes = [prefix + "215" + "ABCDEFGHIJKLMNOP" for prefix in quantities.UNIT_PRODUCT_PREFIXES]
    codes += [prefix + "215" + "A" * 48 for prefix in quantities.AGGREGATE_BOX_PRODUCT_PREFIXES]
    codes += ["0104006396099999215XYZ", "", GS_CODE]
    label_keys = list(rules.PRODUCT_KEY_LABELS) + ["blue:op", "constructor", "  ", None]
    return {
        "names": [
            {"product": p, "key": quantities.product_key_from_name(p), "guard": rules.scan_sku_guard_status({"Товары": p})}
            for p in PRODUCT_NAMES
        ],
        "guard_none": rules.scan_sku_guard_status(None),
        "code_keys": [
            {"code": c, "key": quantities.scan_code_product_key(c), "scan_type": quantities.scan_type_for_code(c),
             "blocks": quantities.block_quantity_for_code(c)}
            for c in codes
        ],
        "mismatch": [
            {"code": c, "product": p, "mismatch": quantities.scan_product_mismatch(c, p),
             "box_mismatch": quantities.aggregate_product_mismatch(c, p)}
            for c, p in MISMATCHES
        ],
        "labels": [{"key": k, "label": rules.format_product_key_label(k)} for k in label_keys],
    }


# ---- scan ----

OWNER = {"client": "ООО Другой", "request": "WH-R-7", "date": "01.10.2026"}
BACKEND_NO = {"available": False, "latest_movement_type": "outbound"}
UNIT_CODES_10 = [UNIT_CODE["red:op"][:-2] + f"{n:02d}" for n in range(10)]

RAW = UNIT_CODE["red:op"]


def attempt(name, raw=RAW, product=RED, plan=2, **extra):
    return {"name": name, "raw": raw, "product": product, "plan": plan, **extra}


def duplicate(name, availability, **extra):
    """The code is held by an order of another client, and the backend is asked whether it may be scanned again"""
    return attempt(name, plan=3, owner=OWNER, availability=availability, **extra)


SCAN_CASES = [
    attempt("accept_unit"),
    attempt("accept_box", BOX_CODE["red:op"], plan=50),
    attempt("accept_last_block_last_position", plan=1),
    attempt("accept_last_block_not_last_position", plan=1, positions=2),
    attempt("accept_unknown_product", product="Неизвестный товар", plan=3),
    attempt("accept_trailing_line_break", RAW + "\r\n"),
    attempt("accept_gs_code", GS_CODE),
    attempt("empty_after_trim", "   "),
    attempt("update_required", update_required=True),
    attempt("update_required_before_position", update_required=True, no_rows=True),
    attempt("no_rows", no_rows=True),
    attempt("format_prefix", "02" + "1" * 33),
    attempt("format_length", RAW + "X"),
    attempt("blocked_code", plan=3, blocked=True),
    attempt("blocked_code_before_plan_check", plan=0, blocked=True),
    attempt("blocked_lookup_after_format_check", "02123", plan=3, blocked=True),
    attempt("plan_zero", plan=0),
    attempt("plan_done", UNIT_CODE["brown:op"], plan=1, scanned=[RAW]),
    attempt("sku_mismatch", UNIT_CODE["brown:op"]),
    attempt("box_wrong_product", BOX_CODE["green:kssl"], "Неизвестный товар", 60),
    attempt("box_exceeds_rest", BOX_CODE["red:op"], plan=10),
    attempt("box_exceeds_rest_partly_scanned", BOX_CODE["red:op"], plan=55, scanned=UNIT_CODES_10),
    attempt("same_position_duplicate", plan=3, scanned=[RAW]),
    duplicate("duplicate_other_order_backend_refuses", BACKEND_NO),
    duplicate("duplicate_other_order_backend_releases", {"available": True, "latest_movement_type": "return"}),
    duplicate("duplicate_backend_available_but_outbound", {"available": True, "latest_movement_type": "outbound"}),
    duplicate("duplicate_backend_refuses_after_return", {"available": False, "latest_movement_type": "return"}),
    duplicate("duplicate_backend_available_without_movement", {"available": True, "latest_movement_type": ""}),
    duplicate("duplicate_backend_no_verdict", {"available": False}),
    duplicate("duplicate_released_by_undo", {"available": True, "latest_movement_type": "undo"}),
    duplicate("duplicate_released_by_upper_case_return", {"available": True, "latest_movement_type": "RETURN"}),
    duplicate("duplicate_released_by_padded_reset", {"available": True, "latest_movement_type": " Reset "}),
    duplicate("duplicate_backend_check_failed", "error"),
    duplicate("duplicate_position_without_backend_item", {"available": True, "latest_movement_type": "return"}, item=""),
    duplicate("duplicate_gs_code_other_order", BACKEND_NO, raw=GS_CODE),
    attempt("duplicate_completed_today", plan=3, completed=[RAW], availability=BACKEND_NO),
    attempt("duplicate_completed_released", plan=3, completed=[RAW], availability={"available": True, "latest_movement_type": "undo"}),
    attempt("no_backend_item", plan=3, item=""),
]


def scan_input(case):
    """The full input of a scan case: the screen state, the rows and what the backend says"""
    positions = [] if case.get("no_rows") else range(1, case.get("positions", 1) + 1)
    rows = [pos(case["product"], case["plan"], item=case.get("item", f"item-{n}")) for n in positions]
    scanned = list(case.get("scanned", []))
    booked, owner_rows = list(scanned), []
    if "owner" in case:
        code = case["raw"].strip()
        owner = case["owner"]
        owner_rows = [pos(case["product"], 5, [code], date=owner["date"], request=owner["request"], client=owner["client"], item="item-owner")]
        booked.append(code)
    return {
        "raw": case["raw"],
        "rows": rows,
        "scanned": scanned,
        "booked": booked,
        "owner_rows": owner_rows,
        "completed": list(case.get("completed", [])),
        "availability": case.get("availability"),
        "blocked": case.get("blocked", False),
        "update_required": case.get("update_required", False),
    }


def run_scan_case(scan):
    from taksklad.main import ScanningApp
    import taksklad.app_scanning as app_scanning
    import taksklad.backend_flow as backend_flow
    from taksklad.kiz_blocklist import BLOCK_REASON_DEFAULT

    rows = [desktop_row(spec) for spec in scan["rows"]]
    fake = SimpleNamespace(
        ensure_update_allowed=lambda: not scan["update_required"],
        operation_in_progress=False,
        current_order=rows[0] if rows else None,
        current_product_idx=0,
        current_legal_entity_orders=rows,
        scanned_codes=list(scan["scanned"]),
        all_existing_codes=set(scan["booked"]),
        today_orders=[desktop_row(spec) for spec in scan["owner_rows"]],
        completed_orders=[{"Коды": scan["completed"]}] if scan["completed"] else [],
        scan_entry=FakeWidget(scan["raw"]),
        progress_label=FakeWidget(),
        last_code_label=FakeWidget(),
        status_var=FakeVar(),
        status_label=FakeWidget(),
        next_product_btn=FakeWidget(),
        finish_btn=FakeWidget(),
        show_error=mock.Mock(),
        show_busy_error=mock.Mock(),
        log_duplicate_code_async=mock.Mock(),
        bell=mock.Mock(),
    )

    def lookup(_code, order_item_id=""):
        if scan["availability"] == "error":
            raise backend_flow.BackendApiError("backend is down")
        return scan["availability"]

    # the real backend_duplicate_scan_reuse_status runs: only the network lookup and the blocklist are replaced
    with (
        mock.patch.object(app_scanning, "write_scan_backup", return_value=True),
        mock.patch.object(app_scanning, "queue_backend_scan") as queue_scan,
        mock.patch.object(app_scanning, "blocked_kiz_reason", return_value=BLOCK_REASON_DEFAULT if scan["blocked"] else ""),
        mock.patch.object(backend_flow, "backend_enabled", return_value=True),
        mock.patch.object(backend_flow, "lookup_kiz_availability", side_effect=lookup),
        mock.patch.object(app_scanning.ScanningActionsMixin, "prompt_kiz_release") as prompt_release,
    ):
        ScanningApp.on_scan(fake)

    return {
        "state": getattr(fake, "scan_feedback_state", "ignored"),
        "message": getattr(fake, "last_scan_feedback_message", ""),
        "scanned_codes_after": list(fake.scanned_codes),
        "queued": queue_scan.call_count,
        "progress_text": fake.progress_label.options.get("text", ""),
        "last_code_text": fake.last_code_label.options.get("text", ""),
        "status_text": fake.status_var.value,
        "next_enabled": fake.next_product_btn.options.get("state", ""),
        "finish_enabled": fake.finish_btn.options.get("state", ""),
        "bell": fake.bell.call_count,
        "release_prompt": prompt_release.call_count,
        "entry_cleared": fake.scan_entry.deleted,
    }


def scan_cases():
    cases = []
    for case in SCAN_CASES:
        scan = scan_input(case)
        cases.append({"name": case["name"], "input": scan, "output": run_scan_case(scan)})
    return cases


def owner_cases():
    from taksklad.desktop_scan_rules import find_code_owner_in_orders

    holder = pos(RED, 5, [UNIT_CODE["red:op"]], client="ООО Другой", request="WH-R-7", date="01.10.2026", item="item-owner")
    gs_holder = pos(RED, 5, [GS_CODE], client="ООО Другой", request="WH-R-7", date="01.10.2026", item="item-owner")
    cases = []
    for name, code, rows in (
        ("first_row_that_holds_the_code", f" {UNIT_CODE['red:op']}\n", [pos(RED, 3), holder]),
        ("empty_code", "", [holder]),
        ("unknown_code", "0100000000000000021X", [holder]),
        ("gs_code", GS_CODE, [gs_holder]),
    ):
        found = find_code_owner_in_orders(code, [desktop_row(spec) for spec in rows])
        owner = {
            "client": found["client"], "date": found["order_date_display"], "product": found["product"],
            "request": found["skladbot_request_number"],
        } if found else None
        cases.append({"name": name, "input": {"code": code, "rows": rows}, "output": owner})
    return cases


def duplicate_messages():
    from taksklad import desktop_scan_rules as rules

    owner = {"client": "ООО Другой", "order_date_display": "01.10.2026", "product": RED, "skladbot_request_number": "WH-R-7"}
    outbound = {"checked": True, "available": False, "latest_movement_type": "outbound", "reason": "latest movement is outbound"}
    released = {"checked": True, "available": True, "latest_movement_type": "return", "reason": "latest movement is return"}
    refused = {"checked": True, "available": False, "latest_movement_type": "", "reason": "no reason"}
    failed = {"checked": False, "available": False, "reason": "backend availability check failed"}
    pairs = [({}, {}), (owner, outbound), ({}, failed), (owner, released), (owner, refused)]
    return [
        {"code": UNIT_CODE["red:op"], "owner": o, "status": s, "text": rules.format_duplicate_scan_message(UNIT_CODE["red:op"], o, s)}
        for o, s in pairs
    ]


# ---- order list ----


def card(request, client, date, blocks, product=RED):
    return pos(product, blocks, request=request, client=client, date=date, payment="T", address="A", representative="R", total=0)


def listed(request, client, payment, address, date, product, blocks):
    return pos(product, blocks, request=request, client=client, payment=payment, address=address, date=date,
               representative="Петров", total=0)


MAIN_LIST = [
    listed("WH-R-12", "ООО Бета", "Перечисление", "Адрес Б", "02.10.2026", RED, 5),
    listed("WH-R-12", "ООО Бета", "Перечисление", "Адрес Б", "02.10.2026", "Chapman Brown OP 20", 3),
    listed("WH-R-3", "ООО Альфа", "Терминал", "Адрес А", "02.10.2026", "Chapman Gold SSL", 2),
    listed("", "ИП Гамма", "", "", "03.10.2026", "Chapman Green OP", 1),
    listed("WH-R-40", "ООО Дельта", "Перечисление", "Адрес Д", "01.10.2026", "Chapman RED SSL", 4),
    listed("WR-9", "ООО Ёлка", "Перечисление", "Адрес Ё", "", "Chapman Brown KSSL", 6),
    listed("WH-R-5", "ООО Зета", "Терминал", "Адрес З", "2026-10-05", RED, 7),
]
SEARCH_LIST = [card("WH-R-1", "К1", "02.10.2026", 2), card("WH-R-1", "К1", "02.10.2026", 3, "Chapman Gold SSL"),
               card("WH-R-2", "К2", "02.10.2026", 1)]

ORDER_LIST_CASES = [(f"search {search or '(empty)'}", MAIN_LIST, search) for search in ["", "бета", "WH-R-3", "без номера", "kssl", "нет такого"]] + [
    ("one_date_two_spellings", [card("WH-R-2", "К1", "2026-10-05", 1), card("WH-R-1", "К2", "05.10.2026", 2)], ""),
    ("card_takes_date_of_first_position", [card("WH-R-1", "К1", "02.10.2026", 2), card("WH-R-1", "К1", "03.10.2026", 3)], ""),
    ("unreadable_dates_keep_first_seen_order", [
        card("WH-R-1", "К1", "Без даты", 2), card("WH-R-2", "К2", "", 3), card("WH-R-3", "К3", "x", 3), card("WH-R-4", "К4", "03.10.2026", 3),
    ], ""),
    ("cards_sorted_by_number_then_code_point", [
        card("", "Ёж", "02.10.2026", 1), card("WH-R-10", "б", "02.10.2026", 1), card("WH-R-9", "а", "02.10.2026", 1),
        card("WH-R-9", "Б", "02.10.2026", 1), card("A-9", "я", "02.10.2026", 1), card("WH-R-9", "\U0001F600", "02.10.2026", 1),
        card("WH-R-9", "Ａ", "02.10.2026", 1),
    ], ""),
    ("empty_parts_of_the_key", [pos(RED, 1, request=" WH-R-1 ", client="", payment="", address="")], ""),
    ("search_counts_every_position_of_a_card", SEARCH_LIST, "gold"),
    ("search_is_trimmed_and_lowercased", SEARCH_LIST, "  R  "),
    ("search_by_request_and_client", SEARCH_LIST, "wh-r-2 к2"),
    ("search_by_placeholder_text", [card("", "К1", "02.10.2026", 1)], "без номера skladbot"),
    ("no_rows", [], ""),
]


def order_list_cases():
    from taksklad import order_list_models

    cases = []
    for name, rows, search in ORDER_LIST_CASES:
        model = order_list_models.build_order_list_model([desktop_row(spec) for spec in rows], search)
        listed_rows = [
            {"kind": "date", "title": row.title} if row.kind == "date" else
            {"kind": "order", "client": row.client, "meta": row.meta_text, "summary": row.summary_text, "group_key": list(row.group_key)}
            for row in model.rows
        ]
        output = {"rows": listed_rows, "subtitle": model.subtitle_text, "counter": model.counter_text}
        cases.append({"name": name, "input": {"rows": rows, "search": search}, "output": output})
    return cases


# ---- rows from the API, block counting ----

UNIT = UNIT_CODE["red:op"]
BOX = BOX_CODE["red:op"]
API_ORDERS = [
    {"id": "o-1", "order_date": "2026-10-03", "payment_type": "Перечисление", "client": "ООО Тест", "address": "Ташкент",
     "representative": None, "status": "new", "skladbot_request_number": "WH-R-9", "skladbot_request_id": "r-9",
     "items": [
         {"id": "i-1", "product": RED, "quantity_pieces": 600, "quantity_blocks": 60, "block_price": 240000,
          "line_total": 14400000, "scanned_blocks": 50, "status": "new", "scan_codes": [BOX],
          "scan_entries": [{"code": BOX, "scan_type": "aggregate_box", "block_quantity": 50, "scanned_at": None}]},
         {"id": "i-2", "product": "Chapman Brown OP 20", "quantity_pieces": 30, "quantity_blocks": 3, "scanned_blocks": 0,
          "status": "new", "scan_codes": [UNIT, BOX], "scan_entries": []},
         {"id": "i-3", "product": "", "quantity_blocks": 0, "scan_codes": []},
     ]},
    {"id": "o-2", "order_date": None, "payment_type": "", "client": "", "address": "", "representative": "Иванов",
     "skladbot_request_number": "", "skladbot_request_id": "",
     "items": [{"id": "i-4", "product": "Chapman Gold SSL", "quantity_blocks": 2, "line_total": 480000, "scan_codes": []}]},
]
BLOCK_CASES = [
    ("box_is_fifty_unit_is_one", [], [UNIT, BOX, UNIT]),
    ("recorded_quantity_wins_unless_not_positive", [{"code": UNIT, "block_quantity": 7}, {"code": BOX, "block_quantity": 0}], [UNIT, BOX]),
    ("last_entry_of_a_code_wins", [{"code": UNIT, "block_quantity": 4}, {"code": UNIT, "block_quantity": 5}, {"code": "  ", "block_quantity": 9}], [UNIT]),
    ("no_codes", [], []),
]


def rows_cases():
    from taksklad.backend_client import backend_order_to_rows

    return [{"name": f"order {order['id']}", "input": order, "output": [spec_of(row) for row in backend_order_to_rows(order)]} for order in API_ORDERS]


def block_cases():
    from taksklad.scan_quantities import scanned_blocks_for_order_codes

    cases = []
    for name, entries, codes in BLOCK_CASES:
        spec = pos(RED, 60, entries=entries)
        cases.append({"name": name, "input": {"row": spec, "codes": codes}, "output": scanned_blocks_for_order_codes(desktop_row(spec), codes)})
    return cases


# ---- position screen, party summary, first incomplete position ----

LATER = {"request": "WH-R-102", "date": "03.10.2026"}
POSITION_CASES = [
    ("fresh_first_of_two", [pos(RED, 2), pos("Chapman Brown OP 20", 3, **LATER)], 0),
    ("partly_scanned", [pos(RED, 3, [UNIT_CODE["red:op"]])], 0),
    ("done_not_last", [pos(RED, 1, [UNIT_CODE["red:op"]]), pos("Chapman Gold SSL", 2, **LATER)], 0),
    ("done_last", [pos("Chapman Gold SSL", 1), pos(RED, 1, [UNIT_CODE["red:op"]], **LATER)], 1),
    ("box_scanned", [pos(RED, 60, [BOX_CODE["red:op"]])], 0),
    ("empty_names", [pos("", 0, client="", address="", total=0)], 0),
    ("gs_code_is_one_code", [pos(RED, 2, [GS_CODE])], 0),
    ("negative_line_total", [pos(RED, 1, total=-1234)], 0),
    ("past_the_last_position", [pos(RED, 1)], 1),
    ("no_rows", [], 0),
    ("party_many_requests_and_dates", [pos(RED, 1, request="WH-R-9"), pos(RED, 1, request="WH-R-10", date="2026-10-05"),
                                       pos(RED, 1, request="WH-R-100", date="05.10.2026"), pos(RED, 1, request="", date="")], 0),
    ("party_requests_by_code_point", [pos(RED, 1, request="\U0001F600"), pos(RED, 1, request="Ａ", date="03.10.2026")], 0),
    ("party_without_request_and_date", [pos(RED, 1, request="", date="")], 0),
    ("party_free_text_date", [pos(RED, 1, request="", date="Без даты"), pos(RED, 1, request="", date="")], 0),
    ("party_unreadable_date_last", [pos(RED, 1, request="A", date="'01.10.2026'"), pos(RED, 1, request="B", date="30.02.2026"),
                                    pos(RED, 1, request="C", date="04.10.2026")], 0),
]
FIRST_INCOMPLETE_CASES = [
    ("all_fresh", [pos("A", 2), pos("B", 1)]),
    ("first_done", [pos("A", 1, [UNIT_CODE["red:op"]]), pos("B", 1)]),
    ("all_done", [pos("A", 1, [UNIT_CODE["red:op"]]), pos("B", 1, [UNIT_CODE["brown:op"]])]),
    ("zero_plan_first", [pos("A", 0), pos("B", 1)]),
    ("box_fills", [pos(RED, 50, [BOX_CODE["red:op"]]), pos("B", 1)]),
    ("gs_code_counts_once", [pos("A", 2, [GS_CODE])]),
    ("no_rows", []),
]


def run_position_case(rows, index):
    from taksklad.main import ScanningApp

    fake = SimpleNamespace(
        current_product_idx=index,
        current_legal_entity_orders=[desktop_row(spec) for spec in rows],
        product_catalog={},
        current_info=FakeWidget(),
        current_client_label=FakeWidget(),
        current_product_label=FakeWidget(),
        position_label=FakeWidget(),
        progress_label=FakeWidget(),
        next_product_btn=FakeWidget(),
        finish_btn=FakeWidget(),
        undo_btn=FakeWidget(),
        codes_btn=FakeWidget(),
        scan_entry=FakeWidget(),
        last_code_label=FakeWidget(),
        party_summary_label=FakeWidget(),
        update_product_photo=lambda _name: None,
        set_scan_entry_enabled=lambda *_args, **_kwargs: None,
        update_scan_guard_status=lambda: None,
    )
    ScanningApp.load_current_product(fake)
    ScanningApp.update_party_summary_display(fake)
    return {
        "info": fake.current_info.options.get("text", ""),
        "client": fake.current_client_label.options.get("text", ""),
        "product": fake.current_product_label.options.get("text", ""),
        "position": fake.position_label.options.get("text", ""),
        "progress": fake.progress_label.options.get("text", ""),
        "last_code": fake.last_code_label.options.get("text", ""),
        "next_state": fake.next_product_btn.options.get("state", ""),
        "finish_state": fake.finish_btn.options.get("state", ""),
        "party": fake.party_summary_label.options.get("text", ""),
    }


def position_cases():
    return [
        {"name": name, "input": {"rows": rows, "index": index}, "output": run_position_case(rows, index)}
        for name, rows, index in POSITION_CASES
    ]


def first_incomplete_cases():
    from taksklad.desktop_scan_rules import first_incomplete_order_index

    return [
        {"name": name, "input": {"rows": rows}, "output": {"index": first_incomplete_order_index([desktop_row(spec) for spec in rows])}}
        for name, rows in FIRST_INCOMPLETE_CASES
    ]


def build_corpus():
    from taksklad import desktop_scan_rules

    with mock.patch.object(desktop_scan_rules, "datetime", fixed_datetime_class(TODAY)):
        return {
            "today": TODAY.isoformat(),
            "today_late": LATE.isoformat(),
            "tables": tables(),
            "blocklist": blocklist(),
            "palette": palette(),
            "text": text_cases(),
            "products": product_cases(),
            "scan": scan_cases(),
            "owners": owner_cases(),
            "duplicate_messages": duplicate_messages(),
            "order_list": order_list_cases(),
            "rows": rows_cases(),
            "blocks": block_cases(),
            "position": position_cases(),
            "first_incomplete": first_incomplete_cases(),
        }


def corpus_text(corpus):
    """The corpus as JSON with every invisible non-ASCII character (BOM, no-break space, U+2028) written as \\uXXXX"""
    text = json.dumps(corpus, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    return "".join(c if c.isascii() or c.isprintable() else f"\\u{ord(c):04x}" for c in text)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="сравнить с файлом корпуса, не переписывая его")
    args = parser.parse_args(argv)
    if str(ROOT / "src") not in sys.path:
        sys.path.insert(0, str(ROOT / "src"))
    text = corpus_text(build_corpus())
    if args.check:
        current = CORPUS_PATH.read_text(encoding="utf-8") if CORPUS_PATH.exists() else ""
        if current != text:
            print("STATION_PARITY_CORPUS_STALE")
            return 1
        print("STATION_PARITY_CORPUS_OK")
        return 0
    CORPUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    CORPUS_PATH.write_text(text, encoding="utf-8")
    print(f"written {CORPUS_PATH.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
