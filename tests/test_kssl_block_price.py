"""Цена за блок у позиций KSSL: 230 000 сум вместо общих 240 000.

Формат KSSL стоит 23 000 за пачку против 24 000 у OP и SSL 100. Smartup
присылает свою сумму и считает её верно сам, а пути, где сумму подставляем мы
(телеграм-файл, ручной заказ из бота, десктопный разборщик), до этой правки
брали единую цену 240 000 и завышали итог сводного листа на 10 000 за блок.

Тест закрывает все четыре места подстановки и отдельно проверяет, что сумма,
пришедшая из источника, остаётся главной.
"""

import tempfile
import unittest
from datetime import date
from pathlib import Path

import openpyxl

from backend.app.excel_importer import excel_file_to_import_payload
from backend.app.imports_service import normalize_import_row
from backend.app.product_prices import block_price_for_product
from backend.app.smartup_auto_import import SmartupAutoImportConfig, build_import_rows
from backend.app.telegram_manual_support import build_manual_import_payload

BROWN_KSSL = "Chapman Brown KSSL 20"
GREEN_KSSL = "Chapman Green KSSL 20"
BROWN_OP = "Chapman Brown OP 20"
BROWN_SSL = "Chapman Brown SSL 100`20"

KSSL_BLOCK_PRICE = 230000
DEFAULT_BLOCK_PRICE = 240000


def orders_sheet_rows(rows):
    """Лист в том виде, в каком приходит телеграм-файл: колонка суммы пустая."""
    sheet_rows = [
        ["", "", "", "", "", "", "", "ИТОГО", "", "ДАТА ДОСТАВКИ"],
        [
            "Торговый представитель",
            "Клиент",
            "Координаты клиента",
            "",
            "",
            "ТМЦ",
            "Тип оплаты",
            "Количество заказа",
            "Сумма с переоценкой",
            "",
        ],
    ]
    for index, (product, quantity, line_sum) in enumerate(rows):
        sheet_rows.append([
            "ТП1",
            f"Client {index + 1}",
            "41.320075",
            "69.298547",
            "41.320075,69.298547",
            product,
            "Перечисление",
            quantity,
            line_sum,
            "2026-09-22" if index == 0 else "",
        ])
    return sheet_rows


def write_orders_workbook(path, rows):
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "Конструктор отчетов"
    for row in orders_sheet_rows(rows):
        sheet.append(row)
    workbook.save(path)


def smartup_order(products):
    return {
        "deal_id": "9101",
        "deal_time": "22.09.2026 09:10:00",
        "delivery_date": "22.09.2026",
        "status": "B#N",
        "payment_type_code": "PYMT:2",
        "person_name": "TEST TRADE MCHJ",
        "delivery_address_full": "Ташкент, тестовая 1",
        "person_latitude": "41.311081",
        "person_longitude": "69.240562",
        "sales_manager_name": "ТП",
        "order_products": products,
    }


def smartup_config():
    return SmartupAutoImportConfig(
        enabled=True,
        smartup_username="user",
        smartup_password="password",
        route_fingerprint_key="synthetic-unit-route-key",
        output_dir=Path("/tmp"),
    )


class BlockPriceTableTests(unittest.TestCase):
    def test_kssl_costs_less_than_other_formats(self):
        for product in (BROWN_KSSL, GREEN_KSSL):
            with self.subTest(product=product):
                self.assertEqual(
                    block_price_for_product(product, DEFAULT_BLOCK_PRICE),
                    KSSL_BLOCK_PRICE,
                )

    def test_other_formats_keep_the_default(self):
        for product in (BROWN_OP, BROWN_SSL, "Chapman RED OP 20", "Chapman Gold SSL 100`20"):
            with self.subTest(product=product):
                self.assertEqual(
                    block_price_for_product(product, DEFAULT_BLOCK_PRICE),
                    DEFAULT_BLOCK_PRICE,
                )

    def test_kssl_recognised_with_supplier_suffix_and_odd_spacing(self):
        for product in (
            f"{BROWN_KSSL} / VON EICKEN / Германия",
            "chapman green kssl 20",
            'Chapman Green KSSL 20"',
        ):
            with self.subTest(product=product):
                self.assertEqual(
                    block_price_for_product(product, DEFAULT_BLOCK_PRICE),
                    KSSL_BLOCK_PRICE,
                )

    def test_unknown_product_keeps_the_default(self):
        for product in ("", "Неизвестный товар", "Chapman Brown"):
            with self.subTest(product=product):
                self.assertEqual(
                    block_price_for_product(product, DEFAULT_BLOCK_PRICE),
                    DEFAULT_BLOCK_PRICE,
                )

    def test_default_is_honoured_even_for_kssl_when_table_is_bypassed(self):
        """Таблица возвращает свою цену, а не множитель от переданной."""
        self.assertEqual(block_price_for_product(BROWN_KSSL, 999), KSSL_BLOCK_PRICE)


class TelegramExcelPriceTests(unittest.TestCase):
    def test_kssl_row_priced_at_230000_and_op_row_untouched(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "Перечисление 22_09_2026.xlsx"
            write_orders_workbook(path, [
                (BROWN_KSSL, 30, ""),
                (BROWN_OP, 20, ""),
            ])
            payload = excel_file_to_import_payload(path, file_name=path.name, source="telegram")

        rows = {row["Товары"]: row for row in payload["rows"]}
        self.assertEqual(sorted(rows), sorted([BROWN_KSSL, BROWN_OP]))

        kssl = rows[BROWN_KSSL]
        self.assertEqual(kssl["Кол-во блок"], 3)
        self.assertEqual(kssl["Цена за блок"], KSSL_BLOCK_PRICE)
        self.assertEqual(kssl["Сумма позиции"], 690000)
        self.assertEqual(kssl["Сумма рассчитанная"], 690000)

        other = rows[BROWN_OP]
        self.assertEqual(other["Кол-во блок"], 2)
        self.assertEqual(other["Цена за блок"], DEFAULT_BLOCK_PRICE)
        self.assertEqual(other["Сумма позиции"], 480000)

    def test_sum_from_file_still_wins_over_the_table(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "Перечисление 22_09_2026.xlsx"
            write_orders_workbook(path, [(BROWN_KSSL, 10, 210000)])
            payload = excel_file_to_import_payload(path, file_name=path.name, source="telegram")

        row = payload["rows"][0]
        self.assertEqual(row["Сумма из файла"], 210000)
        self.assertEqual(row["Сумма позиции"], 210000)
        self.assertEqual(row["Цена за блок"], KSSL_BLOCK_PRICE)


class NormalizeImportRowPriceTests(unittest.TestCase):
    """Путь десктопного разборщика: он шлёт строку без цены вообще."""

    def base_row(self, product, quantity_blocks):
        return {
            "Дата отгрузки": "22.09.2026",
            "Тип оплаты": "Перечисление",
            "Клиент": "TEST TRADE MCHJ",
            "Адрес": "Ташкент, тестовая 1",
            "Координаты": "41.31,69.24",
            "Товары": product,
            "Кол-во ШТ": quantity_blocks * 10,
            "Кол-во блок": quantity_blocks,
            "Статус": "not_completed",
            "ID заказа": "desktop-1",
            "ID импорта": f"desktop-1:{product}",
        }

    def test_row_without_price_gets_kssl_price(self):
        row = normalize_import_row(self.base_row(GREEN_KSSL, 4))
        self.assertEqual(row["block_price"], KSSL_BLOCK_PRICE)
        self.assertEqual(row["line_total"], 920000)

    def test_row_without_price_keeps_default_for_other_formats(self):
        row = normalize_import_row(self.base_row(BROWN_OP, 4))
        self.assertEqual(row["block_price"], DEFAULT_BLOCK_PRICE)
        self.assertEqual(row["line_total"], 960000)

    def test_explicit_price_in_row_wins(self):
        raw = self.base_row(GREEN_KSSL, 4)
        raw["Цена за блок"] = 200000
        row = normalize_import_row(raw)
        self.assertEqual(row["block_price"], 200000)
        self.assertEqual(row["line_total"], 800000)


class SmartupPriceTests(unittest.TestCase):
    def test_amount_from_smartup_stays_the_source_of_truth(self):
        order = smartup_order([
            {
                "external_id": "line-1",
                "product_code": "brown-kssl",
                "product_name": BROWN_KSSL,
                "order_quant": "30",
                "product_price": "23000",
                "sold_amount": "690000",
            },
        ])
        rows = build_import_rows([order], date(2026, 9, 22), "Перечисление.xlsx", smartup_config())

        self.assertEqual(rows[0]["Сумма из файла"], 690000)
        self.assertEqual(rows[0]["Сумма позиции"], 690000)
        self.assertEqual(rows[0]["Цена за блок"], KSSL_BLOCK_PRICE)

    def test_kssl_line_without_amount_falls_back_to_230000(self):
        order = smartup_order([
            {
                "external_id": "line-1",
                "product_code": "green-kssl",
                "product_name": GREEN_KSSL,
                "order_quant": "20",
                "product_price": "23000",
                "sold_amount": "",
            },
            {
                "external_id": "line-2",
                "product_code": "brown-op",
                "product_name": BROWN_OP,
                "order_quant": "20",
                "product_price": "24000",
                "sold_amount": "",
            },
        ])
        rows = build_import_rows([order], date(2026, 9, 22), "Перечисление.xlsx", smartup_config())

        self.assertEqual(rows[0]["Товары"], GREEN_KSSL)
        self.assertEqual(rows[0]["Цена за блок"], KSSL_BLOCK_PRICE)
        self.assertEqual(rows[0]["Сумма позиции"], 460000)

        self.assertEqual(rows[1]["Товары"], BROWN_OP)
        self.assertEqual(rows[1]["Цена за блок"], DEFAULT_BLOCK_PRICE)
        self.assertEqual(rows[1]["Сумма позиции"], 480000)


class TelegramManualOrderPriceTests(unittest.TestCase):
    def manual_flow(self, items):
        return {
            "data": {
                "manual_id": "manual-test",
                "order_date": "22.09.2026",
                "payment_type": "Терминал",
                "client": "TEST TRADE MCHJ",
                "address": "Ташкент, тестовая 1",
                "coordinates": "41.31,69.24",
                "representative": "ТП",
                "items": items,
            }
        }

    def test_manual_kssl_order_priced_at_230000(self):
        payload = build_manual_import_payload(1, self.manual_flow([
            {"product": BROWN_KSSL, "blocks": 5},
            {"product": BROWN_OP, "blocks": 5},
        ]))

        kssl, other = payload["rows"]
        self.assertEqual(kssl["Товары"], BROWN_KSSL)
        self.assertEqual(kssl["Цена за блок"], KSSL_BLOCK_PRICE)
        self.assertEqual(kssl["Сумма позиции"], 1150000)

        self.assertEqual(other["Товары"], BROWN_OP)
        self.assertEqual(other["Цена за блок"], DEFAULT_BLOCK_PRICE)
        self.assertEqual(other["Сумма позиции"], 1200000)


if __name__ == "__main__":
    unittest.main()
