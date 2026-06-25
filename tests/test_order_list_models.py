import unittest
from time import perf_counter

from taksklad.order_list_models import (
    DATE_SEPARATOR_KEY,
    ORDER_CARD_KIND,
    build_order_list_model,
    is_date_separator_key,
)


def make_order(
    request_number,
    client,
    product,
    blocks,
    shipment_date="22.06.2026",
    payment_type="Терминал",
    address="Ташкент",
    representative="ОПТ",
):
    return {
        "Номер заявки SkladBot": request_number,
        "Клиент": client,
        "Товары": product,
        "Кол-во блок": blocks,
        "Дата отгрузки": shipment_date,
        "Тип оплаты": payment_type,
        "Адрес": address,
        "Торговый представитель": representative,
    }


class OrderListModelTests(unittest.TestCase):
    def test_builds_grouped_order_cards_with_date_separators(self):
        model = build_order_list_model([
            make_order("WH-R-199186", 'ЧП "FIRDAVS YUKSAK"', "Chapman Brown SSL", 12),
            make_order("WH-R-199186", 'ЧП "FIRDAVS YUKSAK"', "Chapman RED OP", 8),
            make_order("WH-R-199190", "ООО SAMARKAND TRADE", "Chapman Green OP", 4, shipment_date="23.06.2026"),
        ])

        self.assertEqual(model.total_groups, 2)
        self.assertEqual(model.visible_cards, 2)
        self.assertEqual(model.subtitle_text, "2 активных заказов · список листается вниз")
        self.assertEqual(model.counter_text, "Показаны 2 из 2")
        self.assertTrue(is_date_separator_key(model.visible_order_groups[0]))
        self.assertEqual(model.visible_order_groups[0], (DATE_SEPARATOR_KEY, "22.06.2026"))

        first_card = next(row for row in model.rows if row.kind == ORDER_CARD_KIND)
        self.assertEqual(first_card.request_display, "WH-R-199186")
        self.assertEqual(first_card.client, 'ЧП "FIRDAVS YUKSAK"')
        self.assertEqual(first_card.sku_count, 2)
        self.assertEqual(first_card.blocks_count, 20)
        self.assertEqual(first_card.summary_text, "2 SKU · 20 блоков")

    def test_search_filters_visible_cards_without_changing_total_count(self):
        model = build_order_list_model([
            make_order("WH-R-199186", 'ЧП "FIRDAVS YUKSAK"', "Chapman Brown SSL", 12),
            make_order("WH-R-199190", "ООО SAMARKAND TRADE", "Chapman Green OP", 4),
        ], search_text="green")

        self.assertEqual(model.total_groups, 2)
        self.assertEqual(model.visible_cards, 1)
        self.assertEqual(model.counter_text, "Показаны 1 из 2")
        visible_cards = [row for row in model.rows if row.kind == ORDER_CARD_KIND]
        self.assertEqual(len(visible_cards), 1)
        self.assertEqual(visible_cards[0].client, "ООО SAMARKAND TRADE")

    def test_search_keeps_full_group_summary_for_multi_sku_card(self):
        model = build_order_list_model([
            make_order("WH-R-199186", 'ЧП "FIRDAVS YUKSAK"', "Chapman Brown SSL", 12),
            make_order("WH-R-199186", 'ЧП "FIRDAVS YUKSAK"', "Chapman RED OP", 8),
            make_order("WH-R-199190", "ООО SAMARKAND TRADE", "Chapman Green OP", 4),
        ], search_text="red")

        visible_cards = [row for row in model.rows if row.kind == ORDER_CARD_KIND]
        self.assertEqual(len(visible_cards), 1)
        self.assertEqual(visible_cards[0].client, 'ЧП "FIRDAVS YUKSAK"')
        self.assertEqual(visible_cards[0].summary_text, "2 SKU · 20 блоков")

    def test_missing_request_number_stays_selectable_with_real_group_key(self):
        model = build_order_list_model([
            make_order("", "Клиент без заявки", "Chapman Gold SSL", 3),
        ])

        visible_cards = [row for row in model.rows if row.kind == ORDER_CARD_KIND]
        self.assertEqual(len(visible_cards), 1)
        self.assertEqual(visible_cards[0].request_display, "Без номера SkladBot")
        self.assertEqual(model.visible_order_groups[-1], visible_cards[0].group_key)

    def test_large_order_list_builds_without_truncating_cards(self):
        orders = [
            make_order(
                f"WH-R-{200000 + index}",
                f"CLIENT-{index:04d}",
                "Chapman Brown SSL" if index % 2 else "Chapman RED OP",
                index % 9 + 1,
                shipment_date=f"{22 + index % 3:02d}.06.2026",
            )
            for index in range(2000)
        ]

        started_at = perf_counter()
        model = build_order_list_model(orders)
        elapsed = perf_counter() - started_at

        self.assertEqual(model.total_groups, 2000)
        self.assertEqual(model.visible_cards, 2000)
        self.assertEqual(len([row for row in model.rows if row.kind == ORDER_CARD_KIND]), 2000)
        self.assertLess(elapsed, 2.0)

        filtered = build_order_list_model(orders, search_text="CLIENT-1999")
        visible_cards = [row for row in filtered.rows if row.kind == ORDER_CARD_KIND]
        self.assertEqual(filtered.total_groups, 2000)
        self.assertEqual(filtered.visible_cards, 1)
        self.assertEqual(visible_cards[0].client, "CLIENT-1999")


if __name__ == "__main__":
    unittest.main()
