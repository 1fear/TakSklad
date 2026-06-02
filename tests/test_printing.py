import os
import inspect
import unittest

from PIL import Image

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

    def test_windows_printing_checks_printer_validity_and_captures_output(self):
        source = inspect.getsource(printing.send_image_to_windows_printer)

        self.assertIn("PrinterSettings.IsValid", source)
        self.assertIn("capture_output=True", source)
        self.assertIn("TakSklad printer", source)

    def test_print_dialog_does_not_replace_saved_printer_with_first_available(self):
        source = inspect.getsource(PrintingActionsMixin.confirm_print_settings)

        self.assertNotIn("printer_var.set(available_printers[0])", source)
        self.assertIn("printer_options.insert(0, selected_printer)", source)


if __name__ == "__main__":
    unittest.main()
