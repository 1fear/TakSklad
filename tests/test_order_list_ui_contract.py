import unittest
from types import SimpleNamespace

from taksklad.main import ScanningApp
from taksklad.order_list_models import DATE_SEPARATOR_KEY, ORDER_CARD_KIND
from taksklad.orders import order_group_key


class FakeVar:
    def __init__(self, value=""):
        self.value = value

    def get(self):
        return self.value


class FakeLabel:
    def __init__(self):
        self.options = {}

    def config(self, **kwargs):
        self.options.update(kwargs)


class FakeOrderCardList:
    def __init__(self, selected_key=None):
        self._selected_key = selected_key
        self.rows = ()
        self.selected_key_arg = None
        self.cleared = False

    def set_rows(self, rows, selected_key=None):
        self.rows = tuple(rows)
        self.selected_key_arg = selected_key
        if selected_key:
            self._selected_key = selected_key

    def selected_key(self):
        return self._selected_key

    def select_first_card(self):
        for row in self.rows:
            if getattr(row, "kind", "") == ORDER_CARD_KIND:
                self._selected_key = row.group_key
                return True
        return False

    def clear_selection(self):
        self.cleared = True
        self._selected_key = None


def make_order(request_number, client, product="Chapman Brown SSL", blocks=5):
    return {
        "Номер заявки SkladBot": request_number,
        "Клиент": client,
        "Товары": product,
        "Кол-во блок": blocks,
        "Дата отгрузки": "22.06.2026",
        "Тип оплаты": "Терминал",
        "Адрес": "Ташкент",
    }


class OrderListUiContractTests(unittest.TestCase):
    def test_refresh_order_list_feeds_card_widget_with_real_group_key(self):
        first = make_order("WH-R-199186", 'ЧП "FIRDAVS YUKSAK"', blocks=12)
        second = make_order("WH-R-199186", 'ЧП "FIRDAVS YUKSAK"', product="Chapman RED OP", blocks=8)
        selected_key = order_group_key(first)
        fake = SimpleNamespace(
            today_orders=[first, second],
            search_var=FakeVar(""),
            current_group_key=selected_key,
            order_card_list=FakeOrderCardList(),
            order_list_subtitle_label=FakeLabel(),
            order_list_counter_label=FakeLabel(),
            update_stats_display=lambda: None,
        )

        ScanningApp.refresh_legal_list(fake)

        self.assertEqual(fake.order_card_list.selected_key_arg, selected_key)
        self.assertEqual(fake.visible_order_groups[-1], selected_key)
        self.assertEqual(fake.order_list_counter_label.options["text"], "Показаны 1 из 1")
        visible_cards = [row for row in fake.order_card_list.rows if row.kind == ORDER_CARD_KIND]
        self.assertEqual(visible_cards[0].summary_text, "2 SKU · 20 блоков")

    def test_selected_order_group_rejects_date_separator(self):
        errors = []
        fake = SimpleNamespace(
            order_card_list=FakeOrderCardList(selected_key=(DATE_SEPARATOR_KEY, "22.06.2026")),
            show_error=lambda message, popup=True: errors.append((message, popup)),
        )

        self.assertIsNone(ScanningApp._selected_order_group(fake))
        self.assertEqual(errors, [("Выберите заказ под датой, а не заголовок даты", False)])

    def test_select_first_real_order_uses_first_card_not_date_header(self):
        first = make_order("WH-R-199186", 'ЧП "FIRDAVS YUKSAK"')
        fake = SimpleNamespace(
            today_orders=[first],
            search_var=FakeVar(""),
            current_group_key=None,
            order_card_list=FakeOrderCardList(),
            update_stats_display=lambda: None,
        )
        ScanningApp.refresh_legal_list(fake)

        self.assertTrue(ScanningApp._select_first_real_order(fake))
        self.assertEqual(fake.order_card_list.selected_key(), order_group_key(first))

    def test_reset_current_selection_clears_card_selection(self):
        fake = SimpleNamespace(
            current_info=FakeLabel(),
            current_client_label=FakeLabel(),
            current_product_label=FakeLabel(),
            party_summary_label=FakeLabel(),
            position_label=FakeLabel(),
            progress_label=FakeLabel(),
            next_product_btn=FakeLabel(),
            finish_btn=FakeLabel(),
            undo_btn=FakeLabel(),
            last_code_label=FakeLabel(),
            order_card_list=FakeOrderCardList(selected_key=("WH-R-199186", "Client", "Терминал", "Адрес")),
            update_product_photo=lambda _product: None,
        )

        ScanningApp.reset_current_selection(fake)

        self.assertTrue(fake.order_card_list.cleared)
        self.assertIsNone(fake.current_group_key)


if __name__ == "__main__":
    unittest.main()
