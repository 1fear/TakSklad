import os
import inspect
import unittest

from PIL import Image

from taksklad.app_finish import FinishActionsMixin
from taksklad.app_printing import PrintingActionsMixin
from taksklad import printing


class PrintingTests(unittest.TestCase):
    def test_label_size_parser_accepts_supported_sizes(self):
        self.assertEqual(printing.parse_label_size_text("100x100"), (100, 100))
        self.assertEqual(printing.parse_label_size_text("100х150"), (100, 150))
        self.assertEqual(printing.parse_label_size_text("75 x 50"), (75, 50))
        self.assertEqual(printing.parse_label_size_text("58x40"), (58, 40))
        self.assertEqual(printing.parse_label_size_text("10x10"), (100, 100))

    def test_print_summary_uses_selected_label_size(self):
        original_load_print_settings = printing.load_print_settings
        original_send_image_to_printer = printing.send_image_to_printer
        captured = {}
        files = []

        def fake_settings():
            return {
                "printer_name": "Test Printer",
                "label_width_mm": 58,
                "label_height_mm": 40,
                "dpi": 203,
            }

        def fake_send(file_path, printer_name="", label_width_mm=None, label_height_mm=None):
            captured["printer_name"] = printer_name
            captured["label_width_mm"] = label_width_mm
            captured["label_height_mm"] = label_height_mm
            files.append(file_path)
            return True

        try:
            printing.load_print_settings = fake_settings
            printing.send_image_to_printer = fake_send

            result = printing.print_summary("Tashkent", [{
                "Клиент": "Test Client",
                "Торговый представитель": "Test Rep",
                "Товары": "Chapman Brown OP 20",
                "Отсканировано": 2,
                "Кол-во ШТ в блоке": 10,
            }])

            self.assertTrue(result)
            self.assertEqual(captured["printer_name"], "Test Printer")
            self.assertEqual(captured["label_width_mm"], 58)
            self.assertEqual(captured["label_height_mm"], 40)
            with Image.open(result[0]) as image:
                self.assertEqual(image.size, (printing.mm_to_px(58, 203), printing.mm_to_px(40, 203)))
        finally:
            printing.load_print_settings = original_load_print_settings
            printing.send_image_to_printer = original_send_image_to_printer
            for file_path in files:
                try:
                    os.remove(file_path)
                except OSError:
                    pass

    def test_print_summary_accepts_dialog_selected_settings_without_persisting(self):
        original_load_print_settings = printing.load_print_settings
        original_send_image_to_printer = printing.send_image_to_printer
        captured = {}
        files = []

        def old_saved_settings():
            return {
                "printer_name": "Old Printer",
                "label_width_mm": 100,
                "label_height_mm": 100,
                "dpi": 203,
            }

        def fake_send(file_path, printer_name="", label_width_mm=None, label_height_mm=None):
            captured["printer_name"] = printer_name
            captured["label_width_mm"] = label_width_mm
            captured["label_height_mm"] = label_height_mm
            files.append(file_path)
            return True

        try:
            printing.load_print_settings = old_saved_settings
            printing.send_image_to_printer = fake_send

            result = printing.print_summary(
                "Tashkent",
                [{
                    "Клиент": "Test Client",
                    "Торговый представитель": "Test Rep",
                    "Товары": "Chapman Brown OP 20",
                    "Отсканировано": 1,
                    "Кол-во ШТ в блоке": 10,
                }],
                print_settings={
                    "printer_name": "Dialog Printer",
                    "label_width_mm": 75,
                    "label_height_mm": 50,
                    "dpi": 203,
                },
            )

            self.assertTrue(result)
            self.assertEqual(captured["printer_name"], "Dialog Printer")
            self.assertEqual(captured["label_width_mm"], 75)
            self.assertEqual(captured["label_height_mm"], 50)
            with Image.open(result[0]) as image:
                self.assertEqual(image.size, (printing.mm_to_px(75, 203), printing.mm_to_px(50, 203)))
        finally:
            printing.load_print_settings = original_load_print_settings
            printing.send_image_to_printer = original_send_image_to_printer
            for file_path in files:
                try:
                    os.remove(file_path)
                except OSError:
                    pass

    def test_windows_printing_checks_printer_validity_and_captures_output(self):
        source = inspect.getsource(printing.send_image_to_windows_printer)

        self.assertIn("PrinterSettings.IsValid", source)
        self.assertIn("capture_output=True", source)
        self.assertIn("TakSklad printer", source)

    def test_print_dialog_does_not_replace_saved_printer_with_first_available(self):
        source = inspect.getsource(PrintingActionsMixin.confirm_print_settings)

        self.assertNotIn("printer_var.set(available_printers[0])", source)
        self.assertIn("printer_options.insert(0, selected_printer)", source)

    def test_print_dialog_selected_settings_are_used_for_current_print(self):
        dialog_source = inspect.getsource(PrintingActionsMixin.confirm_print_settings)
        pending_source = inspect.getsource(PrintingActionsMixin.check_pending_prints)
        finish_source = inspect.getsource(FinishActionsMixin.finish_legal_entity)

        self.assertIn("self._selected_print_settings = selected_settings", dialog_source)
        self.assertIn("save_print_settings(selected_settings)", dialog_source)
        self.assertIn("selected_print_settings = getattr(self, \"_selected_print_settings\", None)", pending_source)
        self.assertIn("print_settings=selected_print_settings", pending_source)
        self.assertIn("selected_print_settings = getattr(self, \"_selected_print_settings\", None)", finish_source)
        self.assertIn("print_summary(address, summary_products, print_settings=selected_print_settings)", finish_source)

    def test_pending_print_retry_requires_queue_remove_success(self):
        source = inspect.getsource(PrintingActionsMixin.check_pending_prints)

        self.assertIn("if not remove_pending_print(item.get(\"id\"))", source)
        self.assertIn("Сводка напечатана, но не удалена из очереди печати", source)


if __name__ == "__main__":
    unittest.main()
