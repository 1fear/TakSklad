import unittest
from types import SimpleNamespace

from backend.app.kiz_reports_service import (
    item_kiz_is_completed,
    item_kiz_released_blocks,
    item_requires_kiz_completion,
)


def make_item(**overrides):
    order = SimpleNamespace(status=overrides.pop("order_status", "not_completed"),
                            raw_payload=overrides.pop("order_raw_payload", {}))
    defaults = dict(
        status="not_completed",
        requires_kiz=True,
        quantity_blocks=3,
        scanned_blocks=0,
        scan_codes=None,
        raw_payload={},
        order=order,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


class CompletedWithoutKizTests(unittest.TestCase):
    def test_plain_completed_item_still_requires_kiz(self):
        # Обычное завершение не освобождает от КИЗ, иначе отчёт потеряет строки
        self.assertTrue(item_requires_kiz_completion(make_item(status="completed")))

    def test_item_closed_without_kiz_does_not_hold_the_source_file(self):
        # Аудит 05.08.2026: complete-without-kiz писал признак в raw_payload,
        # но проверка его не читала, и файл-источник вечно висел незавершённым
        item = make_item(status="completed", raw_payload={"completed_without_kiz": True})
        self.assertFalse(item_requires_kiz_completion(item))
        self.assertTrue(item_kiz_is_completed(item))

    def test_order_level_flag_also_releases_the_item(self):
        item = make_item(
            status="completed",
            order_status="completed",
            order_raw_payload={"completed_without_kiz": True},
        )
        self.assertFalse(item_requires_kiz_completion(item))

    def test_flag_stops_working_when_the_order_returns_to_work(self):
        # Аудит 06.08.2026: признак липкий, его никто не снимает. Заказ,
        # заново открытый после closed-without-kiz, обязан снова требовать КИЗ,
        # иначе его позиции навсегда выпадают из контроля (на проде такой был)
        item = make_item(
            status="not_completed",
            order_status="not_completed",
            order_raw_payload={"completed_without_kiz": True},
        )
        self.assertTrue(item_requires_kiz_completion(item))

    def test_item_flag_stops_working_when_the_item_returns_to_work(self):
        item = make_item(status="not_completed", raw_payload={"completed_without_kiz": True})
        self.assertTrue(item_requires_kiz_completion(item))

    def test_terminal_statuses_keep_working(self):
        for status in ("archived_no_kiz", "cancelled", "removed_from_google_sheet"):
            with self.subTest(status=status):
                self.assertFalse(item_requires_kiz_completion(make_item(status=status)))


class ReleasedBlocksTests(unittest.TestCase):
    def test_donor_gap_explained_by_released_blocks_is_complete(self):
        # release_kiz забирает блок из закрытого заказа: разрыв осознан
        item = make_item(
            status="completed",
            quantity_blocks=3,
            scanned_blocks=2,
            raw_payload={"kiz_released_blocks": 1},
        )
        self.assertEqual(item_kiz_released_blocks(item), 1)
        self.assertTrue(item_kiz_is_completed(item))

    def test_unexplained_gap_is_still_incomplete(self):
        item = make_item(status="completed", quantity_blocks=3, scanned_blocks=2)
        self.assertFalse(item_kiz_is_completed(item))

    def test_released_blocks_do_not_mask_a_bigger_gap(self):
        item = make_item(
            status="completed",
            quantity_blocks=5,
            scanned_blocks=2,
            raw_payload={"kiz_released_blocks": 1},
        )
        self.assertFalse(item_kiz_is_completed(item))

    def test_broken_counter_is_treated_as_zero(self):
        for value in (None, "", "нет", -4):
            with self.subTest(value=value):
                item = make_item(quantity_blocks=1, scanned_blocks=0,
                                 raw_payload={"kiz_released_blocks": value})
                self.assertEqual(item_kiz_released_blocks(item), 0)


if __name__ == "__main__":
    unittest.main()
