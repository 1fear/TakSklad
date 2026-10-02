# Логика станции без вёрстки: план реализации (план 2)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** вся логика станции склада в браузере, проверенная ответами самой программы, плюс вход станции на `/`,
полная загрузка заказов и очередь сканов с ритмом программы; вёрстки окон здесь нет, она в плане 3

**Architecture:** программа склада (`src/taksklad`) становится оракулом: генератор вызывает её настоящие функции
на фиксированных входах и пишет ответы в корпус `frontend/src/station/__fixtures__/parity-corpus.json`; модули
`frontend/src/station/logic/` повторяют правила программы и проверяются этим корпусом; Python-тест держит корпус
свежим, второй Python-тест сверяет тексты сообщений станции с кодом программы; вход станции и очередь
опираются на уже работающие `api/core.ts` и офлайн-очередь `features/warehouse/offline`

**Tech Stack:** TypeScript 5 strict, React 19, Vitest (jsdom, msw), ESLint; Python 3.12 unittest

**Spec:** [docs/superpowers/specs/2026-10-02-warehouse-station-web-design.md](../specs/2026-10-02-warehouse-station-web-design.md),
разделы 1, 3, 4, 7; опись окон: [2026-10-02-warehouse-station-web-inventory.md](../specs/2026-10-02-warehouse-station-web-inventory.md);
дорожная карта: [2026-10-02-warehouse-station-roadmap.md](2026-10-02-warehouse-station-roadmap.md)

## Global Constraints

- рабочее дерево: `git worktree add --detach /tmp/station-plan2 origin/main`, ветка `feat/station-logic`; коммиты
  с `ALLOW_NON_MAIN_BRANCH=1`, файлы поимённо, никогда `git add -A` или каталог
- в worktree нет `.venv` и `node_modules`: Python `/Users/anton/Documents/work/TakSklad/.venv/bin/python`,
  запуск из корня дерева с `PYTHONPATH=.`; фронт `npm ci` в `frontend/` один раз в начале
- оракул это программа: ответ станции на любой вход из корпуса обязан совпасть с ответом программы, кроме
  перечня согласованных отличий ниже; корпус только генерируется (`tools/generate_station_parity_corpus.py`),
  руками не правится; `tests/test_station_parity_corpus.py` держит его свежим
- согласованные отличия (спецификация, раздел 7, и решения при прототипе):
  - сообщение о несовпадении товара: строки 6 и 7 «Версия сайта: {версия}» и «Если SKU на блоке верный, обновите страницу (F5).»
  - код КИЗ с символом GS (`\x1d`) станция считает одним кодом; программа режет его на два в `split_codes`
    и из-за этого ошибается в выборе первой недобранной позиции и в поиске владельца кода;
    в корпусе ровно три таких случая помечены в тестах одной строкой комментария
  - занятая станция отвечает `state: "busy"` (у программы вместо этого окно занятости)
- тексты сообщений станции на кириллице находятся дословно в `src/taksklad/`, кроме перечня отличий
  (`tests/test_station_text_parity.py`)
- кодов из блоклиста программы нет нигде во фронте и в корпусе, только SHA-256
- строки сравниваются по кодовым точкам, как в Python; `localeCompare` в логике станции запрещён
- `/` после этого плана открывает станцию, а старый операторский экран больше не монтируется (файлы остаются);
  окно станции здесь заглушка `StationApp`, поэтому **план 2 не выкатывается отдельно**, только вместе с планом 3
- `/` с сессией другой роли (администратор, логист) уходит на `/admin` и вход станции не вызывает:
  чужая cookie не заменяется (замечание финального ревью плана 1)
- ритм очереди как у программы: первый проход через 13 с после старта, дальше каждые 15 с
  (`src/taksklad/main.py:189`, `src/taksklad/app_data_loading.py:27`)
- push, PR, мерж только по отдельному разрешению Антона; выкатки в этом плане нет

## Review Focus

1. Сортировка заказов с кириллицей, буквой ё и символами вне BMP: JS сравнивает единицы UTF-16, Python кодовые точки;
   ожидание: порядок карточек как у программы (Task 2, случаи `text.sorted` и `order_list`)
2. Даты, которые склад реально видит: «Без даты», `'02.10.2026'` в кавычках, `02.10.2026 10:00`, `02.10.26`, пустая;
   ожидание: заголовки и сортировка как у программы, включая «БЕЗ» (Task 2 и Task 4)
3. Код КИЗ с GS: станция считает его одним кодом, позиция не «закрывается» раньше времени (Task 5 и Task 6)
4. Открытие `/` администратором с живой сессией: станция не вызывает свой вход и не затирает cookie,
   уходит на `/admin` (Task 9, тест ветки `redirect-admin`)
5. Шторм 401 во время досылки очереди: повторный вход станции не чаще раза в 30 с, коды не теряются (Task 10)

Весь код задач прогнан 02.10 в черновой копии `origin/main` `3d75e86` (фронт с тех пор не менялся, `81fcc66`
добавил только backend и шаблон nginx): тесты, typecheck и lint зелёные, числа в шагах сняты прогоном;
код задач 8-10 правит существующие файлы, поэтому дан патчем относительно `origin/main`

---
### Task 1: Оракул программы и сторож свежести корпуса

**Files:**
- Create: `tools/generate_station_parity_corpus.py`
- Create: `tests/test_station_parity_corpus.py`
- Generate: `frontend/src/station/__fixtures__/parity-corpus.json` (только запуском генератора)

**Interfaces:**
- Consumes: функции программы `src/taksklad` (через пакет `taksklad` в корне репозитория), ничего из других задач
- Produces: корпус с разделами `tables`, `blocklist`, `palette`, `text`, `products`, `scan`, `owners`,
  `duplicate_messages`, `order_list`, `rows`, `blocks`, `position`, `first_incomplete`; у каждого случая есть входы
  и ответ программы; все TS-тесты задач 2-7 читают только его через `src/station/__tests__/support.ts`

Генератор зовёт настоящие функции программы, включая `ScanningApp.on_scan`, `load_current_product`,
`update_party_summary_display` и `build_order_list_model`, с подставными виджетами, как уже делает
`tests/test_desktop_ui_contract.py`; сетевой ответ о доступности КИЗ подменяется данными случая;
«сегодня» зафиксировано на 02.10.2026 09:00 и 23:59:59

- [ ] **Step 1: Write the failing test**

Создать `tests/test_station_parity_corpus.py`:

```python
"""The station parity corpus is the single oracle of the browser station tests

`tools/generate_station_parity_corpus.py` records what the desktop program answers; the TypeScript tests under
`frontend/src/station/__tests__` read only that file. If the desktop answers differently now, the file is stale
"""

import io
import json
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from taksklad.kiz_blocklist import BLOCKED_KIZ_CODES
from tools.generate_station_parity_corpus import main

STATION = Path(__file__).resolve().parents[1] / "frontend" / "src" / "station"


class StationParityCorpusTests(unittest.TestCase):
    def test_corpus_file_is_what_the_desktop_answers_now(self):
        output = io.StringIO()
        with redirect_stdout(output):
            status = main(["--check"])
        self.assertEqual(status, 0, "regenerate: PYTHONPATH=. python tools/generate_station_parity_corpus.py")
        self.assertIn("STATION_PARITY_CORPUS_OK", output.getvalue())

    def test_no_raw_blocklist_code_anywhere_in_the_station_tree(self):
        # the browser bundle is public: only digests of the blocked codes may be there, in the corpus or in the code
        spellings = [(code, json.dumps(code)[1:-1]) for code in BLOCKED_KIZ_CODES]
        files = [path for path in STATION.rglob("*") if path.is_file()]
        self.assertIn("parity-corpus.json", {path.name for path in files})
        for path in files:
            data = path.read_bytes()
            for number, forms in enumerate(spellings, start=1):
                # the message names the file and the number of the code, never the code
                self.assertFalse(any(form.encode("utf-8") in data for form in forms), f"{path.name} holds blocklist code #{number}")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /tmp/station-plan2 && PYTHONPATH=. /Users/anton/Documents/work/TakSklad/.venv/bin/python -m unittest tests.test_station_parity_corpus -v`
Expected: ERROR `ModuleNotFoundError` на импорте генератора (модуля ещё нет), модуль новый

- [ ] **Step 3: Write the generator**

Создать `tools/generate_station_parity_corpus.py`:

```python
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
```

- [ ] **Step 4: Generate the corpus and run the test**

Run: `cd /tmp/station-plan2 && PYTHONPATH=. /Users/anton/Documents/work/TakSklad/.venv/bin/python tools/generate_station_parity_corpus.py`
Expected: одна строка `written frontend/src/station/__fixtures__/parity-corpus.json …` и файл около 6484 строк

Run: `cd /tmp/station-plan2 && PYTHONPATH=. /Users/anton/Documents/work/TakSklad/.venv/bin/python -m unittest tests.test_station_parity_corpus -v`
Expected: `Ran 2 tests` `OK` (снято прогоном в черновике 02.10)

- [ ] **Step 5: Commit**

```bash
cd /tmp/station-plan2
git add tools/generate_station_parity_corpus.py tests/test_station_parity_corpus.py frontend/src/station/__fixtures__/parity-corpus.json
ALLOW_NON_MAIN_BRANCH=1 git commit -m "test(station): оракул ответов программы склада и сторож свежести корпуса"
```

---

### Task 2: Текст, числа, даты и строки заказа

**Files:**
- Create: `frontend/src/station/logic/text.ts`
- Create: `frontend/src/station/logic/rows.ts`
- Modify: `frontend/src/api.ts` (`OrderItem`: два поля, патч ниже)
- Create: `frontend/src/station/__tests__/support.ts`
- Test: `frontend/src/station/__tests__/text.test.ts`, `frontend/src/station/__tests__/rows.test.ts`

**Interfaces:**
- Consumes: корпус из Task 1; `normalizeKizCode` из `features/warehouse/kizFormat.ts`,
  `blockQuantityForCode`, `scanTypeForCode` из `features/warehouse/scanQuantities.ts`
- Produces: из `text.ts` `normalizeText`, `parseIntValue`, `parseDateToStandard`, `normalizeLookupText`,
  `dateSortKey`, `formatOrderDateHeader(value, today)`, `formatMoney`, `compareText`, `compareKeys`;
  из `rows.ts` тип `StationRow`, `orderToRows(order)`, `groupKey(row)`, `scannedBlocks(row, codes)`;
  из `support.ts` `corpus`, `rowFromSpec`, `todayFrom` для всех тестов станции

Почему не обычные функции JS (снято прототипом, у каждого правила есть случай корпуса): строки сравниваются
по кодовым точкам; `parseIntValue` повторяет синтаксис `float()` Python (принимает `1_000`, `5.`, `1e3`,
отвергает `0x10`), бесконечность и NaN дают 0; `strptime` берёт день и месяц в одну или две цифры, `%y`
с разворотом на 68; пробельные символы как `str.isspace`

- [ ] **Step 1: Write the failing tests**

`frontend/src/station/__tests__/support.ts`:

```ts
/**
 * What every station test shares: the corpus, the two clocks and the conversion of a corpus position into a row
 * The corpus is the only source of expected values (tools/generate_station_parity_corpus.py), tests read nothing else
 */

import raw from "../__fixtures__/parity-corpus.json";
import type { StationRow } from "../logic/rows";

export const corpus = raw;
export const TODAY = new Date(corpus.today);
export const LATE = new Date(corpus.today_late);

/** One order position as the generator spells it out, every field present */
export type RowSpec = {
  date: string;
  payment: string;
  client: string;
  address: string;
  representative: string;
  request: string;
  request_id: string;
  order: string;
  item: string;
  product: string;
  plan: number;
  total: number;
  codes: string[];
  entries: { code: string; block_quantity: number }[];
};

export function rowFromSpec(spec: RowSpec): StationRow {
  return {
    orderDate: spec.date,
    paymentType: spec.payment,
    client: spec.client,
    address: spec.address,
    representative: spec.representative,
    product: spec.product,
    planBlocks: spec.plan,
    lineTotal: spec.total,
    requestNumber: spec.request,
    requestId: spec.request_id,
    backendOrderId: spec.order,
    backendOrderItemId: spec.item,
    existingCodes: spec.codes,
    existingEntries: spec.entries,
  };
}

/** `it.each` rows from corpus items; an empty list would run no test and pass, so it is refused */
export function labelled<T>(items: readonly T[], label: (item: T) => string): [string, T][] {
  if (items.length === 0) throw new Error("the corpus section is empty");
  return items.map((item) => [label(item), item]);
}

/** JSON has no NaN or Infinity: the generator sends them as {"float": "NaN"} */
export function decodeFloat(value: unknown): unknown {
  return value !== null && typeof value === "object" ? Number((value as { float: string }).float) : value;
}
```

`frontend/src/station/__tests__/text.test.ts`:

```ts
/**
 * Text, date and number helpers against what the desktop answered (corpus.text)
 * Cases are where JavaScript would answer differently from Python: float syntax, strptime, whitespace, string order
 */

import { describe, expect, it } from "vitest";

import {
  compareText,
  dateSortKey,
  formatMoney,
  formatOrderDateHeader,
  normalizeLookupText,
  normalizeText,
  parseDateToStandard,
  parseIntValue,
} from "../logic/text";
import { corpus, decodeFloat, labelled, LATE, TODAY } from "./support";

const { dates, ints, money, strip, lookup, sorted } = corpus.text;

describe("dates", () => {
  it.each(labelled(dates, (item) => JSON.stringify(item.value)))("%s", (_name, item) => {
    expect(parseDateToStandard(item.value)).toBe(item.standard);
    expect(formatOrderDateHeader(item.value, TODAY)).toBe(item.header);
    // the day is named by the calendar, not by the hour
    expect(formatOrderDateHeader(item.value, LATE)).toBe(item.header_late);
    expect(dateSortKey(item.value)).toBe(item.sort_day ?? Infinity);
  });
});

describe("numbers", () => {
  // toBe compares with Object.is, so a negative zero where Python has 0 fails here too
  it.each(labelled(ints, (item) => JSON.stringify(item.value)))("parseIntValue(%s)", (_name, item) => {
    expect(parseIntValue(decodeFloat(item.value))).toBe(item.result);
  });

  it.each(labelled(money, (item) => JSON.stringify(item.value)))("formatMoney(%s)", (_name, item) => {
    expect(formatMoney(item.value)).toBe(item.text);
  });
});

describe("whitespace", () => {
  // Python str.strip drops \x1c-\x1f and \x85 but keeps the byte order mark, the other way round from JS trim
  it.each(labelled(strip, (item) => JSON.stringify(item.value)))("normalizeText(%s)", (_name, item) => {
    expect(normalizeText(item.value)).toBe(item.result);
  });

  it.each(labelled(lookup, (item) => JSON.stringify(item.value)))("normalizeLookupText(%s)", (_name, item) => {
    expect(normalizeLookupText(item.value)).toBe(item.result);
  });
});

describe("ordering", () => {
  it("sorts by code point like Python, which the default JS sort does not", () => {
    expect([...sorted.values].sort(compareText)).toEqual(sorted.result);
    expect([...sorted.values].sort()).not.toEqual(sorted.result);
  });
});
```

`frontend/src/station/__tests__/rows.test.ts`:

```ts
/** Rows built from API orders and block counting against the desktop (corpus.rows, corpus.blocks) */

import { describe, expect, it } from "vitest";

import type { Order } from "../../api";
import { orderToRows, scannedBlocks } from "../logic/rows";
import { corpus, labelled, rowFromSpec, type RowSpec } from "./support";

describe("orderToRows", () => {
  it.each(labelled(corpus.rows, (item) => item.name))("%s", (_name, { input, output }) => {
    expect(orderToRows(input as unknown as Order)).toEqual((output as RowSpec[]).map(rowFromSpec));
  });

  it("does not share the code list with the order", () => {
    const order = structuredClone(corpus.rows[0].input) as unknown as Order;
    const [first] = orderToRows(order);
    first.existingCodes.push("x");
    expect(order.items[0].scan_codes).toEqual(corpus.rows[0].input.items[0].scan_codes);
  });
});

describe("scannedBlocks", () => {
  it.each(labelled(corpus.blocks, (item) => item.name))("%s", (_name, { input, output }) => {
    expect(scannedBlocks(rowFromSpec(input.row as RowSpec), input.codes)).toBe(output);
  });
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /tmp/station-plan2/frontend && npx vitest run src/station/__tests__/text.test.ts src/station/__tests__/rows.test.ts`
Expected: оба файла FAIL на разрешении импорта `../logic/text` и `../logic/rows` (модули новые)

- [ ] **Step 3: Write the implementation**

`frontend/src/api.ts`, тип `OrderItem` получает поля, которые backend уже отдаёт (`OrderItemRead` в
`backend/app/schemas.py:144-155`):

```diff
diff --git a/frontend/src/api.ts b/frontend/src/api.ts
index 5f7d2a6..fd57c46 100644
--- a/frontend/src/api.ts
+++ b/frontend/src/api.ts
@@ -5,6 +5,8 @@ export type OrderItem = {
   quantity_blocks: number;
   scanned_blocks: number;
   requires_kiz: boolean;
+  block_price?: number;
+  line_total?: number;
   status: string;
   scan_codes: string[];
   scan_entries?: Array<{
```

`frontend/src/station/logic/text.ts`:

```ts
/**
 * Text, number and date helpers that answer exactly like the desktop program
 * (src/taksklad/utils.py, desktop_scan_rules.py). Python and JavaScript differ in
 * whitespace, float syntax, strptime and string ordering, so each helper below
 * replaces the obvious JS one-liner with the behaviour the desktop has
 */

// Python str.strip/split/\s use str.isspace: it has \x1c-\x1f and \x85 but not \ufeff, JS \s is the opposite
const PY_SPACE = "[\\t\\n\\v\\f\\r\\x1c-\\x1f \\x85\\xa0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000]";
const SPACE_RUN = new RegExp(`${PY_SPACE}+`, "g");
const SPACE_EDGES = new RegExp(`^${PY_SPACE}+|${PY_SPACE}+$`, "g");

export function strip(text: string): string {
  return text.replace(SPACE_EDGES, "");
}

/** Python str.split() without arguments: runs of whitespace, no empty parts */
export function splitWords(text: string): string[] {
  return text.split(SPACE_RUN).filter(Boolean);
}

/** str(value or "").strip(): falsy values, including 0, become "" */
export function normalizeText(value: unknown): string {
  return value ? strip(String(value)) : "";
}

// Python float() syntax: digits may be separated by single underscores
const DIGITS = "\\d+(?:_\\d+)*";
const PY_FLOAT = new RegExp(`^[+-]?(?:${DIGITS}(?:\\.(?:${DIGITS})?)?|\\.${DIGITS})(?:[eE][+-]?${DIGITS})?$`);

/** int(float(text)) with spaces dropped and comma as the decimal mark; anything unparseable or infinite is 0 */
export function parseIntValue(value: unknown): number {
  const text =
    typeof value === "number" ? String(value) : normalizeText(value).replaceAll(" ", "").replaceAll(",", ".");
  if (!PY_FLOAT.test(text)) return 0;
  const number = Number(text.replaceAll("_", ""));
  // "|| 0" turns -0 into 0, Python has no negative zero for ints
  return Number.isFinite(number) ? Math.trunc(number) || 0 : 0;
}

export function normalizeLookupText(value: unknown): string {
  const text = normalizeText(value).toLowerCase().replaceAll("ё", "е").replaceAll("\ufeff", "").replace(/[*:]+/g, "");
  return strip(text.replace(SPACE_RUN, " "));
}

/** Python str.casefold(), enough for the ASCII words the product rules look for (ß becomes ss) */
export function casefold(text: string): string {
  return text.toUpperCase().toLowerCase();
}

/** Python compares strings by code point, JS by UTF-16 unit: they differ above U+FFFF, never use localeCompare */
export function compareText(a: string, b: string): number {
  const left = Array.from(a, (char) => char.codePointAt(0) ?? 0);
  const right = Array.from(b, (char) => char.codePointAt(0) ?? 0);
  const shared = Math.min(left.length, right.length);
  for (let i = 0; i < shared; i += 1) {
    if (left[i] !== right[i]) return left[i] - right[i];
  }
  return left.length - right.length;
}

/** Python tuple ordering over numbers and strings; numbers are compared, never subtracted, because Infinity - Infinity is NaN */
export function compareKeys(a: readonly (number | string)[], b: readonly (number | string)[]): number {
  for (let i = 0; i < a.length; i += 1) {
    const x = a[i];
    const y = b[i];
    if (typeof x === "number" && typeof y === "number") {
      if (x !== y) return x < y ? -1 : 1;
    } else {
      const order = compareText(String(x), String(y));
      if (order !== 0) return order;
    }
  }
  return 0;
}

// strptime directives with the exact ranges Python accepts: %d and %m take one or two digits, %Y four, %y two
const DIRECTIVES: Record<string, string> = {
  d: "(?<d>3[01]|[12]\\d|0[1-9]|[1-9])",
  m: "(?<m>1[0-2]|0[1-9]|[1-9])",
  Y: "(?<Y>\\d{4})",
  y: "(?<y>\\d\\d)",
};

function dateFormat(format: string): RegExp {
  const body = format.replaceAll(".", "\\.").replace(/%([dmYy])/g, (_directive, key: string) => DIRECTIVES[key]);
  return new RegExp(`^${body}$`);
}

const DAY_FIRST = dateFormat("%d.%m.%Y");
const ISO = dateFormat("%Y-%m-%d");
const SORT_FORMATS = [DAY_FIRST, ISO];
// the order is part of the answer: "05/10/2026" is the 5th of October because %d/%m/%Y comes before %m/%d/%Y
const STANDARD_FORMATS = [
  DAY_FIRST,
  ISO,
  dateFormat("%d/%m/%Y"),
  dateFormat("%m/%d/%Y"),
  dateFormat("%d.%m.%y"),
  dateFormat("%Y.%m.%d"),
];

const MS_PER_DAY = 86_400_000;

/** Days since 1970-01-01 of a real calendar date, null for 30 February or year 0 (Python raises ValueError) */
function dayNumber(year: number, month: number, day: number): number | null {
  const date = new Date(0);
  date.setUTCFullYear(year, month - 1, day); // unlike Date.UTC, years 0-99 stay as given
  const real = year >= 1 && date.getUTCMonth() === month - 1 && date.getUTCDate() === day;
  return real ? date.getTime() / MS_PER_DAY : null;
}

type ParsedDate = { year: number; month: number; day: number; days: number };

function parseDate(text: string, formats: readonly RegExp[]): ParsedDate | null {
  for (const format of formats) {
    const groups = format.exec(text)?.groups;
    if (!groups) continue;
    const month = Number(groups.m);
    const day = Number(groups.d);
    // %y pivots like POSIX: 69-99 is 19xx, 00-68 is 20xx
    const year = groups.Y !== undefined ? Number(groups.Y) : Number(groups.y) + (Number(groups.y) <= 68 ? 2000 : 1900);
    const days = dayNumber(year, month, day);
    if (days !== null) return { year, month, day, days };
  }
  return null;
}

function formatDate({ year, month, day }: ParsedDate): string {
  return [String(day).padStart(2, "0"), String(month).padStart(2, "0"), String(year).padStart(4, "0")].join(".");
}

/** str(value) stripped, wrapping quotes removed, cut at the first space; null stays null */
export function cleanDateValue(value: unknown): string | null {
  if (value === null || value === undefined) return null;
  const text = strip(String(value)).replace(/^['"]+|['"]+$/g, "");
  return text.includes(" ") ? (splitWords(text)[0] ?? "") : text;
}

/** DD.MM.YYYY for any of the six accepted formats; an unrecognised text comes back cleaned but unchanged */
export function parseDateToStandard(value: unknown): string | null {
  const cleaned = cleanDateValue(value);
  if (!cleaned) return null;
  const parsed = parseDate(cleaned, STANDARD_FORMATS);
  return parsed ? formatDate(parsed) : cleaned;
}

/** Sort key of a shipment date: day number, +Infinity when it is neither DD.MM.YYYY nor YYYY-MM-DD */
export function dateSortKey(value: unknown): number {
  return parseDate(normalizeText(value), SORT_FORMATS)?.days ?? Infinity;
}

const DAY_PREFIXES = new Map([
  [0, "Сегодня"],
  [1, "Завтра"],
  [-1, "Вчера"],
]);

export function formatOrderDateHeader(value: unknown, today: Date): string {
  const text = parseDateToStandard(value) || normalizeText(value) || "Без даты отгрузки";
  const parsed = parseDate(text, [DAY_FIRST]);
  if (!parsed) return text;
  const todayDays = dayNumber(today.getFullYear(), today.getMonth() + 1, today.getDate());
  const prefix = DAY_PREFIXES.get(parsed.days - (todayDays ?? Infinity));
  return prefix ? `${prefix}, ${formatDate(parsed)}` : formatDate(parsed);
}

/** 1234567 -> "1 234 567" (Python f"{n:,}" with the comma swapped for a plain space) */
export function groupThousands(amount: number): string {
  return String(amount).replace(/\B(?=(\d{3})+(?!\d))/g, " ");
}

export function formatMoney(value: unknown): string {
  const amount = parseIntValue(value);
  return amount <= 0 ? "сумма не указана" : `${groupThousands(amount)} сум`;
}
```

`frontend/src/station/logic/rows.ts`:

```ts
/**
 * One order position as the desktop program sees it, and the conversion from the API order
 * (src/taksklad/backend_client.py backend_order_to_rows, orders.py order_group_key)
 */

import type { Order } from "../../api";
import { blockQuantityForCode } from "../../features/warehouse/scanQuantities";
import { normalizeText, parseDateToStandard, parseIntValue } from "./text";

export type ScanEntry = { code: string; block_quantity: number };

export type StationRow = {
  orderDate: string;
  paymentType: string;
  client: string;
  address: string;
  representative: string;
  product: string;
  planBlocks: number;
  lineTotal: number;
  requestNumber: string;
  requestId: string;
  backendOrderId: string;
  backendOrderItemId: string;
  existingCodes: string[];
  existingEntries: ScanEntry[];
};

export type GroupKey = readonly [requestNumber: string, client: string, paymentType: string, address: string];

export function orderToRows(order: Order): StationRow[] {
  return order.items.map((item) => {
    // an item without recorded entries gets them from its codes: a box code counts for 50 blocks
    const entries = item.scan_entries?.length
      ? item.scan_entries
      : item.scan_codes.map((code) => ({ code: normalizeText(code), block_quantity: blockQuantityForCode(code) }));
    return {
      orderDate: parseDateToStandard(order.order_date) ?? "",
      paymentType: order.payment_type || "",
      client: order.client || "",
      address: order.address || "",
      representative: order.representative || "",
      product: item.product || "",
      planBlocks: parseIntValue(item.quantity_blocks),
      lineTotal: parseIntValue(item.line_total),
      requestNumber: order.skladbot_request_number || "",
      requestId: order.skladbot_request_id || "",
      backendOrderId: order.id || "",
      backendOrderItemId: item.id || "",
      existingCodes: [...item.scan_codes],
      existingEntries: entries.map(({ code, block_quantity }) => ({ code, block_quantity })),
    };
  });
}

/** Positions of one request, client, payment type and address form one card */
export function groupKey(row: StationRow): GroupKey {
  return [
    normalizeText(row.requestNumber),
    normalizeText(row.client) || "Клиент не указан",
    normalizeText(row.paymentType) || "Оплата не указана",
    normalizeText(row.address) || "Адрес не указан",
  ];
}

/** Blocks the given codes add up to: the recorded quantity of a code, else 50 for a box and 1 for a unit */
export function scannedBlocks(row: StationRow, codes: readonly string[]): number {
  const recorded = new Map(
    row.existingEntries.filter((entry) => normalizeText(entry.code)).map((entry) => [normalizeText(entry.code), entry]),
  );
  return codes.reduce((sum, code) => {
    const quantity = Math.trunc(recorded.get(normalizeText(code))?.block_quantity ?? 0);
    return sum + (quantity > 0 ? quantity : blockQuantityForCode(code));
  }, 0);
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /tmp/station-plan2/frontend && npx vitest run src/station/__tests__/text.test.ts src/station/__tests__/rows.test.ts && npm run typecheck`
Expected: `Tests  108 passed`, typecheck без ошибок

- [ ] **Step 5: Commit**

```bash
cd /tmp/station-plan2
git add frontend/src/api.ts frontend/src/station/logic/text.ts frontend/src/station/logic/rows.ts \
  frontend/src/station/__tests__/support.ts frontend/src/station/__tests__/text.test.ts frontend/src/station/__tests__/rows.test.ts
ALLOW_NON_MAIN_BRANCH=1 git commit -m "feat(station): текст, числа, даты и строки заказа как у программы склада"
```

---

### Task 3: Товар по названию и коду, блоклист хешами

**Files:**
- Create: `frontend/src/station/logic/productKeys.ts`
- Create: `frontend/src/station/logic/blocklist.ts`
- Test: `frontend/src/station/__tests__/productKeys.test.ts`, `frontend/src/station/__tests__/blocklist.test.ts`

**Interfaces:**
- Consumes: `text.ts` (Task 2), `aggregateBoxProductKey` и соседи из `features/warehouse/scanQuantities.ts`
- Produces: `UNIT_PRODUCT_PREFIXES`, `productKeyFromName`, `scanCodeProductKey`, `scanProductMismatch`,
  `aggregateProductMismatch`, `PRODUCT_KEY_LABELS`, `formatProductKeyLabel`, `scanSkuGuardStatus(row | null)`;
  `blockedKizReason(code, digests?)` асинхронно через `crypto.subtle`, `sha256Hex`

- [ ] **Step 1: Write the failing tests**

`frontend/src/station/__tests__/productKeys.test.ts`:

```ts
/** Product keys, SKU guard, labels and mismatch rules against the desktop (corpus.tables and corpus.products) */

import { describe, expect, it } from "vitest";

import {
  AGGREGATE_BOX_BLOCK_QUANTITY,
  AGGREGATE_BOX_PRODUCT_PREFIXES,
  blockQuantityForCode,
  scanTypeForCode,
} from "../../features/warehouse/scanQuantities";
import {
  aggregateProductMismatch,
  formatProductKeyLabel,
  PRODUCT_KEY_LABELS,
  productKeyFromName,
  scanCodeProductKey,
  scanProductMismatch,
  scanSkuGuardStatus,
  UNIT_PRODUCT_PREFIXES,
} from "../logic/productKeys";
import { corpus, labelled } from "./support";

const { tables, products } = corpus;

describe("tables", () => {
  it("keep the unit prefixes of the desktop", () => expect(UNIT_PRODUCT_PREFIXES).toEqual(tables.unit_prefixes));
  it("keep the box prefixes of the desktop", () => expect(AGGREGATE_BOX_PRODUCT_PREFIXES).toEqual(tables.aggregate_prefixes));
  it("keep the block quantity of a box", () => expect(AGGREGATE_BOX_BLOCK_QUANTITY).toBe(tables.aggregate_block_quantity));
  it("keep the SKU labels of the desktop", () => expect(PRODUCT_KEY_LABELS).toEqual(tables.product_key_labels));
});

describe("product names", () => {
  it.each(labelled(products.names, (item) => JSON.stringify(item.product)))("%s", (_name, item) => {
    const { product_key: productKey, ...guard } = item.guard as { state: string; message: string; product_key?: string };
    expect(productKeyFromName(item.product)).toBe(item.key);
    expect(scanSkuGuardStatus({ product: item.product })).toEqual({ ...guard, productKey });
  });

  it("has no guard without a position", () => expect(scanSkuGuardStatus(null)).toEqual(products.guard_none));
});

describe("codes", () => {
  it.each(labelled(products.code_keys, (item) => JSON.stringify(item.code)))("%s", (_name, item) => {
    expect(scanCodeProductKey(item.code)).toBe(item.key);
    expect(scanTypeForCode(item.code)).toBe(item.scan_type);
    expect(blockQuantityForCode(item.code)).toBe(item.blocks);
  });

  it.each(labelled(products.mismatch, (item) => JSON.stringify([item.code, item.product])))("mismatch %s", (_name, item) => {
    expect(scanProductMismatch(item.code, item.product)).toBe(item.mismatch);
    expect(aggregateProductMismatch(item.code, item.product)).toBe(item.box_mismatch);
  });
});

describe("labels", () => {
  // "constructor" must come back as itself, not as a member of Object.prototype
  it.each(labelled(products.labels, (item) => JSON.stringify(item.key)))("%s", (_name, item) => {
    expect(formatProductKeyLabel(item.key)).toBe(item.label);
  });
});
```

`frontend/src/station/__tests__/blocklist.test.ts`:

```ts
/** The blocklist keeps only digests; they must be those of the desktop list (corpus.blocklist), the codes are not in the corpus */

import { describe, expect, it } from "vitest";

import { BLOCK_REASON, BLOCKED_KIZ_SHA256, blockedKizReason, sha256Hex } from "../logic/blocklist";
import { corpus } from "./support";

const { reason, sha256, sample } = corpus.blocklist;

describe("blocklist", () => {
  it("holds exactly the digests of the desktop list", () => expect([...BLOCKED_KIZ_SHA256].sort()).toEqual(sha256));

  it("keeps the desktop refusal text", () => expect(BLOCK_REASON).toBe(reason));

  it("hashes like hashlib", async () => expect(await sha256Hex(sample.text)).toBe(sample.sha256));

  it("refuses a code whose digest is listed, trimmed or not, and only that code", async () => {
    const listed = new Set([sample.sha256]);
    expect(await blockedKizReason(sample.text, listed)).toBe(reason);
    expect(await blockedKizReason(` ${sample.text}\r\n`, listed)).toBe(reason);
    expect(await blockedKizReason(`${sample.text}X`, listed)).toBe("");
    expect(await blockedKizReason(" \t\r\n", listed)).toBe("");
  });

  it("lets an ordinary code through", async () => expect(await blockedKizReason(sample.text)).toBe(""));
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /tmp/station-plan2/frontend && npx vitest run src/station/__tests__/productKeys.test.ts src/station/__tests__/blocklist.test.ts`
Expected: FAIL на импорте `../logic/productKeys` и `../logic/blocklist` (модули новые)

- [ ] **Step 3: Write the implementation**

`frontend/src/station/logic/productKeys.ts`:

```ts
/**
 * Which SKU a KIZ code or a product name stands for, and the SKU guard built on it
 * (src/taksklad/scan_quantities.py, desktop_scan_rules.py). The box prefixes and the
 * block quantity already live in features/warehouse/scanQuantities.ts and are reused
 */

import { aggregateBoxProductKey, scanTypeForCode, SCAN_TYPE_AGGREGATE_BOX } from "../../features/warehouse/scanQuantities";
import { casefold, normalizeText, splitWords } from "./text";

export const UNIT_PRODUCT_PREFIXES: Readonly<Record<string, string>> = {
  "0104006396054005": "gold:ssl",
  "0104006396053978": "brown:op",
  "0104006396053947": "red:op",
  "0104006396054067": "brown:ssl",
  "0104006396054036": "red:ssl",
  "0104006396104441": "green:op",
  "0104006396104199": "brown:kssl",
  "0104006396104229": "green:kssl",
};

const PRODUCT_COLORS = ["brown", "red", "gold", "green"];
const PRODUCT_FORMATS = ["op", "ssl", "kssl"];

/** "Chapman RED OP 20" -> "red:op": a color and a format as words, or glued together like "brownop" */
export function productKeyFromName(product: unknown): string {
  const tokens = splitWords(casefold(normalizeText(product)).replace(/[`"']/g, " "));
  const compact = tokens.join("");
  const color = PRODUCT_COLORS.find((item) => tokens.includes(item) || compact.includes(item)) ?? "";
  const format = PRODUCT_FORMATS.find((item) => tokens.includes(item) || (color && compact.includes(color + item))) ?? "";
  return color && format ? `${color}:${format}` : "";
}

function unitProductKey(code: string): string {
  const text = normalizeText(code);
  const prefix = Object.keys(UNIT_PRODUCT_PREFIXES).find((item) => text.startsWith(item));
  return prefix ? UNIT_PRODUCT_PREFIXES[prefix] : "";
}

export function scanCodeProductKey(code: string): string {
  return aggregateBoxProductKey(code) || unitProductKey(code);
}

/** A named product that the code's SKU does not match; a product the rules cannot name never mismatches */
export function scanProductMismatch(code: string, product: unknown): boolean {
  const productKey = productKeyFromName(product);
  if (!productKey) return false;
  const codeKey = scanCodeProductKey(code);
  return !codeKey || productKey !== codeKey;
}

/** Only for box codes: a box of one SKU must not close a position of another, or of an unnamed one */
export function aggregateProductMismatch(code: string, product: unknown): boolean {
  if (scanTypeForCode(code) !== SCAN_TYPE_AGGREGATE_BOX) return false;
  const productKey = productKeyFromName(product);
  return !productKey || productKey !== aggregateBoxProductKey(code);
}

export const PRODUCT_KEY_LABELS: Readonly<Record<string, string>> = {
  "brown:op": "Brown OP",
  "red:op": "RED OP",
  "gold:ssl": "Gold SSL",
  "brown:ssl": "Brown SSL",
  "red:ssl": "RED SSL",
  "green:op": "Green OP",
  "brown:kssl": "Brown KSSL",
  "green:kssl": "Green KSSL",
};

export function formatProductKeyLabel(productKey: unknown): string {
  const key = normalizeText(productKey);
  if (!key) return "не распознан";
  return Object.hasOwn(PRODUCT_KEY_LABELS, key) ? PRODUCT_KEY_LABELS[key] : key;
}

export type SkuGuardStatus = {
  state: "unavailable" | "unknown" | "active";
  message: string;
  productKey?: string;
};

export function scanSkuGuardStatus(row: { product: string } | null): SkuGuardStatus {
  if (!row) return { state: "unavailable", message: "SKU-защита недоступна: выберите позицию." };
  const product = normalizeText(row.product);
  if (!product) return { state: "unknown", message: "SKU-защита не активна: товар не указан." };
  const productKey = productKeyFromName(product);
  if (!productKey) return { state: "unknown", message: `SKU-защита не активна: товар не распознан (${product}).` };
  return { state: "active", message: `SKU-защита активна: ${formatProductKeyLabel(productKey)}.`, productKey };
}
```

`frontend/src/station/logic/blocklist.ts` (в файле только SHA-256, тест проверяет, что сырых кодов нет):

```ts
/**
 * Codes the warehouse must never ship (src/taksklad/kiz_blocklist.py)
 * Only SHA-256 digests are stored: the browser bundle is public, so the codes themselves must not sit in it
 */

import { normalizeKizCode } from "../../features/warehouse/kizFormat";

export const BLOCK_REASON = "Код маркировки заблокирован. Отгрузка запрещена";

// sha256 of the code with spaces, tabs and line breaks trimmed (the desktop normalization), hex
export const BLOCKED_KIZ_SHA256: ReadonlySet<string> = new Set([
  "cd7dc6aea0b6045ec64ed14b515fab810d50343f97baa36bf4f5d32d5ccb7c0d",
  "ffcdb881f32655b466c89451acdccc09e8c001fa100fd9d37bf557024c1d755c",
]);

export async function sha256Hex(text: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
}

/** The refusal text for a blocked code, "" for any other; the digests are a parameter so a test can use its own */
export async function blockedKizReason(code: string, blocked: ReadonlySet<string> = BLOCKED_KIZ_SHA256): Promise<string> {
  const normalized = normalizeKizCode(code);
  if (!normalized) return "";
  return blocked.has(await sha256Hex(normalized)) ? BLOCK_REASON : "";
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /tmp/station-plan2/frontend && npx vitest run src/station/__tests__/productKeys.test.ts src/station/__tests__/blocklist.test.ts`
Expected: `Tests  81 passed`

- [ ] **Step 5: Commit**

```bash
cd /tmp/station-plan2
git add frontend/src/station/logic/productKeys.ts frontend/src/station/logic/blocklist.ts \
  frontend/src/station/__tests__/productKeys.test.ts frontend/src/station/__tests__/blocklist.test.ts
ALLOW_NON_MAIN_BRANCH=1 git commit -m "feat(station): товар по названию и коду, SKU-защита, блоклист хешами"
```

---

### Task 4: Список заказов

**Files:**
- Create: `frontend/src/station/logic/orderList.ts`
- Test: `frontend/src/station/__tests__/orderList.test.ts`

**Interfaces:**
- Consumes: `rows.ts`, `text.ts`
- Produces: `buildOrderListModel(rows, search, today)` → `{ rows: Array<{kind:"date", title} | {kind:"order", client, meta, summary, groupKey}>, totalGroups, visibleCards, subtitle, counter }`

- [ ] **Step 1: Write the failing test**

```ts
/** The order list against build_order_list_model of the desktop (corpus.order_list): grouping, search, order of dates and cards */

import { describe, expect, it } from "vitest";

import { buildOrderListModel } from "../logic/orderList";
import { corpus, labelled, rowFromSpec, TODAY } from "./support";

describe("order list", () => {
  it.each(labelled(corpus.order_list, (item) => item.name))("%s", (_name, { input, output }) => {
    const model = buildOrderListModel(input.rows.map(rowFromSpec), input.search, TODAY);
    expect(model.rows).toEqual(
      output.rows.map((item) =>
        item.kind === "date"
          ? { kind: "date", title: item.title }
          : { kind: "order", client: item.client, meta: item.meta, summary: item.summary, groupKey: item.group_key },
      ),
    );
    expect(model.subtitle).toBe(output.subtitle);
    expect(model.counter).toBe(output.counter);
    // the two counts behind the texts: cards shown, and "из N" at the end of the counter
    expect(model.visibleCards).toBe(output.rows.filter((item) => item.kind === "order").length);
    expect(`Показаны ${model.visibleCards} из ${model.totalGroups}`).toBe(output.counter);
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /tmp/station-plan2/frontend && npx vitest run src/station/__tests__/orderList.test.ts`
Expected: FAIL на импорте `../logic/orderList`

- [ ] **Step 3: Write the implementation**

```ts
/**
 * The list of order cards, grouped by shipment date (src/taksklad/order_list_models.py)
 * Groups keep first-seen order, then sort stably: dates by day, cards by request number
 */

import { groupKey, type GroupKey, type StationRow } from "./rows";
import {
  compareKeys,
  dateSortKey,
  formatOrderDateHeader,
  normalizeLookupText,
  normalizeText,
  parseDateToStandard,
  parseIntValue,
} from "./text";

export type OrderListRow =
  | { kind: "date"; title: string }
  | { kind: "order"; client: string; meta: string; summary: string; groupKey: GroupKey };

export type OrderListModel = {
  rows: OrderListRow[];
  totalGroups: number;
  visibleCards: number;
  subtitle: string;
  counter: string;
};

type Group = { key: GroupKey; rows: StationRow[]; date: string };

/** "WH-R-12" -> 12: the digits at the end of the request number, 0 when there are none (reports.py) */
function requestNumberValue(requestNumber: string): number {
  const digits = /(\d+)$/.exec(normalizeText(requestNumber));
  return digits ? parseIntValue(digits[1]) : 0;
}

/** Cards with a request number first, by its number, then by the lookup form of all four parts (reports.py) */
function cardSortKey([request, client, payment, address]: GroupKey): (number | string)[] {
  return [
    request ? 0 : 1,
    requestNumberValue(request),
    normalizeLookupText(request),
    normalizeLookupText(client),
    normalizeLookupText(payment),
    normalizeLookupText(address),
  ];
}

function searchArea(row: StationRow, [request, client, payment, address]: GroupKey): string {
  const parts = [request || "Без номера SkladBot", client, payment, address, normalizeText(row.representative), normalizeText(row.product)];
  return parts.join(" ").toLowerCase();
}

export function buildOrderListModel(rows: readonly StationRow[], search: string, today: Date): OrderListModel {
  const query = normalizeText(search).toLowerCase();
  const groups = new Map<string, Group>();
  const visible = new Set<Group>();

  for (const row of rows) {
    const key = groupKey(row);
    const id = JSON.stringify(key);
    // a card takes the date of its first position
    const group = groups.get(id) ?? { key, rows: [], date: parseDateToStandard(row.orderDate) || "Без даты" };
    groups.set(id, group);
    group.rows.push(row);
    if (!query || searchArea(row, key).includes(query)) visible.add(group);
  }

  const byDate = new Map<string, Group[]>();
  for (const group of visible) byDate.set(group.date, [...(byDate.get(group.date) ?? []), group]);

  const listRows: OrderListRow[] = [];
  const dates = [...byDate.keys()].sort((a, b) => compareKeys([dateSortKey(a)], [dateSortKey(b)]));
  for (const date of dates) {
    listRows.push({ kind: "date", title: formatOrderDateHeader(date, today).toUpperCase() });
    const cards = (byDate.get(date) ?? []).sort((a, b) => compareKeys(cardSortKey(a.key), cardSortKey(b.key)));
    for (const { key, rows: positions } of cards) {
      const blocks = positions.reduce((sum, position) => sum + position.planBlocks, 0);
      listRows.push({
        kind: "order",
        client: key[1],
        meta: `${key[0] || "Без номера SkladBot"} · ${formatOrderDateHeader(date, today)}`,
        summary: `${positions.length} SKU · ${blocks} блоков`,
        groupKey: key,
      });
    }
  }

  return {
    rows: listRows,
    totalGroups: groups.size,
    visibleCards: visible.size,
    subtitle: `${groups.size} активных заказов · список листается вниз`,
    counter: `Показаны ${visible.size} из ${groups.size}`,
  };
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /tmp/station-plan2/frontend && npx vitest run src/station/__tests__/orderList.test.ts`
Expected: `Tests  16 passed`

- [ ] **Step 5: Commit**

```bash
cd /tmp/station-plan2
git add frontend/src/station/logic/orderList.ts frontend/src/station/__tests__/orderList.test.ts
ALLOW_NON_MAIN_BRANCH=1 git commit -m "feat(station): модель списка заказов с датами, поиском и порядком программы"
```

---

### Task 5: Текущая позиция и сводка партии

**Files:**
- Create: `frontend/src/station/logic/position.ts`
- Test: `frontend/src/station/__tests__/position.test.ts`

**Interfaces:**
- Consumes: `rows.ts`, `text.ts`
- Produces: `firstIncompleteIndex(rows)`, `positionView(rows, index) → PositionView | null`, `PIECES_PER_BLOCK = 10`,
  `partySummaryText(rows, today)`; согласованное отличие: код с GS считается одним

- [ ] **Step 1: Write the failing test**

```ts
/**
 * The position screen, the party summary and the first position to scan against the desktop
 * (corpus.position, corpus.first_incomplete)
 */

import { describe, expect, it } from "vitest";

import { firstIncompleteIndex, partySummaryText, positionView } from "../logic/position";
import { corpus, labelled, rowFromSpec, TODAY } from "./support";

describe("position screen and party summary", () => {
  it.each(labelled(corpus.position, (item) => item.name))("%s", (_name, { input, output }) => {
    const rows = input.rows.map(rowFromSpec);
    const expected = {
      info: output.info,
      client: output.client,
      product: output.product,
      position: output.position,
      progress: output.progress,
      lastCode: output.last_code,
      nextState: output.next_state,
      finishState: output.finish_state,
    };
    const view = positionView(rows, input.index);
    if (view === null) {
      // past the last position the desktop leaves the screen as it was, here untouched labels
      expect(Object.values(expected).every((text) => text === "")).toBe(true);
    } else {
      expect(view).toEqual(expected);
    }
    expect(partySummaryText(rows, TODAY)).toBe(output.party);
  });
});

// approved deviation (GS ruling): the desktop counts a code with GS inside twice and calls the position done, the station does not
const GS_DEVIATIONS = new Map([["gs_code_counts_once", 0]]);

describe("first incomplete position", () => {
  it.each(labelled(corpus.first_incomplete, (item) => item.name))("%s", (name, { input, output }) => {
    const station = GS_DEVIATIONS.get(name);
    // the corpus keeps the desktop answer, so a deviation that the desktop later fixes shows up here
    if (station !== undefined) expect(output.index).not.toBe(station);
    expect(firstIncompleteIndex(input.rows.map(rowFromSpec))).toBe(station ?? output.index);
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /tmp/station-plan2/frontend && npx vitest run src/station/__tests__/position.test.ts`
Expected: FAIL на импорте `../logic/position`

- [ ] **Step 3: Write the implementation**

```ts
/**
 * What the operator sees for the current position and for the whole party
 * (src/taksklad/app_order_display.py load_current_product and update_party_summary_display,
 * desktop_scan_rules.py first_incomplete_order_index)
 */

import { scannedBlocks, type StationRow } from "./rows";
import {
  compareKeys,
  compareText,
  dateSortKey,
  formatMoney,
  formatOrderDateHeader,
  groupThousands,
  normalizeText,
  parseDateToStandard,
} from "./text";

// the desktop default catalog (config.py DEFAULT_PIECES_PER_BLOCK): the browser has no catalog of its own
export const PIECES_PER_BLOCK = 10;

export type ButtonState = "normal" | "disabled";

export type PositionView = {
  info: string;
  client: string;
  product: string;
  position: string;
  progress: string;
  lastCode: string;
  nextState: ButtonState;
  finishState: ButtonState;
};

/**
 * First position that still needs scanning (or has no valid plan); rows.length when all are done
 * Approved deviation: the desktop joins the codes into one text and splits it at every line break including GS,
 * so a KIZ with GS inside counts twice there; the station counts the code list the API gave, as positionView does
 */
export function firstIncompleteIndex(rows: readonly StationRow[]): number {
  const index = rows.findIndex((row) => row.planBlocks <= 0 || scannedBlocks(row, row.existingCodes) < row.planBlocks);
  return index === -1 ? rows.length : index;
}

/** null when index is past the last row, as the desktop leaves the screen untouched then */
export function positionView(rows: readonly StationRow[], index: number): PositionView | null {
  const row = rows[index];
  if (!row) return null;

  const plan = row.planBlocks;
  const scanned = scannedBlocks(row, row.existingCodes);
  const lineTotal = row.lineTotal ? `${groupThousands(row.lineTotal)} сум` : "не указана";
  const info = [
    `№ SkladBot: ${row.requestNumber}`,
    `📅 Дата отгрузки: ${row.orderDate || "не указана"}`,
    `👤 Торг.пред: ${row.representative}`,
    `📍 Адрес: ${row.address}`,
    `💳 Тип оплаты: ${row.paymentType}`,
    `💰 Сумма: ${lineTotal}`,
    `📦 План: ${plan} блоков (1 блок = ${PIECES_PER_BLOCK} ШТ)`,
  ].join("\n");

  const done = plan > 0 && scanned >= plan;
  const isLast = index >= rows.length - 1;
  return {
    info,
    client: `🏢 ${normalizeText(row.client) || "Юр.лицо не указано"}`,
    product: `📦 ${normalizeText(row.product) || "SKU не указан"}`,
    position: `Позиция ${index + 1} из ${rows.length}`,
    progress: `${scanned} / ${plan}`,
    lastCode: row.existingCodes.length ? `Уже записано: ${scanned} блоков, ${row.existingCodes.length} кодов` : "",
    nextState: done && !isLast ? "normal" : "disabled",
    finishState: done && isLast ? "normal" : "disabled",
  };
}

function unique(values: Iterable<string>): string[] {
  return [...new Set(values)];
}

export function partySummaryText(rows: readonly StationRow[], today: Date): string {
  if (!rows.length) return "Партия не выбрана";

  const blocks = rows.reduce((sum, row) => sum + row.planBlocks, 0);
  const total = rows.reduce((sum, row) => sum + row.lineTotal, 0);
  const requests = unique(rows.map((row) => normalizeText(row.requestNumber)).filter(Boolean)).sort(compareText);
  // the desktop sorts a set here, so dates the sort key cannot read (all +Infinity) come out in no fixed order there
  const dates = unique(
    rows.filter((row) => normalizeText(row.orderDate)).map((row) => parseDateToStandard(row.orderDate) || normalizeText(row.orderDate)),
  ).sort((a, b) => compareKeys([dateSortKey(a)], [dateSortKey(b)]));

  const shownRequests = requests.length
    ? requests.slice(0, 2).join(", ") + (requests.length > 2 ? ` +${requests.length - 2}` : "")
    : "без номера SkladBot";
  const shownDates = dates.length ? dates.map((date) => formatOrderDateHeader(date, today)).join(", ") : "дата не указана";
  return `Партия: ${rows.length} поз. · ${blocks} блок. · ${formatMoney(total)}\nДата отгрузки: ${shownDates} · Заявка: ${shownRequests}`;
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /tmp/station-plan2/frontend && npx vitest run src/station/__tests__/position.test.ts`
Expected: `Tests  22 passed`

- [ ] **Step 5: Commit**

```bash
cd /tmp/station-plan2
git add frontend/src/station/logic/position.ts frontend/src/station/__tests__/position.test.ts
ALLOW_NON_MAIN_BRANCH=1 git commit -m "feat(station): текущая позиция и сводка партии как у программы"
```

---

### Task 6: Правила скана

**Files:**
- Create: `frontend/src/station/logic/scanRules.ts`
- Test: `frontend/src/station/__tests__/scanRules.test.ts`

**Interfaces:**
- Consumes: `text.ts`, `rows.ts`, `position.ts`, `productKeys.ts`; `kizFormat.ts`, `scanQuantities.ts`; сеть и блоклист
  приходят зависимостями: `deps.availability` (ответ backend о доступности кода) и `deps.blockReason` (в окне плана 3
  это `blockedKizReason` из Task 3)
- Produces: `evaluateScan(input, deps)` асинхронно, порядок проверок как `on_scan` (`src/taksklad/app_scanning.py:487-615`);
  `findCodeOwner`, `formatDuplicateScanMessage`, `formatScanProductMismatchMessage`; побочные действия
  (очередь, журнал, окно «КИЗ занят») делает вызывающий код по полям ответа (`queued`, `releasePrompt`, `bell`)

- [ ] **Step 1: Write the failing test**

```ts
/**
 * evaluateScan, findCodeOwner and the duplicate message against what the desktop answered (corpus.scan,
 * corpus.owners, corpus.duplicate_messages); every case is the desktop on_scan run on the stated screen state
 * Two approved deviations: the SKU mismatch message names the site, and a code with GS inside is found by the station
 */

import { describe, expect, it, vi } from "vitest";

import { evaluateScan, findCodeOwner, formatDuplicateScanMessage, type ScanDeps, type ScanInput } from "../logic/scanRules";
import { corpus, labelled, rowFromSpec, type RowSpec } from "./support";

const VERSION = "2.0.56";
const NO_OWNER = "Владелец в локальном списке не найден.";

type ScanCase = {
  name: string;
  input: {
    raw: string;
    rows: RowSpec[];
    scanned: string[];
    booked: string[];
    owner_rows: RowSpec[];
    completed: string[];
    availability: null | "error" | { available: boolean; latest_movement_type?: string };
    blocked: boolean;
    update_required: boolean;
  };
  output: {
    state: string;
    message: string;
    scanned_codes_after: string[];
    queued: number;
    progress_text: string;
    last_code_text: string;
    status_text: string;
    next_enabled: string;
    finish_enabled: string;
    bell: number;
    release_prompt: number;
  };
};

// approved deviation (GS ruling): the desktop splits the code text and cannot find the holder of a code with GS inside
const GS_OWNER_FOUND = new Set(["duplicate_gs_code_other_order"]);

function ownerLines({ client, date, product, request }: RowSpec): string {
  return [`Заказ: ${client}`, `Дата отгрузки: ${date}`, `Товар: ${product}`, `SkladBot: ${request}`].map((line) => `${line}\n`).join("");
}

const cases = corpus.scan as unknown as ScanCase[];

function scanInput(input: ScanCase["input"], extra: Partial<ScanInput> = {}): ScanInput {
  return {
    raw: input.raw,
    version: VERSION,
    updateRequired: input.update_required,
    busy: false,
    rows: input.rows.map(rowFromSpec),
    index: 0,
    scannedCodes: input.scanned,
    existingCodes: new Set(input.booked),
    completedCodes: new Set(input.completed),
    ownerRows: input.owner_rows.map(rowFromSpec),
    ...extra,
  };
}

function scanDeps(input: ScanCase["input"], extra: Partial<ScanDeps> = {}): ScanDeps {
  return {
    blockReason: async () => (input.blocked ? corpus.blocklist.reason : ""),
    availability: async () => {
      if (input.availability === null || input.availability === "error") throw new Error("backend unavailable");
      return input.availability;
    },
    ...extra,
  };
}

describe("scan answers", () => {
  it.each(labelled(cases, (item) => item.name))("%s", async (name, { input, output }) => {
    const result = await evaluateScan(scanInput(input), scanDeps(input));

    const lines = result.message.split("\n");
    if (output.message.startsWith("КИЗ не соответствует товару")) {
      // approved deviation: the last two lines name the site, not the desktop program
      expect(lines.slice(0, 5)).toEqual(output.message.split("\n").slice(0, 5));
      expect(lines.slice(5)).toEqual([`Версия сайта: ${VERSION}`, "Если SKU на блоке верный, обновите страницу (F5)."]);
    } else if (GS_OWNER_FOUND.has(name)) {
      expect(output.message).toContain(NO_OWNER);
      expect(result.message).toBe(output.message.replace(`${NO_OWNER}\n`, ownerLines(input.owner_rows[0])));
    } else {
      expect(result.message).toBe(output.message);
    }
    expect({
      state: result.state,
      scannedCodesAfter: result.scannedCodesAfter,
      queued: result.queued,
      progressText: result.progressText,
      lastCodeText: result.lastCodeText,
      statusText: result.statusText,
      nextState: result.nextState,
      finishState: result.finishState,
      bell: result.bell,
      releasePrompt: result.releasePrompt,
    }).toEqual({
      state: output.state,
      scannedCodesAfter: output.scanned_codes_after,
      queued: output.queued > 0,
      progressText: output.progress_text,
      lastCodeText: output.last_code_text,
      statusText: output.status_text,
      nextState: output.next_enabled,
      finishState: output.finish_enabled,
      bell: output.bell > 0,
      releasePrompt: output.release_prompt > 0,
    });
  });
});

describe("what only the page does", () => {
  const byName = (name: string) => cases.find((item) => item.name === name)?.input as ScanCase["input"];

  // the desktop shows a busy dialog while an operation runs; the page has no such operation, only this answer
  it("answers busy without a bell or a refusal", async () => {
    const input = byName("accept_unit");
    const outcome = await evaluateScan(scanInput(input, { busy: true }), scanDeps(input));
    expect(outcome).toMatchObject({ state: "busy", message: "Дождитесь завершения текущей операции", bell: false, queued: false });
  });

  it("asks the backend about this code and this position", async () => {
    const input = byName("duplicate_other_order_backend_refuses");
    const availability = vi.fn(async () => ({ available: false }));
    await evaluateScan(scanInput(input), scanDeps(input, { availability }));
    expect(availability).toHaveBeenCalledExactlyOnceWith(input.raw, input.rows[0].item);
  });

  it("leaves the given screen state untouched", async () => {
    const input = byName("accept_unit");
    const scannedCodes = ["a"];
    const outcome = await evaluateScan(scanInput(input, { scannedCodes }), scanDeps(input));
    expect(scannedCodes).toEqual(["a"]);
    expect(outcome.scannedCodesAfter).toEqual(["a", input.raw]);
    expect(outcome.scannedCodesAfter).not.toBe(scannedCodes);
  });
});

describe("code owner", () => {
  type Held = { client: string; date: string; product: string; request: string };
  // approved deviation (GS ruling): the desktop finds nobody for a code with GS inside, the station finds its holder
  const GS_FOUND: Record<string, Held> = {
    gs_code: { client: "ООО Другой", date: "01.10.2026", product: "Chapman RED OP 20", request: "WH-R-7" },
  };

  it.each(labelled(corpus.owners as unknown as { name: string; input: { code: string; rows: RowSpec[] }; output: Held | null }[], (item) => item.name))(
    "%s",
    (name, { input, output }) => {
      const found = GS_FOUND[name];
      if (found) expect(output).toBeNull();
      const expected = found ?? output;
      expect(findCodeOwner(input.code, input.rows.map(rowFromSpec))).toEqual(
        expected && { client: expected.client, orderDate: expected.date, product: expected.product, requestNumber: expected.request },
      );
    },
  );
});

describe("duplicate message", () => {
  it.each(labelled(corpus.duplicate_messages, (item) => JSON.stringify([item.owner, item.status])))("%s", (_name, item) => {
    const owner = item.owner as { client?: string; order_date_display?: string; product?: string; skladbot_request_number?: string };
    const status = item.status as { checked?: boolean; available?: boolean; latest_movement_type?: string; reason?: string };
    expect(
      formatDuplicateScanMessage(
        item.code,
        owner.client === undefined
          ? null
          : {
              client: owner.client,
              orderDate: owner.order_date_display ?? "",
              product: owner.product ?? "",
              requestNumber: owner.skladbot_request_number ?? "",
            },
        status.checked === undefined
          ? null
          : {
              checked: status.checked,
              available: Boolean(status.available),
              latestMovementType: status.latest_movement_type ?? "",
              reason: status.reason ?? "",
            },
      ),
    ).toBe(item.text);
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /tmp/station-plan2/frontend && npx vitest run src/station/__tests__/scanRules.test.ts`
Expected: FAIL на импорте `../logic/scanRules`

- [ ] **Step 3: Write the implementation**

```ts
/**
 * The answer to one scanned code, step by step as the desktop gives it
 * (src/taksklad/app_scanning.py on_scan). The function is pure: it reads the screen state it
 * is given, asks the two services it is given, and returns what the screen must show
 */

import { kizFormatMessage, kizFormatViolation, normalizeKizCode } from "../../features/warehouse/kizFormat";
import { blockQuantityForCode, scanTypeForCode, SCAN_TYPE_AGGREGATE_BOX } from "../../features/warehouse/scanQuantities";
import type { ButtonState } from "./position";
import {
  aggregateProductMismatch,
  formatProductKeyLabel,
  productKeyFromName,
  scanCodeProductKey,
  scanProductMismatch,
} from "./productKeys";
import { scannedBlocks, type StationRow } from "./rows";
import { normalizeText } from "./text";

export type CodeOwner = { client: string; orderDate: string; product: string; requestNumber: string };

/** What the backend said about reusing a code that is already booked elsewhere */
export type ReuseStatus = { checked: boolean; available: boolean; latestMovementType: string; reason: string };

export type ScanDeps = {
  /** The raw backend answer for a code against an order item; rejects when the backend cannot be asked */
  availability: (code: string, orderItemId: string) => Promise<{ available: boolean; latest_movement_type?: string }>;
  /** The refusal text of a blocked code, "" for any other */
  blockReason: (code: string) => Promise<string>;
};

export type ScanInput = {
  raw: string;
  /** The site version shown in the SKU mismatch message */
  version: string;
  updateRequired: boolean;
  busy: boolean;
  rows: readonly StationRow[];
  index: number;
  scannedCodes: readonly string[];
  /** Codes already booked to any order today */
  existingCodes: ReadonlySet<string>;
  /** Codes of the orders finished in this session */
  completedCodes: ReadonlySet<string>;
  /** Rows searched for the owner of a duplicate code */
  ownerRows: readonly StationRow[];
};

export type ScanOutcome = {
  state: "accepted" | "rejected" | "ignored" | "busy";
  message: string;
  scannedCodesAfter: string[];
  /** True when the code must go to the backend queue */
  queued: boolean;
  progressText: string;
  lastCodeText: string;
  statusText: string;
  /** "" leaves the button as it is */
  nextState: ButtonState | "";
  finishState: ButtonState | "";
  bell: boolean;
  /** True when the operator is offered to release the code */
  releasePrompt: boolean;
};

// movements after which the backend lets a code be scanned again (backend_flow.py REUSABLE_KIZ_MOVEMENTS)
const REUSABLE_MOVEMENTS = new Set(["return", "undo", "reset"]);
// the desktop appends the running operation and its seconds, the page has no such state
const BUSY_MESSAGE = "Дождитесь завершения текущей операции";

/** Approved deviation: the desktop searches the split code text and misses a code with GS inside, the station searches the list */
export function findCodeOwner(code: string, rows: readonly StationRow[]): CodeOwner | null {
  const target = normalizeKizCode(code);
  const row = target ? rows.find((candidate) => candidate.existingCodes.some((held) => normalizeKizCode(held) === target)) : undefined;
  return row ? { client: row.client, orderDate: row.orderDate, product: row.product, requestNumber: row.requestNumber } : null;
}

function backendLine(status: ReuseStatus): string {
  const reason = normalizeText(status.reason);
  const movement = normalizeText(status.latestMovementType);
  if (!status.checked) return reason ? `Backend-проверка повтора недоступна: ${reason}` : "";
  if (status.available) return `Backend разрешил повтор: ${reason || movement || "КИЗ возвращён"}`;
  if (movement) return `Backend не разрешил повтор. Последнее движение: ${movement}`;
  return reason ? `Backend не разрешил повтор: ${reason}` : "";
}

export function formatDuplicateScanMessage(code: string, owner: CodeOwner | null, status: ReuseStatus | null): string {
  const normalized = normalizeKizCode(code);
  const owned: [string, string][] = owner
    ? [["Заказ", owner.client], ["Дата отгрузки", owner.orderDate], ["Товар", owner.product], ["SkladBot", owner.requestNumber]]
    : [];
  return [
    "КИЗ уже отсканирован в другом заказе.",
    owner ? "" : "Владелец в локальном списке не найден.",
    ...owned.map(([label, value]) => (normalizeText(value) ? `${label}: ${normalizeText(value)}` : "")),
    status ? backendLine(status) : "",
    normalized ? `Код: ${normalized}` : "",
    "Сканируйте другой КИЗ.",
  ]
    .filter(Boolean)
    .join("\n");
}

/** Lines 6 and 7 deliberately differ from the desktop (version of the site, F5 instead of restarting the program) */
export function formatScanProductMismatchMessage(code: string, product: string, version: string): string {
  const normalized = normalizeKizCode(code);
  return [
    "КИЗ не соответствует товару текущей позиции.",
    `Позиция: ${normalizeText(product) || "товар не указан"}`,
    `Ожидалось: ${formatProductKeyLabel(productKeyFromName(product))}`,
    `КИЗ распознан как: ${formatProductKeyLabel(scanCodeProductKey(normalized))}`,
    `Префикс КИЗа: ${normalized.slice(0, 18)}${normalized.length > 18 ? "..." : ""}`,
    `Версия сайта: ${version}`,
    "Если SKU на блоке верный, обновите страницу (F5).",
  ].join("\n");
}

/** The refusal text for a code that cannot go into this position, "" when it can */
function positionProblem(row: StationRow, code: string, scannedCodes: readonly string[], version: string): string {
  const plan = row.planBlocks;
  if (plan <= 0) return "В заказе не указано корректное 'Кол-во блок'";
  const before = scannedBlocks(row, scannedCodes);
  if (before >= plan) return `План выполнен! Нельзя сканировать больше ${plan} блоков`;
  if (scanProductMismatch(code, row.product)) return formatScanProductMismatchMessage(code, row.product, version);
  if (scanTypeForCode(code) === SCAN_TYPE_AGGREGATE_BOX) {
    if (aggregateProductMismatch(code, row.product)) return "Код короба не соответствует товару текущей позиции";
    const remaining = plan - before;
    const blocks = blockQuantityForCode(code);
    if (blocks > remaining) return `Короб +${blocks} блоков превышает остаток позиции: осталось ${remaining}`;
  }
  return scannedCodes.includes(code) ? "Код уже отсканирован в этой позиции" : "";
}

/** Backend verdict on reusing a code (backend_flow.py backend_duplicate_scan_reuse_status) */
async function reuseStatus(row: StationRow, code: string, deps: ScanDeps): Promise<ReuseStatus> {
  const itemId = normalizeText(row.backendOrderItemId);
  if (!itemId) {
    return { checked: false, available: false, latestMovementType: "", reason: "backend path is unavailable for this position" };
  }
  try {
    const answer = await deps.availability(code, itemId);
    const movement = normalizeText(answer.latest_movement_type).toLowerCase();
    return {
      checked: true,
      available: Boolean(answer.available) && REUSABLE_MOVEMENTS.has(movement),
      latestMovementType: movement,
      reason: movement ? `latest movement is ${movement}` : "",
    };
  } catch {
    return { checked: false, available: false, latestMovementType: "", reason: "backend availability check failed" };
  }
}

/** The refusal text for a code booked elsewhere that the backend does not release, "" otherwise */
async function duplicateProblem(input: ScanInput, row: StationRow, code: string, deps: ScanDeps): Promise<string> {
  const booked = input.existingCodes.has(code);
  if (!booked && !input.completedCodes.has(code)) return "";
  const status = await reuseStatus(row, code, deps);
  if (status.available) return "";
  return booked
    ? formatDuplicateScanMessage(code, findCodeOwner(code, input.ownerRows), status)
    : "Код уже использован в другом задании сегодня";
}

function accepted(input: ScanInput, row: StationRow, code: string): ScanOutcome {
  const scannedCodesAfter = [...input.scannedCodes, code];
  const scanned = scannedBlocks(row, scannedCodesAfter);
  const plan = row.planBlocks;
  const blocks = blockQuantityForCode(code);
  const isBox = scanTypeForCode(code) === SCAN_TYPE_AGGREGATE_BOX;
  const message = isBox ? `Отсканирован короб +${blocks} (${scanned}/${plan})` : `Отсканирован код (${scanned}/${plan})`;
  const shown = code.slice(0, 40);
  const outcome: ScanOutcome = {
    state: "accepted",
    message,
    scannedCodesAfter,
    queued: true,
    progressText: `${scanned} / ${plan}`,
    lastCodeText: isBox ? `Последний код: короб +${blocks}: ${shown}...` : `Последний код: ${shown}...`,
    statusText: `✅ ${message}`,
    nextState: "",
    finishState: "",
    bell: false,
    releasePrompt: false,
  };
  if (scanned < plan) return outcome;
  if (input.index >= input.rows.length - 1) {
    return { ...outcome, statusText: "🎯 Заказ выполнен! Нажмите 'ЗАВЕРШИТЬ ЗАКАЗ'", nextState: "disabled", finishState: "normal" };
  }
  return { ...outcome, statusText: "🎯 Позиция выполнена! Нажмите 'Следующая позиция'", nextState: "normal", finishState: "disabled" };
}

export async function evaluateScan(input: ScanInput, deps: ScanDeps): Promise<ScanOutcome> {
  const nothing: ScanOutcome = {
    state: "ignored",
    message: "",
    scannedCodesAfter: [...input.scannedCodes],
    queued: false,
    progressText: "",
    lastCodeText: "",
    statusText: "",
    nextState: "",
    finishState: "",
    bell: false,
    releasePrompt: false,
  };
  const rejected = (message: string, releasePrompt = false): ScanOutcome => ({
    ...nothing,
    state: "rejected",
    message,
    bell: true,
    releasePrompt,
  });

  if (input.updateRequired) return rejected("Требуется обновить приложение перед сканированием");
  if (input.busy) return { ...nothing, state: "busy", message: BUSY_MESSAGE };
  const row = input.rows[input.index];
  if (!row) return rejected("Сначала выберите заказ");

  const code = normalizeKizCode(input.raw);
  if (!code) return nothing;
  const rule = kizFormatViolation(code);
  if (rule) return rejected(kizFormatMessage(rule, code.length));
  const blockReason = await deps.blockReason(code);
  if (blockReason) return rejected(`🚫 ${blockReason}`);

  const problem = positionProblem(row, code, input.scannedCodes, input.version);
  if (problem) return rejected(problem);
  const duplicate = await duplicateProblem(input, row, code, deps);
  if (duplicate) return rejected(duplicate, true);
  if (!normalizeText(row.backendOrderItemId)) return rejected("Позиция не связана с backend. Сканирование заблокировано");
  return accepted(input, row, code);
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /tmp/station-plan2/frontend && npx vitest run src/station/__tests__/scanRules.test.ts`
Expected: `Tests  50 passed`

- [ ] **Step 5: Commit**

```bash
cd /tmp/station-plan2
git add frontend/src/station/logic/scanRules.ts frontend/src/station/__tests__/scanRules.test.ts
ALLOW_NON_MAIN_BRANCH=1 git commit -m "feat(station): правила скана в порядке программы, сообщения дословно"
```

---

### Task 7: Палитра и сторож текстов

**Files:**
- Create: `frontend/src/station/tokens.ts`
- Test: `frontend/src/station/__tests__/tokens.test.ts`
- Create: `tests/test_station_text_parity.py`

**Interfaces:**
- Consumes: корпус (раздел `palette`), файлы `frontend/src/station/logic/*.ts` и `src/taksklad/**`
- Produces: `STATION_COLORS` и `fadeHex(color, amount)` для вёрстки плана 3; сторож: любое кириллическое
  сообщение логики станции найдено дословно в программе или стоит в перечне согласованных отличий

- [ ] **Step 1: Write the failing tests**

`frontend/src/station/__tests__/tokens.test.ts`:

```ts
/** The colours of the station equal the desktop palette and the main window colours (corpus.palette) */

import { describe, expect, it } from "vitest";

import * as tokens from "../tokens";
import { corpus, labelled } from "./support";

const { desktop, window, fade } = corpus.palette;

describe("tokens", () => {
  it("are exactly the desktop palette and the window colours", () => {
    const colours = Object.fromEntries(Object.entries(tokens).filter(([, value]) => typeof value === "string"));
    expect(colours).toEqual({ ...desktop, ...window });
  });

  it.each(labelled(fade, (item) => `${item.color} by ${item.amount}`))("fadeHex of %s", (_name, item) => {
    expect(tokens.fadeHex(item.color, item.amount)).toBe(item.result);
    // the hover colour of a button takes the default amount
    if (item.amount === 0.1) expect(tokens.fadeHex(item.color)).toBe(item.result);
  });
});
```

`tests/test_station_text_parity.py`:

```python
"""Operator-facing texts of the station logic must be the desktop's words

Every string literal in `frontend/src/station/logic/*.ts` that holds a Cyrillic word is read as an operator-facing
message. Its fixed parts (the text between `${...}` placeholders and `\\n` breaks) must occur verbatim in a file under
`src/taksklad/`, except the approved deviations named below
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOGIC = ROOT / "frontend" / "src" / "station" / "logic"
DESKTOP = ROOT / "src" / "taksklad"

# the station is a page, not the program: the last two lines of the SKU mismatch message name the site and F5
APPROVED_DEVIATIONS = {
    "Версия сайта: ",
    "Если SKU на блоке верный, обновите страницу (F5).",
}

BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.S)
LINE_COMMENT = re.compile(r"//[^\n]*")
# a regex literal can hold quote marks, so it is cut before the string literals are read
REGEX_LITERAL = re.compile(r"(?<=[(,=:])\s*/(?![/*])(?:[^/\\\n\[]|\\.|\[(?:[^\]\\\n]|\\.)*\])+/[a-z]*")
# a string literal on one line: "double quoted" or `template`
STRING_LITERAL = re.compile(r'"((?:[^"\\\n]|\\.)*)"|`((?:[^`\\\n]|\\.)*)`')
PLACEHOLDER = re.compile(r"\$\{[^{}]*\}")
# two Cyrillic letters in a row: a word, unlike the single "ё" in a replaceAll() call
CYRILLIC_WORD = re.compile(r"[А-Яа-яЁё]{2,}")


def code_without_comments(source):
    return REGEX_LITERAL.sub("", LINE_COMMENT.sub("", BLOCK_COMMENT.sub("", source)))


def fixed_parts(source):
    """The fixed parts of every Cyrillic string literal, one list per literal; a placeholder is code, searched in turn"""
    for match in STRING_LITERAL.finditer(source):
        text = match.group(1) if match.group(1) is not None else match.group(2)
        for placeholder in PLACEHOLDER.findall(text):
            yield from fixed_parts(placeholder[2:-1])
        parts = [part for part in re.split(r"\$\{[^{}]*\}|\\n", text) if part.strip()]
        if CYRILLIC_WORD.search("".join(parts)):
            yield parts


class StationTextParityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.desktop = "\n".join(path.read_text(encoding="utf-8") for path in sorted(DESKTOP.rglob("*.py")))
        cls.found = [
            (path.name, parts)
            for path in sorted(LOGIC.glob("*.ts"))
            for parts in fixed_parts(code_without_comments(path.read_text(encoding="utf-8")))
        ]

    def test_extraction_reads_every_logic_file(self):
        # a broken extraction would check nothing and pass, so it is pinned by what it must find
        self.assertGreater(len(self.found), 40)
        self.assertEqual({name for name, _parts in self.found}, {path.name for path in LOGIC.glob("*.ts")})

    def test_every_fixed_part_occurs_in_the_desktop_sources(self):
        missing = [
            f"{name}: {part!r}"
            for name, parts in self.found
            for part in parts
            if part not in self.desktop and part not in APPROVED_DEVIATIONS
        ]
        self.assertEqual(missing, [])

    def test_the_approved_deviations_are_real(self):
        # an allowance for a text that is not in the logic, or that the desktop has after all, must go
        messages = {part for _name, parts in self.found for part in parts}
        self.assertEqual(APPROVED_DEVIATIONS - messages, set())
        for part in APPROVED_DEVIATIONS:
            self.assertNotIn(part, self.desktop)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /tmp/station-plan2/frontend && npx vitest run src/station/__tests__/tokens.test.ts`
Expected: FAIL на импорте `../tokens`
Run: `cd /tmp/station-plan2 && PYTHONPATH=. /Users/anton/Documents/work/TakSklad/.venv/bin/python -m unittest tests.test_station_text_parity -v`
Expected: зелёный сразу (запирающий: логика задач 2-6 уже на месте); краснеет, если в любом сообщении
`frontend/src/station/logic/*.ts` заменить одну букву (проверено в черновике заменой «выберите» на «выберете»)

- [ ] **Step 3: Write the implementation**

`frontend/src/station/tokens.ts`:

```ts
/**
 * Colours of the desktop program, so the station looks the same to the warehouse
 * The first block is src/taksklad/config.py, the second is the main window (app_layout.py, order_list_widgets.py);
 * the corpus (tools/generate_station_parity_corpus.py) records both and tokens.test.ts requires equality
 */

/** ui_widgets.fade_hex: the colour moved `amount` of the way to white, the hover colour of a button (default 0.10) */
export function fadeHex(color: string, amount = 0.1): string {
  const hex = color.replace(/^#+/, "");
  // the desktop also reads exotic forms like "+f0000"; colours here are always #rrggbb
  if (!/^[0-9a-fA-F]{6}$/.test(hex)) return `#${hex}`;
  const share = Math.max(0, Math.min(1, amount));
  const channel = (start: number) => {
    const value = parseInt(hex.slice(start, start + 2), 16);
    return Math.min(255, Math.floor(value + (255 - value) * share));
  };
  return `#${[0, 2, 4].map((start) => channel(start).toString(16).padStart(2, "0")).join("")}`;
}

export const BG_MAIN = "#f4f1e8";
export const BG_CARD = "#fffdf7";
export const FG_TEXT = "#2e2c28";
export const FG_MUTED = "#777066";
export const ACCENT = "#b28224";
export const SUCCESS = "#2f8a4a";
export const INFO = "#3a6b8f";
export const WARNING = "#d8b64c";
export const DANGER = "#b7483c";
export const ERROR_BG = "#f8ded9";
export const ERROR_FG = "#b7483c";
export const BORDER = "#d8d0bf";
export const DISABLED_BG = "#e0d7b9";
export const DISABLED_FG = "#8f8878";

export const LIST_SURFACE_BG = "#fffaf2";
export const SELECTED_CARD_BG = "#fff6df";
export const PLACEHOLDER_FG = "#a7a095";
export const PRODUCT_PHOTO_BG = "#fffaf0";
export const PRODUCT_PHOTO_SHELL_BG = "#f3ead8";
export const GTIN_BADGE_FG = "#fff7df";
export const SCROLLBAR_BG = "#e5dcc8";
export const SCROLLBAR_THUMB = "#c4ad7a";
export const SCROLLBAR_THUMB_HOVER = fadeHex(SCROLLBAR_THUMB, 0.12);
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /tmp/station-plan2/frontend && npx vitest run src/station/__tests__/tokens.test.ts`
Expected: `Tests  21 passed`
Run: `cd /tmp/station-plan2 && PYTHONPATH=. /Users/anton/Documents/work/TakSklad/.venv/bin/python -m unittest tests.test_station_text_parity tests.test_station_parity_corpus -v`
Expected: `Ran 5 tests` `OK`

- [ ] **Step 5: Commit**

```bash
cd /tmp/station-plan2
git add frontend/src/station/tokens.ts frontend/src/station/__tests__/tokens.test.ts tests/test_station_text_parity.py
ALLOW_NON_MAIN_BRANCH=1 git commit -m "feat(station): палитра программы и сторож текстов сообщений"
```

---

### Task 8: Вход станции, полная загрузка заказов и освобождение КИЗ в API

**Files:**
- Modify: `frontend/src/api/core.ts` (`apiRequestWithHeaders`, `ApiResponse`, `retryAfterSeconds` у ошибки)
- Modify: `frontend/src/api/auth.ts` (`stationLogin`)
- Modify: `frontend/src/api.ts` (реэкспорт, `listAllActiveOrders`, `releaseKiz`; поля `OrderItem` уже из Task 2)
- Test: `frontend/src/__tests__/api.station.test.ts`

**Interfaces:**
- Consumes: существующие `apiRequest`, `ApiRequestError`, `ApiConfig` из `api/core.ts`
- Produces: `apiRequestWithHeaders<T>(config, path, options) → Promise<{ data: T; headers: Headers }>` (единственный путь
  fetch, `apiRequest` теперь его обёртка); `ApiRequestError.retryAfterSeconds` (секунды из `Retry-After`, 0 без него);
  `stationLogin(config) → Promise<AuthSession>` (`POST /api/v1/auth/station` без тела);
  `listAllActiveOrders(config, { signal }) → Promise<Order[]>` (страницы по 200 до пустого
  `X-TakSklad-Next-Cursor`, не больше 100 страниц, повтор курсора это ошибка, тексты ошибок как у
  `backend_request_all_pages` программы); `releaseKiz(config, payload) → Promise<KizReleaseResult>`
  (`POST /api/v1/kiz/release`, как `release_kiz` программы)

Почему полная загрузка: программа видит все активные заказы постранично, а `listActiveOrders` веба берёт
первые 500; на складе бывает больше, и станция не должна терять заказы хвоста

- [ ] **Step 1: Write the failing test**

`frontend/src/__tests__/api.station.test.ts`:

```ts
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";

import {
  ACTIVE_ORDERS_MAX_PAGES,
  ApiRequestError,
  apiRequestWithHeaders,
  listAllActiveOrders,
  releaseKiz,
  stationLogin,
  type ApiConfig,
  type Order,
} from "../api";
import { activeOrder, anonymousSession } from "./fixtures";
import { server } from "./server";

const config: ApiConfig = { apiUrl: "", token: "", csrfToken: "" };
const CURSOR_HEADER = "X-TakSklad-Next-Cursor";

const stationSession = {
  ...anonymousSession,
  authenticated: true,
  login: "warehouse-station",
  role: "station",
  permissions: ["warehouse:read", "warehouse:write", "reports:read"],
  csrf_token: "station-csrf",
};

function orderWithId(id: string): Order {
  return { ...activeOrder, id };
}

async function rejection(promise: Promise<unknown>): Promise<ApiRequestError> {
  const error = await promise.then(() => null, (reason: unknown) => reason);
  expect(error).toBeInstanceOf(ApiRequestError);
  return error as ApiRequestError;
}

describe("apiRequestWithHeaders", () => {
  it("returns the parsed body together with the response headers", async () => {
    server.use(http.get("/api/v1/probe", () => HttpResponse.json({ ok: true }, { headers: { "X-Probe": "yes" } })));

    const { data, headers } = await apiRequestWithHeaders<{ ok: boolean }>(config, "/api/v1/probe");

    expect(data).toEqual({ ok: true });
    expect(headers.get("X-Probe")).toBe("yes");
  });

  it("puts Retry-After seconds on the error", async () => {
    server.use(http.get("/api/v1/probe", () => HttpResponse.json({}, { status: 429, headers: { "Retry-After": "75" } })));

    const failure = await rejection(apiRequestWithHeaders(config, "/api/v1/probe"));

    expect(failure.status).toBe(429);
    expect(failure.retryAfterSeconds).toBe(75);
  });

  it("reads Retry-After given as an HTTP date and ignores garbage", async () => {
    const inTwoMinutes = new Date(Date.now() + 120_000).toUTCString();
    server.use(
      http.get("/api/v1/dated", () => HttpResponse.json({}, { status: 429, headers: { "Retry-After": inTwoMinutes } })),
      http.get("/api/v1/garbage", () => HttpResponse.json({}, { status: 429, headers: { "Retry-After": "soon" } })),
      http.get("/api/v1/absent", () => HttpResponse.json({}, { status: 503 })),
    );

    const dated = await rejection(apiRequestWithHeaders(config, "/api/v1/dated"));
    const garbage = await rejection(apiRequestWithHeaders(config, "/api/v1/garbage"));
    const absent = await rejection(apiRequestWithHeaders(config, "/api/v1/absent"));

    expect(dated.retryAfterSeconds).toBeGreaterThan(100);
    expect(dated.retryAfterSeconds).toBeLessThanOrEqual(120);
    expect(garbage.retryAfterSeconds).toBe(0);
    expect(absent.retryAfterSeconds).toBe(0);
  });
});

describe("stationLogin", () => {
  it("posts with no body and returns the station session", async () => {
    let seen: { method: string; body: string } | null = null;
    server.use(http.post("/api/v1/auth/station", async ({ request }) => {
      seen = { method: request.method, body: await request.text() };
      return HttpResponse.json(stationSession);
    }));

    await expect(stationLogin(config)).resolves.toEqual(stationSession);

    expect(seen).toEqual({ method: "POST", body: "" });
  });

  it("surfaces the denial code outside the warehouse network", async () => {
    server.use(http.post("/api/v1/auth/station", () => HttpResponse.json(
      { detail: { code: "station_network_denied" } },
      { status: 403 },
    )));

    const failure = await rejection(stationLogin(config));

    expect(failure.status).toBe(403);
    expect(failure.code).toBe("station_network_denied");
  });
});

describe("listAllActiveOrders", () => {
  it("follows the cursor header across three pages and keeps the order", async () => {
    const urls: string[] = [];
    server.use(http.get("/api/v1/orders/active", ({ request }) => {
      const url = new URL(request.url);
      urls.push(`${url.searchParams.get("limit")}|${url.searchParams.get("cursor") ?? ""}`);
      const cursor = url.searchParams.get("cursor");
      if (!cursor) return HttpResponse.json([orderWithId("o1"), orderWithId("o2")], { headers: { [CURSOR_HEADER]: "c2" } });
      if (cursor === "c2") return HttpResponse.json([orderWithId("o3")], { headers: { [CURSOR_HEADER]: "c3" } });
      return HttpResponse.json([orderWithId("o4")]);
    }));

    const orders = await listAllActiveOrders(config);

    expect(orders.map((order) => order.id)).toEqual(["o1", "o2", "o3", "o4"]);
    expect(urls).toEqual(["200|", "200|c2", "200|c3"]);
  });

  it("stops after the first page when the header is absent", async () => {
    let requests = 0;
    server.use(http.get("/api/v1/orders/active", () => {
      requests += 1;
      return HttpResponse.json([orderWithId("only")]);
    }));

    await expect(listAllActiveOrders(config)).resolves.toHaveLength(1);

    expect(requests).toBe(1);
  });

  it("treats an empty cursor header as the end", async () => {
    server.use(http.get("/api/v1/orders/active", () => HttpResponse.json([], { headers: { [CURSOR_HEADER]: " " } })));

    await expect(listAllActiveOrders(config)).resolves.toEqual([]);
  });

  it("stops with an error after 100 pages", async () => {
    let requests = 0;
    server.use(http.get("/api/v1/orders/active", () => {
      requests += 1;
      return HttpResponse.json([orderWithId(`o${requests}`)], { headers: { [CURSOR_HEADER]: `page-${requests + 1}` } });
    }));

    await expect(listAllActiveOrders(config)).rejects.toThrow("Backend pagination exceeded the page safety limit");

    expect(ACTIVE_ORDERS_MAX_PAGES).toBe(100);
    expect(requests).toBe(100);
  });

  it("stops at a repeated cursor instead of looping", async () => {
    let requests = 0;
    server.use(http.get("/api/v1/orders/active", () => {
      requests += 1;
      return HttpResponse.json([orderWithId(`o${requests}`)], { headers: { [CURSOR_HEADER]: "same" } });
    }));

    await expect(listAllActiveOrders(config)).rejects.toThrow("Backend pagination returned a repeated cursor");

    expect(requests).toBe(2);
  });

  it("passes the abort signal to the request", async () => {
    server.use(http.get("/api/v1/orders/active", () => HttpResponse.json([])));
    const controller = new AbortController();
    controller.abort();

    await expect(listAllActiveOrders(config, { signal: controller.signal })).rejects.toThrow();
  });
});

describe("releaseKiz", () => {
  it("posts the five desktop fields and returns the result", async () => {
    let payload: unknown = null;
    const result = {
      code: "0104006396053947217ABCDEF",
      released: true,
      outcome: "released",
      latest_movement_type: "outbound",
      donor_order_item_id: "item-9",
      donor_request_number: "WH-R-9",
    };
    server.use(http.post("/api/v1/kiz/release", async ({ request }) => {
      payload = await request.json();
      return HttpResponse.json(result);
    }));

    const released = await releaseKiz({ ...config, csrfToken: "csrf" }, {
      code: "0104006396053947217ABCDEF",
      reason: "returned_to_shelf",
      comment: "",
      workstation_id: "web-station",
      actor: "station",
    });

    expect(released).toEqual(result);
    expect(payload).toEqual({
      code: "0104006396053947217ABCDEF",
      reason: "returned_to_shelf",
      comment: "",
      workstation_id: "web-station",
      actor: "station",
    });
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /tmp/station-plan2/frontend && npx vitest run src/__tests__/api.station.test.ts`
Expected: FAIL, в `../api` нет `stationLogin`, `listAllActiveOrders`, `releaseKiz`, `apiRequestWithHeaders`

- [ ] **Step 3: Write the implementation**

```diff
diff --git a/frontend/src/api/auth.ts b/frontend/src/api/auth.ts
index b9d76b1..5c1be18 100644
--- a/frontend/src/api/auth.ts
+++ b/frontend/src/api/auth.ts
@@ -20,6 +20,11 @@ export function loginWeb(config: ApiConfig, login: string, password: string) {
   });
 }
 
+/** Passwordless warehouse sign-in: the server decides by the client network, no body is sent. */
+export function stationLogin(config: ApiConfig) {
+  return apiRequest<AuthSession>(config, "/api/v1/auth/station", { method: "POST" });
+}
+
 export function logoutWeb(config: ApiConfig) {
   return apiRequest<AuthSession>(config, "/api/v1/auth/logout", { method: "POST" });
 }
diff --git a/frontend/src/api/core.ts b/frontend/src/api/core.ts
index 088d1e3..087b9c7 100644
--- a/frontend/src/api/core.ts
+++ b/frontend/src/api/core.ts
@@ -17,14 +17,17 @@ export class ApiRequestError extends Error {
   status: number;
   statusText: string;
   code: string;
+  /** Seconds from the Retry-After response header, 0 when the server sent none. */
+  retryAfterSeconds: number;
 
-  constructor(status: number, statusText: string, detail: string, code = "") {
+  constructor(status: number, statusText: string, detail: string, code = "", retryAfterSeconds = 0) {
     const prefix = `${status} ${statusText}`.trim();
     super(detail ? `${prefix}: ${detail}` : prefix || "Ошибка запроса");
     this.name = "ApiRequestError";
     this.status = status;
     this.statusText = statusText;
     this.code = code;
+    this.retryAfterSeconds = retryAfterSeconds;
   }
 }
 
@@ -36,11 +39,22 @@ export function defaultApiUrl() {
   return "";
 }
 
+export type ApiResponse<T> = { data: T; headers: Headers };
+
 export async function apiRequest<T>(
   config: ApiConfig,
   path: string,
   options: RequestOptions = {},
 ): Promise<T> {
+  return (await apiRequestWithHeaders<T>(config, path, options)).data;
+}
+
+/** The one fetch path: same request as `apiRequest`, plus the response headers (paging cursors). */
+export async function apiRequestWithHeaders<T>(
+  config: ApiConfig,
+  path: string,
+  options: RequestOptions = {},
+): Promise<ApiResponse<T>> {
   const apiUrl = config.apiUrl.replace(/\/$/, "");
   const method = (options.method ?? "GET").toUpperCase();
   const bearerRequest = Boolean(config.token);
@@ -86,10 +100,25 @@ export async function apiRequest<T>(
         detail = formatTextApiErrorDetail(response.status, body);
       }
     }
-    throw new ApiRequestError(response.status, response.statusText, detail, code);
+    throw new ApiRequestError(
+      response.status,
+      response.statusText,
+      detail,
+      code,
+      parseRetryAfterSeconds(response.headers.get("Retry-After")),
+    );
   }
 
-  return response.json() as Promise<T>;
+  return { data: (await response.json()) as T, headers: response.headers };
+}
+
+/** Retry-After is either whole seconds or an HTTP date. */
+function parseRetryAfterSeconds(value: string | null): number {
+  const text = (value ?? "").trim();
+  if (!text) return 0;
+  if (/^\d+$/.test(text)) return Number(text);
+  const date = Date.parse(text);
+  return Number.isNaN(date) ? 0 : Math.max(0, Math.ceil((date - Date.now()) / 1000));
 }
 
 function apiErrorCode(payload: unknown): string {
```

```diff
diff --git a/frontend/src/api.ts b/frontend/src/api.ts
index 5f7d2a6..fd57c46 100644
--- a/frontend/src/api.ts
+++ b/frontend/src/api.ts
@@ -585,12 +587,18 @@ export type AdminOrderCapability = {
   disabled_reasons: Record<string, string>;
 };
 
-import { ApiRequestError, apiRequest, ensureCookieApiIsSameOrigin, LONG_REQUEST_TIMEOUT_MS } from "./api/core";
+import {
+  ApiRequestError,
+  apiRequest,
+  apiRequestWithHeaders,
+  ensureCookieApiIsSameOrigin,
+  LONG_REQUEST_TIMEOUT_MS,
+} from "./api/core";
 import type { ApiConfig } from "./api/core";
 
-export { ApiRequestError, apiRequest, defaultApiUrl } from "./api/core";
-export type { ApiConfig, RequestOptions } from "./api/core";
-export { getAuthSession, loginWeb, logoutWeb } from "./api/auth";
+export { ApiRequestError, apiRequest, apiRequestWithHeaders, defaultApiUrl } from "./api/core";
+export type { ApiConfig, ApiResponse, RequestOptions } from "./api/core";
+export { getAuthSession, loginWeb, logoutWeb, stationLogin } from "./api/auth";
 export type { AuthSession } from "./api/auth";
 
 export type AdminTableRequest = {
@@ -658,6 +666,37 @@ export function listActiveOrders(config: ApiConfig, limit = 500) {
   return apiRequest<Order[]>(config, `/api/v1/orders/active?${query.toString()}`);
 }
 
+const ACTIVE_ORDERS_PAGE_LIMIT = 200;
+export const ACTIVE_ORDERS_MAX_PAGES = 100;
+const NEXT_CURSOR_HEADER = "X-TakSklad-Next-Cursor";
+
+/**
+ * Every active order, page by page, until the server stops sending a cursor.
+ * Same walk as the desktop (`backend_request_all_pages` in `src/taksklad/backend_client.py`),
+ * with the same safety stops and error texts.
+ */
+export async function listAllActiveOrders(config: ApiConfig, options: { signal?: AbortSignal } = {}) {
+  const orders: Order[] = [];
+  const seenCursors = new Set<string>();
+  let cursor = "";
+  for (let page = 1; page <= ACTIVE_ORDERS_MAX_PAGES; page += 1) {
+    const query = new URLSearchParams({ limit: String(ACTIVE_ORDERS_PAGE_LIMIT) });
+    if (cursor) query.set("cursor", cursor);
+    const { data, headers } = await apiRequestWithHeaders<Order[]>(
+      config,
+      `/api/v1/orders/active?${query.toString()}`,
+      { signal: options.signal },
+    );
+    orders.push(...data);
+    const next = (headers.get(NEXT_CURSOR_HEADER) ?? "").trim();
+    if (!next) return orders;
+    if (seenCursors.has(next)) throw new Error("Backend pagination returned a repeated cursor");
+    seenCursors.add(next);
+    cursor = next;
+  }
+  throw new Error("Backend pagination exceeded the page safety limit");
+}
+
 export function getAdminTable(config: ApiConfig, options: AdminTableRequest = {}) {
   const query = new URLSearchParams();
   if (options.cursor) query.set("cursor", options.cursor);
@@ -882,6 +921,31 @@ export function markReturn(config: ApiConfig, orderId: string, payload: {
   });
 }
 
+export type KizReleasePayload = {
+  code: string;
+  reason: string;
+  comment: string;
+  workstation_id: string | null;
+  actor: string;
+};
+
+export type KizReleaseResult = {
+  code: string;
+  released: boolean;
+  outcome: string;
+  latest_movement_type: string;
+  donor_order_item_id: string;
+  donor_request_number: string;
+};
+
+/** The block is physically back on the shelf, so its KIZ may ship again (desktop `release_kiz`). */
+export function releaseKiz(config: ApiConfig, payload: KizReleasePayload) {
+  return apiRequest<KizReleaseResult>(config, "/api/v1/kiz/release", {
+    method: "POST",
+    body: payload,
+  });
+}
+
 export async function downloadDiagnosticsLog(config: ApiConfig) {
   const apiUrl = config.apiUrl.replace(/\/$/, "");
   ensureCookieApiIsSameOrigin(apiUrl, Boolean(config.token));
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /tmp/station-plan2/frontend && npx vitest run src/__tests__/api.station.test.ts && npx vitest run && npm run typecheck`
Expected: `Tests  12 passed` в первом прогоне; полный прогон зелёный (старые тесты `apiRequest` не
заметили смены пути fetch); typecheck без ошибок

- [ ] **Step 5: Commit**

```bash
cd /tmp/station-plan2
git add frontend/src/api/core.ts frontend/src/api/auth.ts frontend/src/api.ts frontend/src/__tests__/api.station.test.ts
ALLOW_NON_MAIN_BRANCH=1 git commit -m "feat(station): вход станции, постраничная загрузка заказов и освобождение КИЗ в API"
```

---

### Task 9: `/` открывает станцию

**Files:**
- Modify: `frontend/src/workspace/surface.ts` (`AppSurface` = `"station" | "admin"`)
- Modify: `frontend/src/__tests__/surface.test.ts`
- Create: `frontend/src/station/entry/stationEntryMachine.ts`
- Create: `frontend/src/station/entry/StationEntry.tsx`
- Create: `frontend/src/station/StationApp.tsx` (заглушка до плана 3)
- Create: `frontend/src/station/queue/stationLock.ts`
- Modify: `frontend/src/App.tsx` (`/` и всё вне `/admin` это `StationEntry`, форма входа только у `/admin`)
- Modify: `frontend/src/test/server.ts`, `frontend/src/__tests__/server.ts` (вход станции по умолчанию отвечает 403
  `station_network_denied`, как вне сети склада)
- Modify: `frontend/src/__tests__/App.a11y.test.tsx`, `frontend/src/__tests__/App.characterization.test.tsx`
- Modify: `frontend/e2e/synthetic-api.ts`, `frontend/e2e/synthetic-smoke.spec.ts`, `frontend/e2e/design-capture.spec.ts`,
  `frontend/e2e/offline-queue-store.spec.ts`, `frontend/e2e/performance.spec.ts`
- Test: `frontend/src/station/__tests__/stationEntryMachine.test.ts`, `frontend/src/station/__tests__/StationEntry.test.tsx`,
  `frontend/src/station/__tests__/stationLock.test.ts`, `frontend/src/station/__tests__/webLocks.ts`

**Interfaces:**
- Consumes: `getAuthSession`, `stationLogin`, `ApiRequestError.retryAfterSeconds` (Task 8)
- Produces: `resolveStationEntry(deps)` и `runStationEntry(deps, publish, signal, sleep?)`, состояния
  `checking | ready{session, csrfToken} | redirect-admin | offline{retryInMs} | duplicate-tab`;
  `STATION_RETRY_MIN_MS = 30_000`; `acquireStationLock(locks?) → Promise<boolean>`, замок `taksklad-station`
  на всю жизнь страницы; `StationApp({ session, config })`, в которую план 3 поставит окно

Таблица решений входа (из спецификации, раздел 2, и замечания финального ревью плана 1):

| что видит `/` | состояние |
|---|---|
| сессия станции | `ready` |
| сессия другой роли | `redirect-admin`, вход станции не вызывается, чужая cookie не заменяется |
| сессии нет, вход станции 200 с ролью `station` | `ready` |
| сессии нет, 403 `station_network_denied` | `redirect-admin` (не сеть склада) |
| сеть, 5xx, 429, 200 не станции | `offline`, повтор не раньше 30 с (или `Retry-After`, если он длиннее) |
| `ready`, но замок держит другое окно | `duplicate-tab`, это окно ничего не шлёт |

Вне сети склада `/` уводит на `/admin` с формой «Вход в панель управления»; заголовок старого входа
«Вход в складскую web-панель» и экран «Нет доступа к складской web-панели» уходят вместе со старым `/`
(спецификация, раздел 1); файлы `OperatorWorkspace` остаются в репозитории, но не монтируются

На `/admin` экран «Нет доступа к панели управления» даёт ссылку «Открыть складская web-панель» только сессии
станции: любую другую роль `/` сразу вернул бы обратно, и ссылка водила бы по кругу (найдено прототипом);
остальным тот же экран с прежним текстом «…Обратитесь к администратору склада.», новых текстов нет

Очередь станции открывает ту же базу IndexedDB, что старый операторский экран; события, которые он мог
не отправить, станция отправит своим полным проходом, backend принимает их идемпотентно (409 на уже принятый код)

Имя `stationEntryMachine.ts`, а не `stationEntry.ts`: на macOS файловая система не различает регистр,
и `stationEntry.ts` рядом с `StationEntry.tsx` даёт ошибку TypeScript TS1261

- [ ] **Step 1: Write the failing tests**

`frontend/src/station/__tests__/webLocks.ts` (подделка `navigator.locks` для тестов):

```ts
/** The part of Web Locks the station relies on: ifAvailable, held while the callback's promise is pending. */
export function fakeLocks() {
  const held = new Set<string>();
  const requests: Array<{ name: string; options: unknown }> = [];
  const locks = {
    async request(name: string, options: LockOptions, callback: (lock: Lock | null) => unknown) {
      requests.push({ name, options });
      if (held.has(name)) return callback(null);
      held.add(name);
      try {
        return await callback({ name, mode: "exclusive" } as Lock);
      } finally {
        held.delete(name);
      }
    },
  } as unknown as Pick<LockManager, "request">;
  return { locks, held, requests };
}
```

`frontend/src/station/__tests__/stationLock.test.ts`:

```ts
import { describe, expect, it } from "vitest";

import { STATION_LOCK_NAME, acquireStationLock } from "../queue/stationLock";
import { fakeLocks } from "./webLocks";

describe("acquireStationLock", () => {
  it("takes the lock when it is free and holds it for the life of the page", async () => {
    const { locks, held, requests } = fakeLocks();

    await expect(acquireStationLock(locks)).resolves.toBe(true);

    expect(requests).toEqual([{ name: STATION_LOCK_NAME, options: { ifAvailable: true } }]);
    expect(held.has(STATION_LOCK_NAME)).toBe(true);
  });

  it("reports a second window as not owning the station", async () => {
    const { locks } = fakeLocks();

    await expect(acquireStationLock(locks)).resolves.toBe(true);
    await expect(acquireStationLock(locks)).resolves.toBe(false);
  });

  it("treats a browser without Web Locks as available", async () => {
    await expect(acquireStationLock(undefined)).resolves.toBe(true);
    await expect(acquireStationLock({} as Pick<LockManager, "request">)).resolves.toBe(true);
  });

  it("treats a refused lock request as available instead of blocking the station", async () => {
    const refusing = { request: () => Promise.reject(new DOMException("denied", "SecurityError")) };

    await expect(acquireStationLock(refusing as unknown as Pick<LockManager, "request">)).resolves.toBe(true);
  });

  it("uses navigator.locks by default and counts jsdom, which has none, as available", async () => {
    expect("locks" in navigator).toBe(false);

    await expect(acquireStationLock()).resolves.toBe(true);
  });
});
```

`frontend/src/station/__tests__/stationEntryMachine.test.ts`:

```ts
import { afterEach, describe, expect, it, vi } from "vitest";

import type { AuthSession } from "../../api/auth";
import { ApiRequestError } from "../../api/core";
import { anonymousSession, authenticatedSession } from "../../__tests__/fixtures";
import {
  STATION_NETWORK_DENIED_CODE,
  STATION_RETRY_MIN_MS,
  resolveStationEntry,
  retryDelayMs,
  runStationEntry,
  type StationEntryDeps,
  type StationEntryState,
} from "../entry/stationEntryMachine";

const stationSession: AuthSession = {
  authenticated: true,
  login: "warehouse-station",
  role: "station",
  permissions: ["warehouse:read", "warehouse:write", "reports:read"],
  expires_at: "2030-01-01T00:00:00Z",
  csrf_token: "station-csrf",
};

function deps(overrides: Partial<StationEntryDeps> = {}): StationEntryDeps {
  return {
    getSession: vi.fn(async () => anonymousSession),
    login: vi.fn(async () => stationSession),
    acquireLock: vi.fn(async () => true),
    ...overrides,
  };
}

function apiError(status: number, code = "", retryAfterSeconds = 0) {
  return new ApiRequestError(status, "", "", code, retryAfterSeconds);
}

describe("resolveStationEntry", () => {
  it("opens the station when the session already belongs to it, without signing in", async () => {
    const d = deps({ getSession: vi.fn(async () => stationSession) });

    await expect(resolveStationEntry(d)).resolves.toEqual({
      kind: "ready",
      session: stationSession,
      csrfToken: "station-csrf",
    });
    expect(d.login).not.toHaveBeenCalled();
  });

  it.each(["admin", "operator", "warehouse", ""])("sends role %j to /admin and never replaces its cookie", async (role) => {
    const d = deps({ getSession: vi.fn(async () => ({ ...authenticatedSession, role })) });

    await expect(resolveStationEntry(d)).resolves.toEqual({ kind: "redirect-admin" });
    expect(d.login).not.toHaveBeenCalled();
  });

  it("signs in when there is no session and opens the station on 200", async () => {
    const d = deps();

    await expect(resolveStationEntry(d)).resolves.toEqual({
      kind: "ready",
      session: stationSession,
      csrfToken: "station-csrf",
    });
    expect(d.login).toHaveBeenCalledTimes(1);
  });

  it("sends the browser to /admin when the network is not the warehouse", async () => {
    const d = deps({ login: vi.fn(async () => { throw apiError(403, STATION_NETWORK_DENIED_CODE); }) });

    await expect(resolveStationEntry(d)).resolves.toEqual({ kind: "redirect-admin" });
  });

  it.each([
    ["403 with another code", apiError(403, "origin_denied")],
    ["500", apiError(500)],
    ["503 station user unavailable", apiError(503)],
    ["network error", new TypeError("Failed to fetch")],
    ["429 without Retry-After", apiError(429)],
  ])("goes offline and retries in 30 s on %s", async (_name, failure) => {
    const d = deps({ login: vi.fn(async () => { throw failure; }) });

    await expect(resolveStationEntry(d)).resolves.toEqual({ kind: "offline", retryInMs: STATION_RETRY_MIN_MS });
  });

  it("honours a longer Retry-After on 429 and ignores a shorter one", async () => {
    const longer = deps({ login: vi.fn(async () => { throw apiError(429, "", 120); }) });
    const shorter = deps({ login: vi.fn(async () => { throw apiError(429, "", 5); }) });

    await expect(resolveStationEntry(longer)).resolves.toEqual({ kind: "offline", retryInMs: 120_000 });
    await expect(resolveStationEntry(shorter)).resolves.toEqual({ kind: "offline", retryInMs: 30_000 });
  });

  it("goes offline when the session check itself fails, without signing in", async () => {
    const d = deps({ getSession: vi.fn(async () => { throw new TypeError("Failed to fetch"); }) });

    await expect(resolveStationEntry(d)).resolves.toEqual({ kind: "offline", retryInMs: 30_000 });
    expect(d.login).not.toHaveBeenCalled();
  });

  it("does not build a screen on a 200 that is not a station session", async () => {
    const d = deps({ login: vi.fn(async () => ({ ...stationSession, role: "admin" })) });

    await expect(resolveStationEntry(d)).resolves.toEqual({ kind: "offline", retryInMs: 30_000 });
  });
});

describe("retryDelayMs", () => {
  it("only lets Retry-After lengthen a 429", () => {
    expect(retryDelayMs(apiError(503, "", 600))).toBe(30_000);
    expect(retryDelayMs(apiError(429, "", 600))).toBe(600_000);
    expect(retryDelayMs(new Error("x"))).toBe(30_000);
  });
});

describe("runStationEntry", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  function recorder() {
    const states: StationEntryState[] = [];
    return { states, publish: (state: StationEntryState) => states.push(state) };
  }

  it("publishes checking, then ready, and takes the station lock once", async () => {
    const { states, publish } = recorder();
    const d = deps();

    await runStationEntry(d, publish, new AbortController().signal);

    expect(states.map((state) => state.kind)).toEqual(["checking", "ready"]);
    expect(d.acquireLock).toHaveBeenCalledTimes(1);
  });

  it("shows duplicate-tab and never reaches ready when another window holds the lock", async () => {
    const { states, publish } = recorder();
    const d = deps({ acquireLock: vi.fn(async () => false) });

    await runStationEntry(d, publish, new AbortController().signal);

    expect(states.map((state) => state.kind)).toEqual(["checking", "duplicate-tab"]);
  });

  it("does not touch the lock for /admin or while offline", async () => {
    const { publish } = recorder();
    const denied = deps({ login: vi.fn(async () => { throw apiError(403, STATION_NETWORK_DENIED_CODE); }) });

    await runStationEntry(denied, publish, new AbortController().signal);

    expect(denied.acquireLock).not.toHaveBeenCalled();
  });

  it("waits out the retry delay while offline and then opens the station", async () => {
    const { states, publish } = recorder();
    const sleeps: number[] = [];
    const login = vi.fn<StationEntryDeps["login"]>()
      .mockRejectedValueOnce(apiError(503))
      .mockRejectedValueOnce(apiError(429, "", 90))
      .mockResolvedValueOnce(stationSession);

    await runStationEntry(
      deps({ login }),
      publish,
      new AbortController().signal,
      async (ms) => { sleeps.push(ms); },
    );

    expect(states.map((state) => state.kind)).toEqual(["checking", "offline", "offline", "ready"]);
    expect(sleeps).toEqual([30_000, 90_000]);
    expect(login).toHaveBeenCalledTimes(3);
  });

  it("really sleeps 30 s between attempts and stops when the window goes away", async () => {
    vi.useFakeTimers();
    const { states, publish } = recorder();
    const controller = new AbortController();
    const login = vi.fn(async () => { throw apiError(503); });

    const running = runStationEntry(deps({ login }), publish, controller.signal);
    await vi.advanceTimersByTimeAsync(0);
    expect(login).toHaveBeenCalledTimes(1);

    await vi.advanceTimersByTimeAsync(29_999);
    expect(login).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(1);
    expect(login).toHaveBeenCalledTimes(2);

    controller.abort();
    await running;
    await vi.advanceTimersByTimeAsync(120_000);

    expect(login).toHaveBeenCalledTimes(2);
    expect(states.at(-1)?.kind).toBe("offline");
  });
});
```

`frontend/src/station/__tests__/StationEntry.test.tsx`:

```tsx
import { render, screen, waitFor } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { anonymousSession, authenticatedSession } from "../../__tests__/fixtures";
import type { AuthSession } from "../../api/auth";
import { server } from "../../test/server";
import StationEntry from "../entry/StationEntry";
import { acquireStationLock } from "../queue/stationLock";
import { fakeLocks } from "./webLocks";

const stationSession = {
  ...authenticatedSession,
  login: "warehouse-station",
  role: "station",
  permissions: ["warehouse:read", "warehouse:write", "reports:read"],
  csrf_token: "station-csrf",
};

let replace: ReturnType<typeof vi.fn>;
let stationLogins: number;

beforeEach(() => {
  replace = vi.fn();
  vi.stubGlobal("location", { ...window.location, replace });
  stationLogins = 0;
});

afterEach(() => {
  vi.unstubAllGlobals();
  Reflect.deleteProperty(navigator, "locks");
});

function session(body: AuthSession) {
  server.use(http.get("/api/v1/auth/session", () => HttpResponse.json(body)));
}

function stationLogin(respond: () => Response) {
  server.use(http.post("/api/v1/auth/station", () => {
    stationLogins += 1;
    return respond();
  }));
}

describe("StationEntry", () => {
  it("opens the station window for a station session without signing in again", async () => {
    session(stationSession);
    stationLogin(() => HttpResponse.json(stationSession));

    render(<StationEntry />);

    expect(screen.getByTestId("station-checking")).toBeInTheDocument();
    expect(await screen.findByTestId("station-app")).toBeInTheDocument();
    expect(stationLogins).toBe(0);
    expect(replace).not.toHaveBeenCalled();
  });

  it("signs in from the warehouse network when there is no session", async () => {
    session(anonymousSession);
    stationLogin(() => HttpResponse.json(stationSession));

    render(<StationEntry />);

    expect(await screen.findByTestId("station-app")).toBeInTheDocument();
    expect(stationLogins).toBe(1);
  });

  it("leaves an admin session alone and goes to /admin", async () => {
    session(authenticatedSession);
    stationLogin(() => HttpResponse.json(stationSession));

    render(<StationEntry />);

    await waitFor(() => expect(replace).toHaveBeenCalledWith("/admin"));
    expect(stationLogins).toBe(0);
    expect(screen.queryByTestId("station-app")).not.toBeInTheDocument();
  });

  it("goes to /admin outside the warehouse network", async () => {
    session(anonymousSession);
    stationLogin(() => HttpResponse.json({ detail: { code: "station_network_denied" } }, { status: 403 }));

    render(<StationEntry />);

    await waitFor(() => expect(replace).toHaveBeenCalledWith("/admin"));
    expect(replace).toHaveBeenCalledTimes(1);
  });

  it("stays on a neutral offline element when the backend cannot sign the station in", async () => {
    session(anonymousSession);
    stationLogin(() => HttpResponse.json({}, { status: 503 }));

    render(<StationEntry />);

    expect(await screen.findByTestId("station-offline")).toBeInTheDocument();
    expect(replace).not.toHaveBeenCalled();
    expect(stationLogins).toBe(1);
  });

  it("shows duplicate-tab when another window already owns the station", async () => {
    session(stationSession);
    const { locks } = fakeLocks();
    Object.defineProperty(navigator, "locks", { value: locks, configurable: true });
    await expect(acquireStationLock(locks)).resolves.toBe(true);

    render(<StationEntry />);

    expect(await screen.findByTestId("station-duplicate-tab")).toBeInTheDocument();
    expect(screen.queryByTestId("station-app")).not.toBeInTheDocument();
  });
});
```

Тесты поверхности, App и обработчики msw:

```diff
diff --git a/frontend/src/__tests__/App.a11y.test.tsx b/frontend/src/__tests__/App.a11y.test.tsx
index 91477a8..7630a51 100644
--- a/frontend/src/__tests__/App.a11y.test.tsx
+++ b/frontend/src/__tests__/App.a11y.test.tsx
@@ -5,7 +5,7 @@ import { http, HttpResponse } from "msw";
 import { beforeEach, describe, expect, it } from "vitest";
 
 import App from "../App";
-import { anonymousSession } from "./fixtures";
+import { anonymousSession, authenticatedSession } from "./fixtures";
 import { defaultHandlers, server } from "./server";
 
 beforeEach(() => {
@@ -20,8 +20,9 @@ function setPath(pathname: string) {
 describe("focused accessibility characterization", () => {
   it("has no automated axe violations on the login semantic surface", async () => {
     server.use(http.get("/api/v1/auth/session", () => HttpResponse.json(anonymousSession)));
+    setPath("/admin");
     const { container } = render(<App />);
-    await screen.findByRole("heading", { name: "Вход в складскую web-панель" });
+    await screen.findByRole("heading", { name: "Вход в панель управления" });
 
     const results = await axe(container);
     expect(results).toHaveNoViolations();
@@ -32,9 +33,10 @@ describe("focused accessibility characterization", () => {
       http.get("/api/v1/auth/session", () => HttpResponse.json(anonymousSession)),
       http.post("/api/v1/auth/login", () => HttpResponse.json({ message: "Неверные данные" }, { status: 401 })),
     );
+    setPath("/admin");
     const user = userEvent.setup();
     render(<App />);
-    await screen.findByRole("heading", { name: "Вход в складскую web-панель" });
+    await screen.findByRole("heading", { name: "Вход в панель управления" });
 
     const phone = screen.getByRole("textbox", { name: "Телефон" });
     const password = screen.getByLabelText("Пароль");
@@ -94,11 +96,16 @@ describe("focused accessibility characterization", () => {
     expect(await axe(container)).toHaveNoViolations();
   });
 
-  it("has no automated axe violations on the operator workspace shell", async () => {
+  it("has no automated axe violations on the station placeholder", async () => {
+    server.use(http.get("/api/v1/auth/session", () => HttpResponse.json({
+      ...authenticatedSession,
+      login: "warehouse-station",
+      role: "station",
+      permissions: ["warehouse:read", "warehouse:write", "reports:read"],
+    })));
     const { container } = render(<App />);
 
-    await screen.findByRole("heading", { name: "Операторский складской контур" });
-    expect(screen.getByRole("heading", { name: "Склад · PostgreSQL" })).toBeInTheDocument();
+    await screen.findByTestId("station-app");
     expect(await axe(container)).toHaveNoViolations();
   });
 });
diff --git a/frontend/src/__tests__/App.characterization.test.tsx b/frontend/src/__tests__/App.characterization.test.tsx
index 7337459..72f324a 100644
--- a/frontend/src/__tests__/App.characterization.test.tsx
+++ b/frontend/src/__tests__/App.characterization.test.tsx
@@ -1,7 +1,7 @@
 import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
 import userEvent from "@testing-library/user-event";
 import { delay, http, HttpResponse } from "msw";
-import { beforeEach, describe, expect, it, vi } from "vitest";
+import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
 
 import App from "../App";
 import {
@@ -21,10 +21,29 @@ beforeEach(() => {
   window.history.pushState({}, "", "/");
 });
 
+afterEach(() => {
+  vi.unstubAllGlobals();
+});
+
 function setPath(pathname: string) {
   window.history.pushState({}, "", pathname);
 }
 
+const stationSession = {
+  ...authenticatedSession,
+  login: "warehouse-station",
+  role: "station",
+  permissions: ["warehouse:read", "warehouse:write", "reports:read"],
+  csrf_token: "station-csrf",
+};
+
+/** jsdom cannot navigate: swap location for a copy whose replace is observable. */
+function stubLocationReplace() {
+  const replace = vi.fn();
+  vi.stubGlobal("location", { ...window.location, replace });
+  return replace;
+}
+
 async function renderAuthenticatedAdminApp() {
   setPath("/admin");
   const user = userEvent.setup();
@@ -42,11 +61,12 @@ describe("login and session characterization", () => {
         return HttpResponse.json(anonymousSession);
       }),
     );
+    setPath("/admin");
 
     render(<App />);
 
     expect(screen.getByText("Загружаем доступ...")).toBeInTheDocument();
-    expect(await screen.findByRole("heading", { name: "Вход в складскую web-панель" })).toBeInTheDocument();
+    expect(await screen.findByRole("heading", { name: "Вход в панель управления" })).toBeInTheDocument();
     expect(screen.getByLabelText("Телефон")).toHaveAttribute("autocomplete", "username");
     expect(screen.getByLabelText("Пароль")).toHaveAttribute("autocomplete", "current-password");
   });
@@ -59,9 +79,10 @@ describe("login and session characterization", () => {
         { status: 401, statusText: "Unauthorized" },
       )),
     );
+    setPath("/admin");
     const user = userEvent.setup();
     render(<App />);
-    await screen.findByRole("heading", { name: "Вход в складскую web-панель" });
+    await screen.findByRole("heading", { name: "Вход в панель управления" });
 
     await user.type(screen.getByLabelText("Телефон"), "+998 90 111 22 33");
     await user.type(screen.getByLabelText("Пароль"), "synthetic-password");
@@ -108,40 +129,74 @@ describe("login and session characterization", () => {
     expect(await screen.findByText(firstAdminRow.client)).toBeInTheDocument();
   });
 
-  it("chooses the operator workspace on root and keeps the admin link visible", async () => {
-    const user = userEvent.setup();
+  it("opens the station on root for a station session and never mounts the old operator screen", async () => {
+    server.use(http.get("/api/v1/auth/session", () => HttpResponse.json(stationSession)));
+
     render(<App />);
 
-    expect(await screen.findByRole("heading", { name: "Операторский складской контур" })).toBeInTheDocument();
-    expect(screen.getByRole("heading", { name: "Склад · PostgreSQL" })).toBeInTheDocument();
-    expect(screen.getByRole("link", { name: "Панель управления" })).toHaveAttribute("href", "/admin");
+    expect(await screen.findByTestId("station-app")).toBeInTheDocument();
+    expect(screen.queryByRole("heading", { name: "Операторский складской контур" })).not.toBeInTheDocument();
+    expect(screen.queryByRole("heading", { name: "Вход в складскую web-панель" })).not.toBeInTheDocument();
+  });
 
-    await user.click(screen.getByRole("button", { name: "Обновить" }));
-    expect(await screen.findByText("Активные заказы обновлены: 1")).toBeInTheDocument();
+  it("signs the station in on root when the warehouse network allows it", async () => {
+    server.use(
+      http.get("/api/v1/auth/session", () => HttpResponse.json(anonymousSession)),
+      http.post("/api/v1/auth/station", () => HttpResponse.json(stationSession)),
+    );
+
+    render(<App />);
+
+    expect(await screen.findByTestId("station-app")).toBeInTheDocument();
   });
 
-  it("links to /admin when a warehouse user has an imports-only admin section", async () => {
-    server.use(http.get("/api/v1/auth/session", () => HttpResponse.json({
-      ...authenticatedSession,
-      permissions: ["warehouse:read", "imports:read"],
-    })));
+  it("sends an unauthenticated root outside the warehouse network to /admin", async () => {
+    server.use(http.get("/api/v1/auth/session", () => HttpResponse.json(anonymousSession)));
+    const replace = stubLocationReplace();
+
+    render(<App />);
+
+    await waitFor(() => expect(replace).toHaveBeenCalledWith("/admin"));
+    expect(screen.queryByTestId("station-app")).not.toBeInTheDocument();
+    expect(screen.queryByLabelText("Телефон")).not.toBeInTheDocument();
+  });
+
+  it("sends an admin session on root to /admin without signing the station in over it", async () => {
+    let stationLogins = 0;
+    server.use(http.post("/api/v1/auth/station", () => {
+      stationLogins += 1;
+      return HttpResponse.json(stationSession);
+    }));
+    const replace = stubLocationReplace();
+
+    render(<App />);
+
+    await waitFor(() => expect(replace).toHaveBeenCalledWith("/admin"));
+    expect(stationLogins).toBe(0);
+  });
+
+  it("shows the station session on /admin a link back to the station", async () => {
+    server.use(http.get("/api/v1/auth/session", () => HttpResponse.json(stationSession)));
+    setPath("/admin");
 
     render(<App />);
 
-    expect(await screen.findByRole("heading", { name: "Операторский складской контур" })).toBeInTheDocument();
-    expect(screen.getByRole("link", { name: "Панель управления" })).toHaveAttribute("href", "/admin");
+    expect(await screen.findByRole("heading", { name: "Нет доступа к панели управления" })).toBeInTheDocument();
+    expect(screen.getByRole("link", { name: "Открыть складская web-панель" })).toHaveAttribute("href", "/");
   });
 
-  it("shows an access-denied link to /admin when warehouse:read is missing on root", async () => {
+  it("offers no station link to another role without admin sections, since / would send it back", async () => {
     server.use(http.get("/api/v1/auth/session", () => HttpResponse.json({
       ...authenticatedSession,
-      permissions: authenticatedSession.permissions.filter((permission) => permission !== "warehouse:read"),
+      permissions: ["warehouse:read"],
     })));
+    setPath("/admin");
 
     render(<App />);
 
-    expect(await screen.findByRole("heading", { name: "Нет доступа к складской web-панели" })).toBeInTheDocument();
-    expect(screen.getByRole("link", { name: "Открыть панель управления" })).toHaveAttribute("href", "/admin");
+    expect(await screen.findByRole("heading", { name: "Нет доступа к панели управления" })).toBeInTheDocument();
+    expect(screen.queryByRole("link", { name: "Открыть складская web-панель" })).not.toBeInTheDocument();
+    expect(screen.getByText(/Обратитесь к администратору склада/)).toBeInTheDocument();
   });
 });
 
diff --git a/frontend/src/__tests__/server.ts b/frontend/src/__tests__/server.ts
index 6c952d3..7376076 100644
--- a/frontend/src/__tests__/server.ts
+++ b/frontend/src/__tests__/server.ts
@@ -23,6 +23,10 @@ import { server } from "../test/server";
 export const defaultHandlers = [
   http.get("/api/v1/auth/session", () => HttpResponse.json(authenticatedSession)),
   http.post("/api/v1/auth/login", () => HttpResponse.json(authenticatedSession)),
+  http.post("/api/v1/auth/station", () => HttpResponse.json(
+    { detail: { code: "station_network_denied" } },
+    { status: 403, statusText: "Forbidden" },
+  )),
   http.post("/api/v1/auth/logout", () => HttpResponse.json({ ...authenticatedSession, authenticated: false })),
   http.get("/api/v1/orders/active", () => HttpResponse.json([activeOrder])),
   http.get("/api/v1/kiz/availability", ({ request }) => {
diff --git a/frontend/src/__tests__/surface.test.ts b/frontend/src/__tests__/surface.test.ts
index 59fe50a..c8e867c 100644
--- a/frontend/src/__tests__/surface.test.ts
+++ b/frontend/src/__tests__/surface.test.ts
@@ -2,19 +2,32 @@ import { describe, expect, it } from "vitest";
 
 import {
   accessibleAdminTabsForPermissions,
+  alternateSurfacePath,
   hasAdminSurfaceAccess,
   hasOperatorSurfaceAccess,
   resolveAppSurface,
+  surfacePath,
+  surfaceTitle,
 } from "../workspace/surface";
 
 describe("surface helper characterization", () => {
-  it("routes root to operator and /admin paths to admin", () => {
-    expect(resolveAppSurface("/")).toBe("operator");
-    expect(resolveAppSurface("/orders")).toBe("operator");
+  it("routes /admin and below to admin and everything else to the station", () => {
+    expect(resolveAppSurface("/")).toBe("station");
+    expect(resolveAppSurface("/orders")).toBe("station");
+    expect(resolveAppSurface("/administrator")).toBe("station");
     expect(resolveAppSurface("/admin")).toBe("admin");
     expect(resolveAppSurface("/admin/incidents")).toBe("admin");
   });
 
+  it("maps each surface to its own path, the other surface and a title", () => {
+    expect(surfacePath("station")).toBe("/");
+    expect(surfacePath("admin")).toBe("/admin");
+    expect(alternateSurfacePath("station")).toBe("/admin");
+    expect(alternateSurfacePath("admin")).toBe("/");
+    expect(surfaceTitle("station")).toBe("Складская web-панель");
+    expect(surfaceTitle("admin")).toBe("Панель управления");
+  });
+
   it("fails closed for operator access without warehouse:read", () => {
     expect(hasOperatorSurfaceAccess(["warehouse:read"])).toBe(true);
     expect(hasOperatorSurfaceAccess(["warehouse:write"])).toBe(false);
diff --git a/frontend/src/test/server.ts b/frontend/src/test/server.ts
index bd0bda5..304cb72 100644
--- a/frontend/src/test/server.ts
+++ b/frontend/src/test/server.ts
@@ -1,3 +1,10 @@
+import { http, HttpResponse } from "msw";
 import { setupServer } from "msw/node";
 
-export const server = setupServer();
+// Outside the warehouse network the station sign-in is refused; tests that need it open override this.
+export const server = setupServer(
+  http.post("/api/v1/auth/station", () => HttpResponse.json(
+    { detail: { code: "station_network_denied" } },
+    { status: 403, statusText: "Forbidden" },
+  )),
+);
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /tmp/station-plan2/frontend && npx vitest run src/station/__tests__/stationLock.test.ts src/station/__tests__/stationEntry.test.ts src/station/__tests__/StationEntry.test.tsx src/__tests__/surface.test.ts src/__tests__/App.characterization.test.tsx`
Expected: новые файлы FAIL на импорте `../queue/stationLock`, `../entry/stationEntryMachine`, `../entry/StationEntry`;
`surface.test.ts` FAIL на `resolveAppSurface("/")` (ждёт `station`, получает `operator`); характеризация App FAIL
на ветке `/` (ждёт вход станции, видит старую форму входа)

- [ ] **Step 3: Write the implementation**

`frontend/src/station/queue/stationLock.ts`:

```ts
/**
 * One station per browser: a Web Lock held until the page goes away.
 *
 * The desktop refuses a second copy of itself (`src/taksklad/main.py:194`). The browser equivalent is one window
 * owning the queue and the print queue; a second window must show "already open" and send nothing.
 */

export const STATION_LOCK_NAME = "taksklad-station";

/**
 * True when this window now owns the station, false when another window already does.
 * A browser without Web Locks (or one that refuses the call) cannot tell, so it counts as available.
 */
export async function acquireStationLock(
  locks: Pick<LockManager, "request"> | undefined = typeof navigator === "undefined" ? undefined : navigator.locks,
): Promise<boolean> {
  if (!locks?.request) return true;
  return new Promise<boolean>((resolve) => {
    locks
      .request(STATION_LOCK_NAME, { ifAvailable: true }, (lock) => {
        resolve(lock !== null);
        // The lock lasts as long as this promise is pending: for the life of the page.
        return lock ? new Promise<void>(() => undefined) : undefined;
      })
      .catch(() => resolve(true));
  });
}
```

`frontend/src/station/entry/stationEntryMachine.ts`:

```ts
/**
 * What "/" does when it opens: the decision table, with no React in it.
 *
 *   session of the station          -> ready
 *   session of anyone else          -> redirect-admin (never sign in over an admin cookie)
 *   no session, station login ok    -> ready
 *   no session, 403 network denied  -> redirect-admin (not the warehouse network)
 *   no session, network error / 5xx / 429 -> offline, retry in not less than 30 s
 *   ready, but another window holds the station lock -> duplicate-tab
 */

import type { AuthSession } from "../../api/auth";
import { ApiRequestError } from "../../api/core";

export const STATION_NETWORK_DENIED_CODE = "station_network_denied";

/** The station signs in again no more often than this (spec section 2). */
export const STATION_RETRY_MIN_MS = 30_000;

export type StationEntryState =
  | { kind: "checking" }
  | { kind: "ready"; session: AuthSession; csrfToken: string }
  | { kind: "redirect-admin" }
  | { kind: "offline"; retryInMs: number }
  | { kind: "duplicate-tab" };

export type StationEntryDeps = {
  getSession(): Promise<AuthSession>;
  login(): Promise<AuthSession>;
  /** True when this window now owns the station, false when another window does. */
  acquireLock(): Promise<boolean>;
};

function isStationSession(session: AuthSession): boolean {
  return session.authenticated && session.role === "station";
}

function ready(session: AuthSession): StationEntryState {
  return { kind: "ready", session, csrfToken: session.csrf_token || "" };
}

/** 30 s for any failure, the server's Retry-After when it asks for longer (429). */
export function retryDelayMs(error: unknown): number {
  const asked = error instanceof ApiRequestError && error.status === 429 ? error.retryAfterSeconds * 1000 : 0;
  return Math.max(STATION_RETRY_MIN_MS, asked);
}

function offline(error: unknown): StationEntryState {
  return { kind: "offline", retryInMs: retryDelayMs(error) };
}

function afterLoginFailure(error: unknown): StationEntryState {
  if (error instanceof ApiRequestError && error.status === 403 && error.code === STATION_NETWORK_DENIED_CODE) {
    return { kind: "redirect-admin" };
  }
  return offline(error);
}

/** One pass of the table. Never throws: every failure becomes a state. */
export async function resolveStationEntry(deps: StationEntryDeps): Promise<StationEntryState> {
  let session: AuthSession;
  try {
    session = await deps.getSession();
  } catch (error) {
    return offline(error);
  }
  if (session.authenticated) {
    return isStationSession(session) ? ready(session) : { kind: "redirect-admin" };
  }

  try {
    const signedIn = await deps.login();
    // A 200 that is not a station session is not something to build a screen on.
    return isStationSession(signedIn) ? ready(signedIn) : offline(null);
  } catch (error) {
    return afterLoginFailure(error);
  }
}

/**
 * Runs the table until it settles, publishing every state it passes through.
 * `offline` waits out `retryInMs` and tries again, like the desktop started without a network.
 */
export async function runStationEntry(
  deps: StationEntryDeps,
  publish: (state: StationEntryState) => void,
  signal: AbortSignal,
  sleep: (ms: number, signal: AbortSignal) => Promise<void> = abortableSleep,
): Promise<void> {
  publish({ kind: "checking" });
  while (!signal.aborted) {
    const state = await resolveStationEntry(deps);
    if (signal.aborted) return;

    if (state.kind === "ready" && !(await deps.acquireLock())) {
      publish({ kind: "duplicate-tab" });
      return;
    }
    publish(state);
    if (state.kind !== "offline") return;
    await sleep(state.retryInMs, signal);
  }
}

function abortableSleep(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    const timer = setTimeout(done, ms);
    signal.addEventListener("abort", done, { once: true });
    function done() {
      clearTimeout(timer);
      signal.removeEventListener("abort", done);
      resolve();
    }
  });
}
```

`frontend/src/station/StationApp.tsx`:

```tsx
import type { AuthSession } from "../api/auth";
import type { ApiConfig } from "../api/core";

export type StationAppProps = {
  session: AuthSession;
  config: ApiConfig;
};

/** Placeholder: the real station window arrives in a later plan. */
export default function StationApp(_props: StationAppProps) {
  return <div data-testid="station-app" />;
}
```

`frontend/src/station/entry/StationEntry.tsx`:

```tsx
import { useEffect, useMemo, useState } from "react";

import { getAuthSession, stationLogin } from "../../api/auth";
import { defaultApiUrl, type ApiConfig } from "../../api/core";
import { acquireStationLock } from "../queue/stationLock";
import StationApp from "../StationApp";
import { runStationEntry, type StationEntryState } from "./stationEntryMachine";

/** What "/" shows. Status elements stay neutral until the station window gets its desktop status line. */
export default function StationEntry() {
  const [state, setState] = useState<StationEntryState>({ kind: "checking" });
  const csrfToken = state.kind === "ready" ? state.csrfToken : "";
  const stationConfig = useMemo<ApiConfig>(() => ({ apiUrl: defaultApiUrl(), token: "", csrfToken }), [csrfToken]);

  useEffect(() => {
    const controller = new AbortController();
    const config: ApiConfig = { apiUrl: defaultApiUrl(), token: "", csrfToken: "" };
    void runStationEntry(
      {
        getSession: () => getAuthSession(config, controller.signal),
        login: () => stationLogin(config),
        acquireLock: () => acquireStationLock(),
      },
      setState,
      controller.signal,
    );
    return () => controller.abort();
  }, []);

  useEffect(() => {
    if (state.kind === "redirect-admin") window.location.replace("/admin");
  }, [state.kind]);

  switch (state.kind) {
    case "ready":
      return <StationApp session={state.session} config={stationConfig} />;
    case "offline":
      return <div data-testid="station-offline" />;
    case "duplicate-tab":
      return <div data-testid="station-duplicate-tab" />;
    case "redirect-admin":
      return null;
    case "checking":
      return <div data-testid="station-checking" />;
  }
}
```

`surface.ts` и `App.tsx`:

```diff
diff --git a/frontend/src/App.tsx b/frontend/src/App.tsx
index 0b82135..357e0f6 100644
--- a/frontend/src/App.tsx
+++ b/frontend/src/App.tsx
@@ -22,13 +22,26 @@ import "@fontsource/ibm-plex-mono/600.css";
 import "./styles.css";
 
 const AdminWorkspace = lazy(() => import("./workspace/AdminWorkspace"));
-const OperatorWorkspace = lazy(() => import("./workspace/OperatorWorkspace"));
+// The whole station is this one chunk: its entry statically imports everything the window needs.
+const StationEntry = lazy(() => import("./station/entry/StationEntry"));
 
 function initialConfig(): ApiConfig {
   return { apiUrl: defaultApiUrl(), token: "", csrfToken: "" };
 }
 
 function App() {
+  // "/" and everything outside /admin is the station, which signs itself in; /admin keeps the login form.
+  if (resolveAppSurface(window.location.pathname) === "station") {
+    return (
+      <Suspense fallback={null}>
+        <StationEntry />
+      </Suspense>
+    );
+  }
+  return <AdminApp />;
+}
+
+function AdminApp() {
   const [config, setConfig] = useState<ApiConfig>(initialConfig);
   const [authChecked, setAuthChecked] = useState(false);
   const [session, setSession] = useState<AuthSession | null>(null);
@@ -36,7 +49,6 @@ function App() {
   const [loginPassword, setLoginPassword] = useState("");
   const [loginLoading, setLoginLoading] = useState(false);
   const [loginError, setLoginError] = useState("");
-  const surface = resolveAppSurface(window.location.pathname);
 
   useEffect(() => {
     const controller = new AbortController();
@@ -107,7 +119,7 @@ function App() {
   if (!session) {
     return (
       <LoginScreen
-        surface={surface}
+        surface="admin"
         phone={loginPhone}
         password={loginPassword}
         error={loginError}
@@ -119,30 +131,17 @@ function App() {
     );
   }
 
-  const canUseOperatorSurface = hasOperatorSurfaceAccess(session.permissions);
+  // "/" lets in only the station role; anyone else would be sent straight back here.
+  const canUseStationSurface = session.role === "station" && hasOperatorSurfaceAccess(session.permissions);
   const canUseAdminSurface = hasAdminSurfaceAccess(session.permissions);
 
-  if (surface === "operator" && !canUseOperatorSurface) {
+  if (!canUseAdminSurface) {
     return (
       <AccessDeniedScreen
-        surface={surface}
-        canUseAlternateSurface={canUseAdminSurface}
-        title="Нет доступа к складской web-панели"
-        message={canUseAdminSurface
-          ? "Для операционного контура нужно право warehouse:read. Откройте административный контур."
-          : "Для операционного контура нужно право warehouse:read. Обратитесь к администратору склада."}
-        onLogout={() => logout(config)}
-      />
-    );
-  }
-
-  if (surface === "admin" && !canUseAdminSurface) {
-    return (
-      <AccessDeniedScreen
-        surface={surface}
-        canUseAlternateSurface={canUseOperatorSurface}
+        surface="admin"
+        canUseAlternateSurface={canUseStationSurface}
         title="Нет доступа к панели управления"
-        message={canUseOperatorSurface
+        message={canUseStationSurface
           ? "Для административного контура нужны права admin:read или доступный admin-раздел. Откройте складской контур."
           : "Для административного контура нужны права admin:read или доступный admin-раздел. Обратитесь к администратору склада."}
         onLogout={() => logout(config)}
@@ -152,25 +151,14 @@ function App() {
 
   return (
     <Suspense fallback={<LoadingGate />}>
-      {surface === "admin" ? (
-        <AdminWorkspace
-          config={config}
-          authUser={session.login}
-          authRole={session.role}
-          authPermissions={session.permissions}
-          onSessionExpired={expireSession}
-          onLogout={logout}
-        />
-      ) : (
-        <OperatorWorkspace
-          config={config}
-          authUser={session.login}
-          authRole={session.role}
-          authPermissions={session.permissions}
-          onSessionExpired={expireSession}
-          onLogout={logout}
-        />
-      )}
+      <AdminWorkspace
+        config={config}
+        authUser={session.login}
+        authRole={session.role}
+        authPermissions={session.permissions}
+        onSessionExpired={expireSession}
+        onLogout={logout}
+      />
     </Suspense>
   );
 }
@@ -199,7 +187,7 @@ function AccessDeniedScreen({
   onLogout: () => void;
 }) {
   const fallbackPath = alternateSurfacePath(surface);
-  const fallbackTitle = surfaceTitle(surface === "admin" ? "operator" : "admin");
+  const fallbackTitle = surfaceTitle(surface === "admin" ? "station" : "admin");
 
   return (
     <main className="login-shell">
diff --git a/frontend/src/workspace/surface.ts b/frontend/src/workspace/surface.ts
index 3327697..dc99322 100644
--- a/frontend/src/workspace/surface.ts
+++ b/frontend/src/workspace/surface.ts
@@ -1,4 +1,4 @@
-export type AppSurface = "operator" | "admin";
+export type AppSurface = "station" | "admin";
 export type AdminWorkspaceTab =
   | "table"
   | "calendar"
@@ -11,7 +11,7 @@ export type AdminWorkspaceTab =
   | "activity";
 
 export function resolveAppSurface(pathname: string): AppSurface {
-  return pathname === "/admin" || pathname.startsWith("/admin/") ? "admin" : "operator";
+  return pathname === "/admin" || pathname.startsWith("/admin/") ? "admin" : "station";
 }
 
 export function hasOperatorSurfaceAccess(permissions: string[]): boolean {
@@ -38,7 +38,7 @@ export function surfacePath(surface: AppSurface): string {
 }
 
 export function alternateSurfacePath(surface: AppSurface): string {
-  return surfacePath(surface === "admin" ? "operator" : "admin");
+  return surfacePath(surface === "admin" ? "station" : "admin");
 }
 
 export function surfaceTitle(surface: AppSurface): string {
```

Синтетический API и сценарии e2e (сценарий сканера старого `/` помечен `test.fixme` и вернётся в плане 3
уже для окна станции):

```diff
diff --git a/frontend/e2e/design-capture.spec.ts b/frontend/e2e/design-capture.spec.ts
index e9fe0eb..9c5e41d 100644
--- a/frontend/e2e/design-capture.spec.ts
+++ b/frontend/e2e/design-capture.spec.ts
@@ -20,12 +20,14 @@ test.use({ viewport: WIDE });
 
 test("логин", async ({ page }) => {
   await installSyntheticApi(page, { authenticated: false });
-  await page.goto("/");
-  await page.getByRole("heading", { name: "Вход в складскую web-панель" }).waitFor();
+  await page.goto("/admin");
+  await page.getByRole("heading", { name: "Вход в панель управления" }).waitFor();
   await shot(page, "01-login");
 });
 
-test("операторский контур и сканер КИЗ", async ({ page }) => {
+// "/" is the station now and the old operator screen is no longer mounted: this capture returns
+// with the station window in a later plan.
+test.fixme("операторский контур и сканер КИЗ", async ({ page }) => {
   const api = await installSyntheticApi(page);
   page.on("dialog", (d) => d.accept());
   await page.goto("/");
diff --git a/frontend/e2e/offline-queue-store.spec.ts b/frontend/e2e/offline-queue-store.spec.ts
index d5e806b..3d3bf71 100644
--- a/frontend/e2e/offline-queue-store.spec.ts
+++ b/frontend/e2e/offline-queue-store.spec.ts
@@ -57,7 +57,8 @@ async function runInQueue<T>(page: Page, dbName: string, script: QueueScript): P
 
 test.beforeEach(async ({ page }) => {
   await installSyntheticApi(page);
-  await page.goto("/");
+  // Any same-origin page that stays put will do; "/" now redirects an admin session to /admin.
+  await page.goto("/admin");
 });
 
 test("очередь переживает перезагрузку вкладки", async ({ page }) => {
@@ -214,7 +215,7 @@ test("две вкладки одного origin видят одну очеред
 
   const second = await context.newPage();
   await installSyntheticApi(second);
-  await second.goto("/");
+  await second.goto("/admin");
 
   const seenBySecondTab = await runInQueue<string[]>(second, dbName, `
     return (await store.listPending()).map((item) => item.code);
diff --git a/frontend/e2e/performance.spec.ts b/frontend/e2e/performance.spec.ts
index 8c766cc..7ebda6c 100644
--- a/frontend/e2e/performance.spec.ts
+++ b/frontend/e2e/performance.spec.ts
@@ -350,18 +350,10 @@ test("@performance keyboard-only login, navigation, selection, action, dropdown
   await page.keyboard.press("Space");
   await expect(orderSelector).toBeChecked();
 
-  await page.goto("/", { waitUntil: "networkidle" });
-  await expect(page.getByRole("heading", { name: "Склад · PostgreSQL" })).toBeVisible();
-  await expect(page.getByText("Smartup ID: 261000001")).toBeVisible();
-  await expect(page.getByRole("textbox", { name: "КИЗ" })).toBeVisible();
-  const returnLookup = page.getByPlaceholder("WH-R-...");
-  await focusByTab(returnLookup, "return lookup");
-  await page.keyboard.type("WH-R-SYNTHETIC");
-
   const logout = page.getByRole("button", { name: "Выйти" });
   await focusByTab(logout, "logout");
   await page.keyboard.press("Enter");
-  await expect(page.getByRole("heading", { name: "Вход в складскую web-панель" })).toBeVisible();
+  await expect(page.getByRole("heading", { name: "Вход в панель управления" })).toBeVisible();
 
   keyboardEvidence = {
     pass: matrix.every((entry) => entry.pass)
diff --git a/frontend/e2e/synthetic-api.ts b/frontend/e2e/synthetic-api.ts
index 8821046..7e8861f 100644
--- a/frontend/e2e/synthetic-api.ts
+++ b/frontend/e2e/synthetic-api.ts
@@ -466,6 +466,10 @@ export async function installSyntheticApi(page: Page, options: SyntheticApiOptio
       state.loggedIn = true;
       return json(route, syntheticUser);
     }
+    // The synthetic browser is never on the warehouse network, so the station sign-in is always refused.
+    if (path === "/api/v1/auth/station") {
+      return json(route, { detail: { code: "station_network_denied" } }, 403);
+    }
     if (path === "/api/v1/auth/logout") {
       state.loggedIn = false;
       return json(route, { ...syntheticUser, authenticated: false });
diff --git a/frontend/e2e/synthetic-smoke.spec.ts b/frontend/e2e/synthetic-smoke.spec.ts
index 0b00c57..af68e39 100644
--- a/frontend/e2e/synthetic-smoke.spec.ts
+++ b/frontend/e2e/synthetic-smoke.spec.ts
@@ -50,21 +50,37 @@ async function openWarehouseSurface(page: Page) {
 
 test("@smoke login and session use only a synthetic user", async ({ page }) => {
   const api = await installSyntheticApi(page, { authenticated: false });
-  await page.goto("/");
+  await page.goto("/admin");
 
-  await expect(page.getByRole("heading", { name: "Вход в складскую web-панель" })).toBeVisible();
+  await expect(page.getByRole("heading", { name: "Вход в панель управления" })).toBeVisible();
   await page.locator('input[inputmode="tel"]').fill("+998 90 000 00 01");
   await page.locator('input[type="password"]').fill("synthetic-password");
   await page.getByRole("button", { name: "Войти" }).click();
 
-  await expect(page.getByRole("heading", { name: "Склад · PostgreSQL" })).toBeVisible();
+  await expect(page.getByRole("heading", { name: "Позиции заказов" })).toBeVisible();
   await expect(page.getByText("Альфа Тест").first()).toBeVisible();
   expect(api.requests).toContain("POST /api/v1/auth/login");
   await page.getByRole("button", { name: "Выйти" }).click();
-  await expect(page.getByRole("heading", { name: "Вход в складскую web-панель" })).toBeVisible();
+  await expect(page.getByRole("heading", { name: "Вход в панель управления" })).toBeVisible();
+});
+
+test("@smoke root outside the warehouse network lands on the admin login", async ({ page, browserConsole }) => {
+  const api = await installSyntheticApi(page, { authenticated: false });
+  await page.goto("/");
+
+  await expect(page).toHaveURL(/\/admin$/);
+  await expect(page.getByRole("heading", { name: "Вход в панель управления" })).toBeVisible();
+  expect(api.requests).toContain("POST /api/v1/auth/station");
+  expect(api.requests).not.toContain("POST /api/v1/auth/login");
+  // The refused station sign-in is the one expected failed request of this test.
+  const refusal = browserConsole.problems.filter((problem) => problem.includes("403"));
+  expect(refusal).toHaveLength(1);
+  browserConsole.problems.splice(0, browserConsole.problems.length);
 });
 
-test("@smoke warehouse scanner, completion and print stay inside the synthetic API", async ({ page }) => {
+// "/" is the station now and the old operator screen is no longer mounted from it: this scenario
+// returns, rewritten for the station window, in a later plan.
+test.fixme("@smoke warehouse scanner, completion and print stay inside the synthetic API", async ({ page }) => {
   const api = await installSyntheticApi(page);
   await page.addInitScript(() => {
     (window as typeof window & { __printCalls?: number }).__printCalls = 0;
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /tmp/station-plan2/frontend && npx vitest run && npm run typecheck && npm run lint && npm run test:a11y`
Expected: `Test Files  29 passed`, `Tests  629 passed`; typecheck и lint без ошибок;
a11y `Tests  7 passed`

Run: `cd /tmp/station-plan2/frontend && npm run e2e`
Expected: `21 passed`, `4 skipped`; два сценария старого `/` (сканер с печатью и снимок операторского контура) помечены
`test.fixme` и вернутся в плане 3 уже для окна станции

Run: `cd /tmp/station-plan2 && PYTHONPATH=. /Users/anton/Documents/work/TakSklad/.venv/bin/python -m unittest tests.test_frontend_csrf_contract tests.test_web_offline_queue_contract -v`
Expected: `OK` (контракты CSRF и офлайн-очереди фронта не задеты сменой `/`)

- [ ] **Step 5: Commit**

```bash
cd /tmp/station-plan2
git add frontend/src/workspace/surface.ts frontend/src/__tests__/surface.test.ts \
  frontend/src/station/entry/stationEntryMachine.ts frontend/src/station/entry/StationEntry.tsx \
  frontend/src/station/StationApp.tsx frontend/src/station/queue/stationLock.ts frontend/src/App.tsx \
  frontend/src/test/server.ts frontend/src/__tests__/server.ts \
  frontend/src/__tests__/App.a11y.test.tsx frontend/src/__tests__/App.characterization.test.tsx \
  frontend/src/station/__tests__/stationEntryMachine.test.ts frontend/src/station/__tests__/StationEntry.test.tsx \
  frontend/src/station/__tests__/stationLock.test.ts frontend/src/station/__tests__/webLocks.ts \
  frontend/e2e/synthetic-api.ts frontend/e2e/synthetic-smoke.spec.ts frontend/e2e/design-capture.spec.ts \
  frontend/e2e/offline-queue-store.spec.ts frontend/e2e/performance.spec.ts
ALLOW_NON_MAIN_BRANCH=1 git commit -m "feat(station): корень сайта открывает станцию, вход без пароля и одно окно на браузер"
```

---

### Task 10: Очередь сканов с ритмом программы

**Files:**
- Modify: `frontend/src/features/warehouse/offline/replay.ts` (необязательный фильтр прохода)
- Create: `frontend/src/station/queue/stationQueue.ts`
- Test: `frontend/src/__tests__/offlineReplayFilter.test.ts`, `frontend/src/station/__tests__/stationQueue.test.ts`

**Interfaces:**
- Consumes: `createScan`, `completeWarehouseOrder`, `ApiRequestError` (существующие); `normalizeKizCode`;
  `createIndexedDbQueueStore`, `classifyReplayFailure`, `offlineEventKey` из `features/warehouse/offline`
- Produces: `ReplayFilter = { orderItemIds?: Set<string>; orderIds?: Set<string> }`,
  `eventMatchesReplayFilter(event, filter?)`, `replayQueue(store, deps, filter?)` (без фильтра как раньше);
  `createStationQueue({ getConfig, actor, workstationId, relogin, onCycle?, store?, send?, now? })` с методами
  `start()`, `stop()`, `enqueueScan({orderId, orderItemId, code})`, `enqueueComplete(orderId)`, `flushAll()`,
  `flushItem(orderItemId)`, `flushOrder(orderId, orderItemIds)`, `recordLoadedOrders(orders)`,
  `isDelivered(orderItemId, code)`; константы `STATION_SYNC_FIRST_RUN_MS = 13_000`,
  `STATION_SYNC_INTERVAL_MS = 15_000`, `RELOGIN_MIN_INTERVAL_MS = 30_000`

Правила, которые держат тесты: скан сначала пишется в очередь и только потом уходит; «сохранено» показывается
только по `isDelivered` (код был в последней загрузке или проход его отправил, 409 «уже есть» считается
доставкой); проходы не идут параллельно; «Следующая позиция» шлёт только сканы своей позиции, «Завершить»
сначала сканы позиций заказа, потом `order_complete`, как `backend_event_matches_filter` программы
(`src/taksklad/backend_events.py:427`); `order_complete` не уходит, пока у заказа в очереди ждёт скан, даже
отфильтрованный; 401 вызывает повторный вход станции не чаще раза в 30 с, а коды остаются в очереди

- [ ] **Step 1: Write the failing tests**

`frontend/src/__tests__/offlineReplayFilter.test.ts`:

```ts
import { describe, expect, it } from "vitest";

import { ApiRequestError } from "../api/core";
import { createMemoryQueueStore } from "../features/warehouse/offline/queueStore";
import { eventMatchesReplayFilter, replayQueue } from "../features/warehouse/offline/replay";
import type { OfflineEvent } from "../features/warehouse/offline/queueTypes";
import { scanEvent } from "./fixtures";

function completeEvent(orderId: string): OfflineEvent {
  return scanEvent({ type: "order_complete", orderId, orderItemId: "", code: "" });
}

function recorder(failOn: (event: OfflineEvent) => boolean = () => false) {
  const sent: string[] = [];
  return {
    sent,
    deps: {
      async sendScan(event: OfflineEvent) {
        if (failOn(event)) throw new ApiRequestError(503, "Service Unavailable", "");
        sent.push(`scan:${event.orderItemId}`);
      },
      async sendComplete(event: OfflineEvent) {
        sent.push(`complete:${event.orderId}`);
      },
    },
  };
}

async function queueOf(...events: OfflineEvent[]) {
  const store = createMemoryQueueStore();
  for (const event of events) await store.enqueue(event);
  return store;
}

describe("eventMatchesReplayFilter", () => {
  it("matches a scan by its item and a completion by its order, like the desktop", () => {
    const scan = scanEvent({ orderId: "o1", orderItemId: "i1" });
    const complete = completeEvent("o1");

    expect(eventMatchesReplayFilter(scan, { orderItemIds: new Set(["i1"]) })).toBe(true);
    expect(eventMatchesReplayFilter(scan, { orderIds: new Set(["o1"]) })).toBe(false);
    expect(eventMatchesReplayFilter(complete, { orderIds: new Set(["o1"]) })).toBe(true);
    expect(eventMatchesReplayFilter(complete, { orderItemIds: new Set(["i1"]) })).toBe(false);
  });

  it("matches everything without a filter and nothing for empty sets", () => {
    const scan = scanEvent();

    expect(eventMatchesReplayFilter(scan)).toBe(true);
    expect(eventMatchesReplayFilter(scan, {})).toBe(false);
    expect(eventMatchesReplayFilter(scan, { orderItemIds: new Set(), orderIds: new Set() })).toBe(false);
  });
});

describe("replayQueue with a filter", () => {
  it("sends only the chosen item's scans and leaves the rest queued", async () => {
    const store = await queueOf(
      scanEvent({ orderItemId: "i1", code: "A" }),
      scanEvent({ orderItemId: "i2", code: "B" }),
      completeEvent("order-1"),
    );
    const { sent, deps } = recorder();

    const summary = await replayQueue(store, deps, { orderItemIds: new Set(["i1"]) });

    expect(sent).toEqual(["scan:i1"]);
    expect(summary).toEqual({ synced: 1, blocked: 0, failed: 0, remaining: 2 });
  });

  it("sends only completions for orderIds alone, and no scans", async () => {
    const store = await queueOf(scanEvent({ orderId: "o1", orderItemId: "i1" }), completeEvent("o2"));
    const { sent, deps } = recorder();

    await replayQueue(store, deps, { orderIds: new Set(["o2"]) });

    expect(sent).toEqual(["complete:o2"]);
  });

  it("sends scans before the completion of the same order", async () => {
    const store = await queueOf(
      scanEvent({ orderId: "o1", orderItemId: "i1", code: "A" }),
      scanEvent({ orderId: "o1", orderItemId: "i2", code: "B" }),
      completeEvent("o1"),
    );
    const { sent, deps } = recorder();

    await replayQueue(store, deps, { orderItemIds: new Set(["i1", "i2"]), orderIds: new Set(["o1"]) });

    expect(sent).toEqual(["scan:i1", "scan:i2", "complete:o1"]);
  });

  it("holds a completion while a scan of its order is kept out of the pass", async () => {
    const store = await queueOf(
      scanEvent({ orderId: "o1", orderItemId: "i1", code: "A" }),
      scanEvent({ orderId: "o1", orderItemId: "i3", code: "C" }),
      completeEvent("o1"),
    );
    const { sent, deps } = recorder();

    const summary = await replayQueue(store, deps, { orderItemIds: new Set(["i1"]), orderIds: new Set(["o1"]) });

    expect(sent).toEqual(["scan:i1"]);
    expect(summary.remaining).toBe(2);
  });

  it("holds a completion while a scan of its order fails in the pass", async () => {
    const store = await queueOf(scanEvent({ orderId: "o1", orderItemId: "i1" }), completeEvent("o1"));
    const { sent, deps } = recorder((event) => event.orderItemId === "i1");

    await replayQueue(store, deps, { orderItemIds: new Set(["i1"]), orderIds: new Set(["o1"]) });

    expect(sent).toEqual([]);
  });
});
```

`frontend/src/station/__tests__/stationQueue.test.ts`:

```ts
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { activeOrder, orderItem } from "../../__tests__/fixtures";
import type { Order } from "../../api";
import { ApiRequestError } from "../../api/core";
import { createMemoryQueueStore } from "../../features/warehouse/offline/queueStore";
import type { OfflineEvent } from "../../features/warehouse/offline/queueTypes";
import {
  RELOGIN_MIN_INTERVAL_MS,
  STATION_SYNC_FIRST_RUN_MS,
  STATION_SYNC_INTERVAL_MS,
  createStationQueue,
  type CycleOutcome,
  type StationQueueOptions,
} from "../queue/stationQueue";

const CODE_A = "0104006396053947217AAAAAAAAAA";
const CODE_B = "0104006396053947217BBBBBBBBBB";
const CODE_C = "0104006396053947217CCCCCCCCCC";

function http(status: number, code = "") {
  return new ApiRequestError(status, "", "", code);
}

/** A transport that records what reached the "server" and fails on demand. */
function fakeServer(failWith: (event: OfflineEvent, attempt: number) => unknown = () => null) {
  const sent: string[] = [];
  const attempts = new Map<string, number>();
  async function handle(event: OfflineEvent, label: string) {
    const key = `${label}:${event.orderItemId || event.orderId}:${event.code}`;
    const attempt = (attempts.get(key) ?? 0) + 1;
    attempts.set(key, attempt);
    const failure = failWith(event, attempt);
    if (failure) throw failure;
    sent.push(label === "scan" ? `scan:${event.orderItemId}:${event.code}` : `complete:${event.orderId}`);
  }
  return {
    sent,
    send: {
      sendScan: (event: OfflineEvent) => handle(event, "scan"),
      sendComplete: (event: OfflineEvent) => handle(event, "complete"),
    },
  };
}

function build(overrides: Partial<StationQueueOptions> = {}) {
  const store = createMemoryQueueStore();
  const server = fakeServer();
  const relogin = vi.fn(async () => undefined);
  const queue = createStationQueue({
    getConfig: () => ({ apiUrl: "", token: "", csrfToken: "csrf" }),
    actor: "station",
    workstationId: "web-station",
    relogin,
    store,
    send: server.send,
    ...overrides,
  });
  return { queue, store, server, relogin };
}

function orderWithCodes(itemId: string, codes: string[]): Order {
  return { ...activeOrder, items: [orderItem({ id: itemId, scan_codes: codes })] };
}

/** The memory store and the pass are chains of promises; give them time to finish after a timer fires. */
async function settle() {
  for (let turn = 0; turn < 50; turn += 1) await Promise.resolve();
}

describe("cadence", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("writes a scan to the queue without sending it", async () => {
    const { queue, store, server } = build();

    await queue.enqueueScan({ orderId: "order-1", orderItemId: "item-1", code: CODE_A });

    expect(server.sent).toEqual([]);
    expect(await store.listPending()).toHaveLength(1);
  });

  it("runs the first cycle 13 s after start and then every 15 s", async () => {
    const { queue, server } = build();
    await queue.enqueueScan({ orderId: "order-1", orderItemId: "item-1", code: CODE_A });

    queue.start();
    await vi.advanceTimersByTimeAsync(STATION_SYNC_FIRST_RUN_MS - 1);
    expect(server.sent).toEqual([]);
    await vi.advanceTimersByTimeAsync(1);
    await settle();
    expect(server.sent).toEqual([`scan:item-1:${CODE_A}`]);

    await queue.enqueueScan({ orderId: "order-1", orderItemId: "item-1", code: CODE_B });
    await vi.advanceTimersByTimeAsync(STATION_SYNC_INTERVAL_MS - 1);
    expect(server.sent).toHaveLength(1);
    await vi.advanceTimersByTimeAsync(1);
    await settle();
    expect(server.sent).toEqual([`scan:item-1:${CODE_A}`, `scan:item-1:${CODE_B}`]);

    expect([STATION_SYNC_FIRST_RUN_MS, STATION_SYNC_INTERVAL_MS]).toEqual([13_000, 15_000]);
  });

  it("stops cycling after stop()", async () => {
    const { queue, server } = build();
    await queue.enqueueScan({ orderId: "order-1", orderItemId: "item-1", code: CODE_A });

    queue.start();
    queue.stop();
    await vi.advanceTimersByTimeAsync(60_000);

    expect(server.sent).toEqual([]);
  });

  it("plans the next cycle when the previous one ends, and skips a tick while a screen pass runs", async () => {
    let release: () => void = () => undefined;
    const gate = new Promise<void>((resolve) => { release = resolve; });
    const outcomes: CycleOutcome[] = [];
    const server = fakeServer();
    const { queue } = build({
      send: {
        sendScan: async (event) => { await gate; await server.send.sendScan(event); },
        sendComplete: server.send.sendComplete,
      },
      onCycle: (outcome) => outcomes.push(outcome),
    });
    await queue.enqueueScan({ orderId: "order-1", orderItemId: "item-1", code: CODE_A });

    queue.start();
    const screenPass = queue.flushItem("item-1");
    await vi.advanceTimersByTimeAsync(STATION_SYNC_FIRST_RUN_MS);
    expect(outcomes).toHaveLength(0);

    release();
    await screenPass;
    await vi.advanceTimersByTimeAsync(STATION_SYNC_INTERVAL_MS);
    expect(outcomes).toHaveLength(1);
    expect(server.sent).toEqual([`scan:item-1:${CODE_A}`]);
  });

  it("reports each cycle, including a storage failure, and keeps cycling", async () => {
    const outcomes: CycleOutcome[] = [];
    const { queue, store } = build({ onCycle: (outcome) => outcomes.push(outcome) });
    vi.spyOn(store, "listPending").mockRejectedValueOnce(new Error("storage gone"));

    queue.start();
    await vi.advanceTimersByTimeAsync(STATION_SYNC_FIRST_RUN_MS);
    await vi.advanceTimersByTimeAsync(STATION_SYNC_INTERVAL_MS);

    expect(outcomes.map((outcome) => outcome.ok)).toEqual([false, true]);
  });
});

describe("flushItem", () => {
  it("sends only that item's scans and reports what happened", async () => {
    const { queue, store, server } = build();
    await queue.enqueueScan({ orderId: "order-1", orderItemId: "item-1", code: CODE_A });
    await queue.enqueueScan({ orderId: "order-1", orderItemId: "item-2", code: CODE_B });
    await queue.enqueueScan({ orderId: "order-1", orderItemId: "item-1", code: CODE_C });
    await queue.enqueueComplete("order-1");

    const result = await queue.flushItem("item-1");

    expect(result).toEqual({ delivered: [CODE_A, CODE_C], blocked: [], pending: 0, networkFailed: false });
    expect(server.sent).toEqual([`scan:item-1:${CODE_A}`, `scan:item-1:${CODE_C}`]);
    expect((await store.listPending()).map((event) => event.type)).toEqual(["scan", "order_complete"]);
  });

  it("counts what is still queued for the item and flags a failed network", async () => {
    const server = fakeServer((event) => (event.code === CODE_B ? http(503) : null));
    const { queue } = build({ send: server.send });
    await queue.enqueueScan({ orderId: "order-1", orderItemId: "item-1", code: CODE_A });
    await queue.enqueueScan({ orderId: "order-1", orderItemId: "item-1", code: CODE_B });
    await queue.enqueueScan({ orderId: "order-1", orderItemId: "item-2", code: CODE_C });

    const result = await queue.flushItem("item-1");

    expect(result).toEqual({ delivered: [CODE_A], blocked: [], pending: 1, networkFailed: true });
  });

  it("hands back events the server refused for good", async () => {
    const server = fakeServer(() => http(409, "kiz_already_owned"));
    const { queue, store } = build({ send: server.send });
    await queue.enqueueScan({ orderId: "order-1", orderItemId: "item-1", code: CODE_A });

    const result = await queue.flushItem("item-1");

    expect(result.delivered).toEqual([]);
    expect(result.pending).toBe(0);
    expect(result.networkFailed).toBe(false);
    expect(result.blocked).toHaveLength(1);
    expect(result.blocked[0]).toMatchObject({ reasonCode: "kiz_already_owned", event: { code: CODE_A } });
    expect(await store.listBlocked()).toHaveLength(1);
  });

  it("does not report an earlier blocked event as this pass's", async () => {
    let refuse = true;
    const server = fakeServer((event) => (refuse && event.code === CODE_A ? http(409, "kiz_already_owned") : null));
    const { queue } = build({ send: server.send });
    await queue.enqueueScan({ orderId: "order-1", orderItemId: "item-1", code: CODE_A });
    await queue.flushItem("item-1");
    refuse = false;
    await queue.enqueueScan({ orderId: "order-1", orderItemId: "item-1", code: CODE_B });

    const result = await queue.flushItem("item-1");

    expect(result).toEqual({ delivered: [CODE_B], blocked: [], pending: 0, networkFailed: false });
  });
});

describe("flushOrder", () => {
  it("sends the order's scans first and then its completion, leaving other orders alone", async () => {
    const { queue, server } = build();
    await queue.enqueueScan({ orderId: "order-1", orderItemId: "item-1", code: CODE_A });
    await queue.enqueueScan({ orderId: "order-2", orderItemId: "item-9", code: CODE_C });
    await queue.enqueueScan({ orderId: "order-1", orderItemId: "item-2", code: CODE_B });
    await queue.enqueueComplete("order-1");

    const result = await queue.flushOrder("order-1", ["item-1", "item-2"]);

    expect(server.sent).toEqual([`scan:item-1:${CODE_A}`, `scan:item-2:${CODE_B}`, "complete:order-1"]);
    expect(result).toEqual({ delivered: [CODE_A, CODE_B], blocked: [], pending: 0, networkFailed: false });
  });

  it("does not complete the order while one of its scans fails", async () => {
    const server = fakeServer((event) => (event.code === CODE_B ? http(503) : null));
    const { queue } = build({ send: server.send });
    await queue.enqueueScan({ orderId: "order-1", orderItemId: "item-1", code: CODE_A });
    await queue.enqueueScan({ orderId: "order-1", orderItemId: "item-2", code: CODE_B });
    await queue.enqueueComplete("order-1");

    const result = await queue.flushOrder("order-1", ["item-1", "item-2"]);

    expect(server.sent).toEqual([`scan:item-1:${CODE_A}`]);
    expect(result).toEqual({ delivered: [CODE_A], blocked: [], pending: 2, networkFailed: true });
  });

  it("does not complete the order while a scan of an item outside the list still waits", async () => {
    const { queue, server } = build();
    await queue.enqueueScan({ orderId: "order-1", orderItemId: "item-1", code: CODE_A });
    await queue.enqueueScan({ orderId: "order-1", orderItemId: "item-3", code: CODE_C });
    await queue.enqueueComplete("order-1");

    const result = await queue.flushOrder("order-1", ["item-1"]);

    expect(server.sent).toEqual([`scan:item-1:${CODE_A}`]);
    expect(result.pending).toBe(1);
  });
});

describe("delivered state", () => {
  it("is true for codes the last API load listed on the item", () => {
    const { queue } = build();

    queue.recordLoadedOrders([orderWithCodes("item-1", [CODE_A])]);

    expect(queue.isDelivered("item-1", CODE_A)).toBe(true);
    expect(queue.isDelivered("item-1", ` ${CODE_A}\n`)).toBe(true);
    expect(queue.isDelivered("item-1", CODE_B)).toBe(false);
    expect(queue.isDelivered("item-2", CODE_A)).toBe(false);
  });

  it("is never true for a scan that is only queued, and turns true once a pass synced it", async () => {
    const { queue } = build();
    queue.recordLoadedOrders([orderWithCodes("item-1", [])]);

    await queue.enqueueScan({ orderId: "order-1", orderItemId: "item-1", code: CODE_B });
    expect(queue.isDelivered("item-1", CODE_B)).toBe(false);

    await queue.flushItem("item-1");
    expect(queue.isDelivered("item-1", CODE_B)).toBe(true);
  });

  it("stays false when the server did not take the scan", async () => {
    const network = fakeServer(() => http(503));
    const refusal = fakeServer(() => http(409, "kiz_already_owned"));
    const failing = build({ send: network.send });
    const refused = build({ send: refusal.send });

    for (const { queue } of [failing, refused]) {
      await queue.enqueueScan({ orderId: "order-1", orderItemId: "item-1", code: CODE_B });
      await queue.flushItem("item-1");
      expect(queue.isDelivered("item-1", CODE_B)).toBe(false);
    }
  });

  it("counts a 409 duplicate acknowledgement as delivered: the server already holds the code", async () => {
    const server = fakeServer(() => http(409, "scan_duplicate_ack"));
    const { queue } = build({ send: server.send });
    await queue.enqueueScan({ orderId: "order-1", orderItemId: "item-1", code: CODE_B });

    const result = await queue.flushItem("item-1");

    expect(result.delivered).toEqual([CODE_B]);
    expect(queue.isDelivered("item-1", CODE_B)).toBe(true);
  });

  it("is marked by the cycle and the whole-queue pass too", async () => {
    const { queue } = build();
    await queue.enqueueScan({ orderId: "order-1", orderItemId: "item-1", code: CODE_A });
    await queue.enqueueScan({ orderId: "order-1", orderItemId: "item-2", code: CODE_B });

    await queue.flushAll();

    expect(queue.isDelivered("item-1", CODE_A)).toBe(true);
    expect(queue.isDelivered("item-2", CODE_B)).toBe(true);
  });

  it("starts over with every API load", async () => {
    const { queue } = build();
    await queue.enqueueScan({ orderId: "order-1", orderItemId: "item-1", code: CODE_B });
    await queue.flushItem("item-1");

    queue.recordLoadedOrders([orderWithCodes("item-1", [CODE_A])]);

    expect(queue.isDelivered("item-1", CODE_B)).toBe(false);
    expect(queue.isDelivered("item-1", CODE_A)).toBe(true);
  });

  it("withdraws the mark when a code is scanned again, until the server takes it again", async () => {
    const { queue } = build();
    queue.recordLoadedOrders([orderWithCodes("item-1", [CODE_A])]);

    await queue.enqueueScan({ orderId: "order-1", orderItemId: "item-1", code: CODE_A });
    expect(queue.isDelivered("item-1", CODE_A)).toBe(false);

    await queue.flushItem("item-1");
    expect(queue.isDelivered("item-1", CODE_A)).toBe(true);
  });
});

describe("re-login on 401", () => {
  function unauthorizedUntilRelogin() {
    let signedIn = false;
    const server = fakeServer(() => (signedIn ? null : http(401)));
    return { server, signIn: () => { signedIn = true; } };
  }

  it("signs in again and retries the same request once", async () => {
    const { server, signIn } = unauthorizedUntilRelogin();
    const relogin = vi.fn(async () => { signIn(); });
    const { queue } = build({ send: server.send, relogin });
    await queue.enqueueScan({ orderId: "order-1", orderItemId: "item-1", code: CODE_A });

    const result = await queue.flushItem("item-1");

    expect(relogin).toHaveBeenCalledTimes(1);
    expect(result).toEqual({ delivered: [CODE_A], blocked: [], pending: 0, networkFailed: false });
  });

  it("retries only once: a second 401 stays a retryable failure", async () => {
    const server = fakeServer(() => http(401));
    const relogin = vi.fn(async () => undefined);
    const { queue, store } = build({ send: server.send, relogin });
    await queue.enqueueScan({ orderId: "order-1", orderItemId: "item-1", code: CODE_A });

    const result = await queue.flushItem("item-1");

    expect(relogin).toHaveBeenCalledTimes(1);
    expect(result).toEqual({ delivered: [], blocked: [], pending: 1, networkFailed: true });
    expect(await store.listBlocked()).toEqual([]);
  });

  it("asks no more than once per 30 s however many requests answer 401", async () => {
    let clock = 1_000_000;
    const server = fakeServer(() => http(401));
    const relogin = vi.fn(async () => undefined);
    const { queue } = build({ send: server.send, relogin, now: () => clock });
    await queue.enqueueScan({ orderId: "order-1", orderItemId: "item-1", code: CODE_A });
    await queue.enqueueScan({ orderId: "order-1", orderItemId: "item-2", code: CODE_B });

    await queue.flushAll();
    expect(relogin).toHaveBeenCalledTimes(1);

    clock += RELOGIN_MIN_INTERVAL_MS - 1;
    await queue.flushAll();
    expect(relogin).toHaveBeenCalledTimes(1);

    clock += 1;
    await queue.flushAll();
    expect(relogin).toHaveBeenCalledTimes(2);
  });

  it("keeps the scan queued when the re-login itself fails", async () => {
    const server = fakeServer(() => http(401));
    const relogin = vi.fn(async () => { throw http(403, "station_network_denied"); });
    const { queue } = build({ send: server.send, relogin });
    await queue.enqueueScan({ orderId: "order-1", orderItemId: "item-1", code: CODE_A });

    const result = await queue.flushItem("item-1");

    expect(result).toEqual({ delivered: [], blocked: [], pending: 1, networkFailed: true });
  });

  it("does not re-login for other failures", async () => {
    const server = fakeServer(() => http(503));
    const relogin = vi.fn(async () => undefined);
    const { queue } = build({ send: server.send, relogin });
    await queue.enqueueScan({ orderId: "order-1", orderItemId: "item-1", code: CODE_A });

    await queue.flushItem("item-1");

    expect(relogin).not.toHaveBeenCalled();
  });

  it("also covers the order completion", async () => {
    const { server, signIn } = unauthorizedUntilRelogin();
    const relogin = vi.fn(async () => { signIn(); });
    const { queue } = build({ send: server.send, relogin });
    await queue.enqueueComplete("order-1");

    const result = await queue.flushOrder("order-1", []);

    expect(server.sent).toEqual(["complete:order-1"]);
    expect(result.pending).toBe(0);
  });
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /tmp/station-plan2/frontend && npx vitest run src/__tests__/offlineReplayFilter.test.ts src/station/__tests__/stationQueue.test.ts`
Expected: FAIL, в `replay.ts` нет `eventMatchesReplayFilter`, модуля `../queue/stationQueue` нет

- [ ] **Step 3: Write the implementation**

```diff
diff --git a/frontend/src/features/warehouse/offline/replay.ts b/frontend/src/features/warehouse/offline/replay.ts
index 158e55e..98f430b 100644
--- a/frontend/src/features/warehouse/offline/replay.ts
+++ b/frontend/src/features/warehouse/offline/replay.ts
@@ -21,6 +21,9 @@
  * Ordering still holds where it matters: `order_complete` is never sent while
  * the same order still has a scan waiting in the queue, whatever the reason it
  * is waiting.
+ *
+ * An optional filter narrows a pass to part of the queue (the station sends one
+ * position, or one order, at a time). Without it the pass covers everything.
  */
 
 import { classifyReplayFailure } from "./errorPolicy";
@@ -39,6 +42,24 @@ export type ReplaySummary = {
   remaining: number;
 };
 
+/**
+ * Which queued events a pass may send. Same predicate as the desktop
+ * (`backend_event_matches_filter`, `src/taksklad/backend_events.py:427`): a scan
+ * is chosen by its order item, an `order_complete` by its order. A set that is
+ * missing selects nothing of that kind.
+ */
+export type ReplayFilter = {
+  orderItemIds?: Set<string>;
+  orderIds?: Set<string>;
+};
+
+/** No filter means every event; with a filter only the chosen ones. */
+export function eventMatchesReplayFilter(event: OfflineEvent, filter?: ReplayFilter): boolean {
+  if (!filter) return true;
+  if (event.type === "scan") return filter.orderItemIds?.has(event.orderItemId) ?? false;
+  return filter.orderIds?.has(event.orderId) ?? false;
+}
+
 function errorMessage(error: unknown): string {
   return error instanceof Error ? error.message : String(error);
 }
@@ -54,8 +75,11 @@ function blockReason(error: unknown): { code: string; message: string } {
 /** Consecutive retryable failures that mean the backend itself is unreachable. */
 export const MAX_CONSECUTIVE_RETRY_FAILURES = 3;
 
-export async function replayQueue(store: OfflineQueueStore, deps: ReplayDeps): Promise<ReplaySummary> {
-  const pending = await store.listPending();
+export async function replayQueue(
+  store: OfflineQueueStore,
+  deps: ReplayDeps,
+  filter?: ReplayFilter,
+): Promise<ReplaySummary> {
   let synced = 0;
   let blocked = 0;
   let failed = 0;
@@ -66,6 +90,13 @@ export async function replayQueue(store: OfflineQueueStore, deps: ReplayDeps): P
   // scanned block is still waiting to be sent.
   const ordersWithWaitingScans = new Set<string>();
 
+  // A scan the filter keeps out of this pass is waiting too.
+  const pending: OfflineEvent[] = [];
+  for (const event of await store.listPending()) {
+    if (eventMatchesReplayFilter(event, filter)) pending.push(event);
+    else if (event.type === "scan") ordersWithWaitingScans.add(event.orderId);
+  }
+
   for (const event of pending) {
     const key = offlineEventKey(event);
 
```

`frontend/src/station/queue/stationQueue.ts`:

```ts
/**
 * The station's scan queue: the desktop's backend event queue, in the page.
 *
 * A scan is written to the durable queue first and sent later, by a cycle (every 15 s) or on demand
 * (next position, finish order, manual refresh). Nothing is called "saved" before the server took it:
 * `isDelivered` is the only source for that label.
 */

import { completeWarehouseOrder, createScan, type ApiConfig, type Order } from "../../api";
import { ApiRequestError } from "../../api/core";
import { classifyReplayFailure } from "../../features/warehouse/offline/errorPolicy";
import { normalizeKizCode } from "../../features/warehouse/kizFormat";
import {
  createIndexedDbQueueStore,
  type BlockedEvent,
  type OfflineQueueStore,
} from "../../features/warehouse/offline/queueStore";
import { offlineEventKey, type OfflineEvent } from "../../features/warehouse/offline/queueTypes";
import {
  eventMatchesReplayFilter,
  replayQueue,
  type ReplayDeps,
  type ReplayFilter,
} from "../../features/warehouse/offline/replay";

/** First cycle after start (`src/taksklad/main.py:189`) and the gap between cycles (`src/taksklad/app_data_loading.py:27`). */
export const STATION_SYNC_FIRST_RUN_MS = 13_000;
export const STATION_SYNC_INTERVAL_MS = 15_000;

/** A 401 asks for a new station sign-in no more often than this (spec section 4). */
export const RELOGIN_MIN_INTERVAL_MS = 30_000;

export type FlushResult = {
  /** Codes the server holds after this pass, including ones it already had (409 duplicate ack). */
  delivered: string[];
  /** Events the server refused for good during this pass, now in the blocked section. */
  blocked: BlockedEvent[];
  /** Events of this pass's scope still waiting in the queue. */
  pending: number;
  /** The server could not be reached or could not answer (network, 5xx, 401 after a re-login). */
  networkFailed: boolean;
};

export type CycleOutcome = { ok: true; result: FlushResult } | { ok: false; error: unknown };

export type StationQueueOptions = {
  /** Read on every send: the CSRF token changes after a re-login. */
  getConfig: () => ApiConfig;
  actor: string;
  workstationId: string;
  /** Signs the station in again; called on a 401, not more often than once per 30 s. */
  relogin: () => Promise<void>;
  onCycle?: (outcome: CycleOutcome) => void;
  store?: OfflineQueueStore;
  /** Defaults to the real API; tests pass their own. */
  send?: ReplayDeps;
  now?: () => number;
};

export type StationQueue = ReturnType<typeof createStationQueue>;

export function createStationQueue(options: StationQueueOptions) {
  const { getConfig, actor, workstationId, relogin, onCycle } = options;
  const now = options.now ?? (() => Date.now());
  const store = options.store ?? createIndexedDbQueueStore();
  const send: ReplayDeps = options.send ?? {
    sendScan: async (event) => {
      await createScan(getConfig(), {
        order_item_id: event.orderItemId,
        code: event.code,
        workstation_id: event.workstationId,
        scanned_by: event.actor,
      });
    },
    sendComplete: async (event) => {
      await completeWarehouseOrder(getConfig(), event.orderId);
    },
  };

  // Delivered: codes the last API load listed on the item, plus codes a pass synced since that load.
  let loadedCodes = new Map<string, Set<string>>();
  let syncedCodes = new Map<string, Set<string>>();

  function remember(into: Map<string, Set<string>>, orderItemId: string, code: string) {
    const codes = into.get(orderItemId) ?? new Set<string>();
    codes.add(normalizeKizCode(code));
    into.set(orderItemId, codes);
  }

  function forget(orderItemId: string, code: string) {
    loadedCodes.get(orderItemId)?.delete(normalizeKizCode(code));
    syncedCodes.get(orderItemId)?.delete(normalizeKizCode(code));
  }

  // 401 -> sign in again -> retry the same request once; a second 401 is left to the queue's own retry.
  let lastReloginAt = Number.NEGATIVE_INFINITY;
  async function withRelogin<T>(call: () => Promise<T>): Promise<T> {
    try {
      return await call();
    } catch (error) {
      if (!(error instanceof ApiRequestError) || error.status !== 401) throw error;
      const at = now();
      if (at - lastReloginAt < RELOGIN_MIN_INTERVAL_MS) throw error;
      lastReloginAt = at;
      try {
        await relogin();
      } catch {
        throw error;
      }
      return call();
    }
  }

  // One pass at a time: a screen action waits for the running pass, the cycle skips instead of waiting.
  let tail: Promise<void> = Promise.resolve();
  let passesInFlight = 0;
  function exclusive<T>(pass: () => Promise<T>): Promise<T> {
    passesInFlight += 1;
    const run = tail.then(pass);
    tail = run.then(() => undefined, () => undefined);
    return run.finally(() => { passesInFlight -= 1; });
  }

  async function runPass(filter?: ReplayFilter): Promise<FlushResult> {
    const delivered: string[] = [];
    const blockedKeys = new Set<string>();

    const deps: ReplayDeps = {
      sendScan: async (event) => {
        try {
          await withRelogin(() => send.sendScan(event));
        } catch (error) {
          const verdict = classifyReplayFailure(error);
          if (verdict === "blocked") blockedKeys.add(offlineEventKey(event));
          if (verdict !== "synced") throw error;
          // 409 duplicate ack: the server already holds the code, which is delivery all the same.
        }
        remember(syncedCodes, event.orderItemId, event.code);
        delivered.push(event.code);
      },
      sendComplete: async (event) => {
        try {
          await withRelogin(() => send.sendComplete(event));
        } catch (error) {
          if (classifyReplayFailure(error) === "blocked") blockedKeys.add(offlineEventKey(event));
          throw error;
        }
      },
    };

    const summary = await replayQueue(store, deps, filter);
    const [stillQueued, allBlocked] = await Promise.all([store.listPending(), store.listBlocked()]);
    const blockedNow = new Map(allBlocked.filter((item) => blockedKeys.has(item.key)).map((item) => [item.key, item]));
    return {
      delivered,
      blocked: [...blockedNow.values()],
      pending: stillQueued.filter((event) => eventMatchesReplayFilter(event, filter)).length,
      networkFailed: summary.failed > 0,
    };
  }

  function flush(filter?: ReplayFilter): Promise<FlushResult> {
    return exclusive(() => runPass(filter));
  }

  // The cycle is a chain, not an interval: the next run is planned when the previous one ends.
  let timer: ReturnType<typeof setTimeout> | undefined;
  let started = false;
  function plan(delayMs: number) {
    clearTimeout(timer);
    timer = started ? setTimeout(() => void cycle(), delayMs) : undefined;
  }
  async function cycle() {
    try {
      if (passesInFlight > 0) return;
      const outcome: CycleOutcome = await flush().then(
        (result) => ({ ok: true, result }),
        (error: unknown) => ({ ok: false, error }),
      );
      onCycle?.(outcome);
    } finally {
      plan(STATION_SYNC_INTERVAL_MS);
    }
  }

  function newEvent(type: OfflineEvent["type"], orderId: string, orderItemId: string, code: string): OfflineEvent {
    const at = new Date(now()).toISOString();
    return {
      type,
      orderId,
      orderItemId,
      code,
      actor,
      workstationId,
      scannedAt: at,
      createdAt: at,
      attempts: 0,
      lastError: "",
    };
  }

  return {
    start() {
      started = true;
      plan(STATION_SYNC_FIRST_RUN_MS);
    },
    stop() {
      started = false;
      clearTimeout(timer);
      timer = undefined;
    },

    /** Durable write only: nothing is sent, nothing is delivered. */
    async enqueueScan(input: { orderId: string; orderItemId: string; code: string }) {
      // A code scanned again has to wait for the server again, whatever it was before.
      forget(input.orderItemId, input.code);
      await store.enqueue(newEvent("scan", input.orderId, input.orderItemId, input.code));
    },
    async enqueueComplete(orderId: string) {
      await store.enqueue(newEvent("order_complete", orderId, "", ""));
    },

    /** Whole queue, on demand (manual refresh); the cycle runs the same pass. */
    flushAll: () => flush(),
    /** Next position: only the scans of this item (`src/taksklad/app_scanning.py:650`). */
    flushItem: (orderItemId: string) => flush({ orderItemIds: new Set([orderItemId]) }),
    /** Finish: the scans of the order's items first, then its order_complete (`src/taksklad/app_finish.py:150`). */
    flushOrder: (orderId: string, orderItemIds: string[]) =>
      flush({ orderItemIds: new Set(orderItemIds), orderIds: new Set([orderId]) }),

    /**
     * Call with every full API load of the order list. The loaded codes become the delivered
     * baseline. A pass that finishes while the load is in flight is forgotten here, which can only
     * show a delivered code as queued, never the reverse.
     */
    recordLoadedOrders(orders: Order[]) {
      loadedCodes = new Map();
      syncedCodes = new Map();
      for (const order of orders) {
        for (const item of order.items) {
          for (const code of item.scan_codes) remember(loadedCodes, item.id, code);
        }
      }
    },
    isDelivered(orderItemId: string, code: string): boolean {
      const normalized = normalizeKizCode(code);
      return Boolean(loadedCodes.get(orderItemId)?.has(normalized) || syncedCodes.get(orderItemId)?.has(normalized));
    },
  };
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /tmp/station-plan2/frontend && npx vitest run src/__tests__/offlineReplayFilter.test.ts src/station/__tests__/stationQueue.test.ts src/__tests__/offlineReplay.test.ts`
Expected: `Tests  68 passed` (старый `offlineReplay.test.ts` зелёный без изменений: без фильтра проход прежний)

- [ ] **Step 5: Commit**

```bash
cd /tmp/station-plan2
git add frontend/src/features/warehouse/offline/replay.ts frontend/src/station/queue/stationQueue.ts \
  frontend/src/__tests__/offlineReplayFilter.test.ts frontend/src/station/__tests__/stationQueue.test.ts
ALLOW_NON_MAIN_BRANCH=1 git commit -m "feat(station): очередь сканов с ритмом программы и отправкой по позиции и заказу"
```

---

### Task 11: Полная проверка ветки

**Files:**
- никаких новых файлов; в PR уходят только коммиты задач 1-10

- [ ] **Step 1: Фронт целиком**

Run: `cd /tmp/station-plan2/frontend && npx vitest run && npm run typecheck && npm run lint && npm run test:a11y && npm run e2e`
Expected: `Test Files  31 passed`, `Tests  661 passed` (на `origin/main` 17 файлов
и 283 теста); typecheck и lint без ошибок; a11y `Tests  7 passed`; e2e `21 passed`, `4 skipped`

- [ ] **Step 2: Сторожа станции и контракты фронта**

Run: `cd /tmp/station-plan2 && PYTHONPATH=. /Users/anton/Documents/work/TakSklad/.venv/bin/python -m unittest tests.test_station_parity_corpus tests.test_station_text_parity tests.test_frontend_csrf_contract tests.test_web_offline_queue_contract tests.test_desktop_ui_contract -v`
Expected: `OK`

- [ ] **Step 3: Корпус свежий**

Run: `cd /tmp/station-plan2 && PYTHONPATH=. /Users/anton/Documents/work/TakSklad/.venv/bin/python tools/generate_station_parity_corpus.py && git status --short frontend/src/station/__fixtures__/`
Expected: пустой вывод `git status` (повторная генерация даёт тот же файл байт в байт)

- [ ] **Step 4: Полный набор Python-тестов**

Запускать один, без параллельных прогонов в соседних деревьях: общий файловый замок даёт ложные падения

Run: `cd /tmp/station-plan2 && TAKSKLAD_PYTHON_BIN=/Users/anton/Documents/work/TakSklad/.venv/bin/python PYTHONPATH=. /Users/anton/Documents/work/TakSklad/.venv/bin/python -m unittest discover -s tests > /tmp/station-plan2-tests.log 2>&1; grep -E '^Ran [0-9]+ tests|^OK|^FAILED' /tmp/station-plan2-tests.log`
Expected: `Ran 2261 tests` и `OK (skipped=94, expected failures=4)` (на `origin/main` `c99b448` 2256, план добавляет 5);
postgres-набор не нужен: backend и маршруты RBAC план не трогает

- [ ] **Step 5: Состав ветки**

Run: `cd /tmp/station-plan2 && git status --short && git log --oneline origin/main..HEAD && git diff --stat origin/main..HEAD | tail -1`
Expected: дерево чистое (артефакты DR, если тесты их переписали, вернуть `git checkout -- test-artifacts/`);
10 коммитов задач; в списке файлов нет `backend/`, `deploy/`, `src/taksklad/`

- [ ] **Step 6: PR (только по разрешению Антона)**

Push и PR делаются после отдельного «да»; в описании PR вывод шагов 1-4 и строка, что план 2 не выкатывается
отдельно: `/` без окна станции показал бы пустую заглушку

```bash
cd /tmp/station-plan2
ALLOW_NON_MAIN_BRANCH=1 git push -u origin feat/station-logic
gh pr create --base main --head feat/station-logic --title "Логика станции склада без вёрстки (план 2)" --body-file <файл с описанием>
```
