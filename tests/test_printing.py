import os
import inspect
import subprocess
import unittest
from types import SimpleNamespace
from unittest import mock

from PIL import Image

from taksklad.app_finish import FinishActionsMixin
from taksklad.app_printing import PrintingActionsMixin
from taksklad import main as main_module
from taksklad import printing

CREATE_NO_WINDOW = 0x08000000


class FakeNtOs:
    """Подмена os только для модуля printing: остальной код процесса видит настоящий os."""

    name = "nt"

    def __getattr__(self, attribute):
        return getattr(os, attribute)


class FakePosixOs(FakeNtOs):
    name = "posix"


def nt_printing():
    return mock.patch.object(printing, "os", FakeNtOs())


def posix_printing():
    return mock.patch.object(printing, "os", FakePosixOs())


def completed(stdout="", returncode=0):
    return subprocess.CompletedProcess(["fake"], returncode, stdout=stdout, stderr="")


class PrintingTests(unittest.TestCase):
    def test_label_size_parser_accepts_supported_sizes(self):
        self.assertEqual(printing.parse_label_size_text("100x100"), (100, 100))
        self.assertEqual(printing.parse_label_size_text("100х150"), (100, 150))
        self.assertEqual(printing.parse_label_size_text("75 x 50"), (75, 50))
        self.assertEqual(printing.parse_label_size_text("58x40"), (58, 40))
        self.assertEqual(printing.parse_label_size_text("10x10"), (100, 100))

    def test_print_summary_uses_selected_label_size(self):
        original_load_print_settings = printing.load_print_settings
        original_send_images_to_printer = printing.send_images_to_printer
        captured = {}
        files = []

        def fake_settings():
            return {
                "printer_name": "Test Printer",
                "label_width_mm": 58,
                "label_height_mm": 40,
                "dpi": 203,
            }

        def fake_send(file_paths, printer_name="", label_width_mm=None, label_height_mm=None, **kwargs):
            captured["printer_name"] = printer_name
            captured["label_width_mm"] = label_width_mm
            captured["label_height_mm"] = label_height_mm
            files.extend(file_paths)
            return True

        try:
            printing.load_print_settings = fake_settings
            printing.send_images_to_printer = fake_send

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
            printing.send_images_to_printer = original_send_images_to_printer
            for file_path in files:
                try:
                    os.remove(file_path)
                except OSError:
                    pass

    def test_print_summary_accepts_dialog_selected_settings_without_persisting(self):
        original_load_print_settings = printing.load_print_settings
        original_send_images_to_printer = printing.send_images_to_printer
        captured = {}
        files = []

        def old_saved_settings():
            return {
                "printer_name": "Old Printer",
                "label_width_mm": 100,
                "label_height_mm": 100,
                "dpi": 203,
            }

        def fake_send(file_paths, printer_name="", label_width_mm=None, label_height_mm=None, **kwargs):
            captured["printer_name"] = printer_name
            captured["label_width_mm"] = label_width_mm
            captured["label_height_mm"] = label_height_mm
            files.extend(file_paths)
            return True

        try:
            printing.load_print_settings = old_saved_settings
            printing.send_images_to_printer = fake_send

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
            printing.send_images_to_printer = original_send_images_to_printer
            for file_path in files:
                try:
                    os.remove(file_path)
                except OSError:
                    pass

    def test_windows_printing_checks_printer_validity_and_captures_output(self):
        source = inspect.getsource(printing.send_images_to_windows_printer)

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


class PrinterCacheTests(unittest.TestCase):
    def setUp(self):
        printing.invalidate_printer_cache()
        self.addCleanup(printing.invalidate_printer_cache)

    def test_second_call_is_served_from_cache_without_subprocess(self):
        with nt_printing(), mock.patch.object(
            printing.subprocess, "run", return_value=completed("Name\nZebra\nEPSON\n")
        ) as run:
            first = printing.list_available_printers()
            second = printing.list_available_printers()

        self.assertEqual(first, ["EPSON", "Zebra"])
        self.assertEqual(second, ["EPSON", "Zebra"])
        self.assertEqual(run.call_count, 1)

    def test_invalidate_makes_next_call_run_subprocess_again(self):
        with nt_printing(), mock.patch.object(
            printing.subprocess, "run", return_value=completed("Zebra\n")
        ) as run:
            printing.list_available_printers()
            printing.invalidate_printer_cache()
            printing.list_available_printers()

        self.assertEqual(run.call_count, 2)

    def test_empty_result_is_not_cached(self):
        answers = [completed(""), completed(""), completed("Zebra\n")]
        with nt_printing(), mock.patch.object(printing.subprocess, "run", side_effect=answers) as run:
            first = printing.list_available_printers()
            second = printing.list_available_printers()
            third = printing.list_available_printers()

        self.assertEqual(first, [])
        self.assertEqual(second, ["Zebra"])
        self.assertEqual(third, ["Zebra"])
        # первый вызов: powershell и wmic пусты, второй вызов: powershell отдал список, третий из кэша
        self.assertEqual(run.call_count, 3)

    def test_cache_lives_ten_minutes_by_monotonic_clock(self):
        clock = [1000.0]
        fake_time = SimpleNamespace(monotonic=lambda: clock[0])
        with nt_printing(), mock.patch.object(printing, "time", fake_time), mock.patch.object(
            printing.subprocess, "run", return_value=completed("Zebra\n")
        ) as run:
            printing.list_available_printers()
            clock[0] += 599
            printing.list_available_printers()
            self.assertEqual(run.call_count, 1)
            clock[0] += 2
            printing.list_available_printers()
            self.assertEqual(run.call_count, 2)

        self.assertEqual(printing.PRINTER_CACHE_TTL_SECONDS, 600)

    def test_printer_reading_runs_without_console_window(self):
        answers = [completed(""), completed("Zebra\n")]
        with nt_printing(), mock.patch.object(
            printing.subprocess, "CREATE_NO_WINDOW", CREATE_NO_WINDOW, create=True
        ), mock.patch.object(printing.subprocess, "run", side_effect=answers) as run:
            self.assertEqual(printing.list_available_printers(), ["Zebra"])

        # powershell пуст, поэтому прочитан и запасной wmic: оба без окна консоли
        self.assertEqual([call.args[0][0] for call in run.call_args_list], ["powershell", "wmic"])
        for call in run.call_args_list:
            self.assertEqual(call.kwargs["creationflags"], CREATE_NO_WINDOW)

    def test_printer_reading_passes_zero_flags_when_constant_is_missing(self):
        run = mock.Mock(return_value=completed("Zebra\n"))
        with posix_printing(), mock.patch.object(printing, "subprocess", SimpleNamespace(run=run)):
            self.assertEqual(printing.list_available_printers(), ["Zebra"])

        self.assertEqual(run.call_args.args[0], ["lpstat", "-e"])
        self.assertEqual(run.call_args.kwargs["creationflags"], 0)

    def test_cached_list_cannot_be_changed_by_caller(self):
        with nt_printing(), mock.patch.object(
            printing.subprocess, "run", return_value=completed("Zebra\n")
        ) as run:
            printing.list_available_printers().append("Мусор")
            self.assertEqual(printing.list_available_printers(), ["Zebra"])

        self.assertEqual(run.call_count, 1)

    def test_invalidate_during_running_fetch_drops_that_stale_result(self):
        def run_and_invalidate(command, **kwargs):
            printing.invalidate_printer_cache()
            return completed("Zebra\n")

        with nt_printing(), mock.patch.object(
            printing.subprocess, "run", side_effect=run_and_invalidate
        ) as run:
            self.assertEqual(printing.list_available_printers(), ["Zebra"])
            printing.list_available_printers()

        self.assertEqual(run.call_count, 2)

    def test_prefetch_fills_cache_and_swallows_errors(self):
        with nt_printing(), mock.patch.object(
            printing.subprocess, "run", return_value=completed("Zebra\n")
        ) as run:
            printing.prefetch_available_printers()
            self.assertEqual(printing.list_available_printers(), ["Zebra"])
        self.assertEqual(run.call_count, 1)

        printing.invalidate_printer_cache()
        with mock.patch.object(printing, "list_available_printers", side_effect=RuntimeError("boom")), \
                mock.patch.object(printing.logging, "exception") as log_exception:
            printing.prefetch_available_printers()
        log_exception.assert_called_once()

    def test_prefetch_does_not_touch_tk(self):
        source = inspect.getsource(printing.prefetch_available_printers)

        self.assertNotIn("tkinter", source)
        self.assertNotIn("self.after", source)

    def test_startup_warms_printer_cache_in_daemon_thread_next_to_pending_prints(self):
        source = inspect.getsource(main_module.ScanningApp.__init__)
        pending_line = "self.after(500, self.check_pending_prints)"
        prefetch_line = "threading.Thread(target=prefetch_available_printers, daemon=True).start()"

        self.assertEqual(source.count(prefetch_line), 1)
        self.assertEqual(source.count(pending_line), 1)
        self.assertEqual(source.index(pending_line) + len(pending_line) + 9, source.index(prefetch_line))
        self.assertIs(main_module.prefetch_available_printers, printing.prefetch_available_printers)
        self.assertTrue(hasattr(main_module.threading, "Thread"))

    def test_print_dialog_still_reads_printers_through_list_available_printers(self):
        source = inspect.getsource(PrintingActionsMixin.confirm_print_settings)

        self.assertIn("available_printers = list_available_printers()", source)


class BatchPrintingTests(unittest.TestCase):
    SETTINGS = {
        "printer_name": "Test Printer",
        "label_width_mm": 100,
        "label_height_mm": 100,
        "dpi": 96,
    }

    def setUp(self):
        printing.invalidate_printer_cache()
        self.addCleanup(printing.invalidate_printer_cache)
        self.created_files = []
        self.addCleanup(self._remove_created_files)

    def _remove_created_files(self):
        for file_path in self.created_files:
            try:
                os.remove(file_path)
            except OSError:
                pass

    def products(self, count):
        return [
            {
                "Клиент": "Test Client",
                "Торговый представитель": "Test Rep",
                "Товары": f"Chapman Brown OP {number}",
                "Отсканировано": 1,
                "Кол-во ШТ в блоке": 10,
            }
            for number in range(count)
        ]

    def run_recorder(self, returncode=0, error=None):
        record = SimpleNamespace(calls=[], scripts=[], script_paths=[])

        def fake_run(command, **kwargs):
            record.calls.append((command, kwargs))
            if "-File" in command:
                script_path = command[command.index("-File") + 1]
                record.script_paths.append(script_path)
                with open(script_path, encoding="utf-8") as script_file:
                    record.scripts.append(script_file.read())
            if error is not None:
                raise error
            return completed("TakSklad printer: Test Printer\n", returncode)

        record.fake_run = fake_run
        return record

    def test_three_pages_go_to_printer_in_one_powershell_process(self):
        record = self.run_recorder()
        with nt_printing(), mock.patch.object(
            printing.subprocess, "CREATE_NO_WINDOW", CREATE_NO_WINDOW, create=True
        ), mock.patch.object(printing.subprocess, "run", side_effect=record.fake_run):
            result = printing.print_summary("Tashkent", self.products(7), print_settings=self.SETTINGS)

        self.created_files.extend(result or [])
        self.assertEqual(len(result), 3)
        self.assertEqual(len(record.calls), 1)
        command, kwargs = record.calls[0]
        self.assertEqual(
            command,
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", record.script_paths[0]],
        )
        self.assertEqual(kwargs["timeout"], 90)
        self.assertIs(kwargs["check"], True)
        self.assertEqual(kwargs["creationflags"], CREATE_NO_WINDOW)
        self.assertIs(kwargs["capture_output"], True)
        self.assertIs(kwargs["text"], True)

        script = record.scripts[0]
        for file_path in result:
            self.assertIn(printing.powershell_quote(os.path.abspath(file_path)), script)
        self.assertEqual(script.count("Add-Type -AssemblyName System.Drawing"), 1)
        self.assertEqual(script.count("New-Object System.Drawing.Printing.PrintDocument"), 1)
        self.assertIn("foreach ($imagePath in $imagePaths)", script)
        self.assertFalse(os.path.exists(record.script_paths[0]))

    def test_timeout_is_thirty_seconds_per_page(self):
        record = self.run_recorder()
        with nt_printing(), mock.patch.object(printing.subprocess, "run", side_effect=record.fake_run):
            self.assertTrue(printing.send_images_to_windows_printer(["a.png", "b.png"]))
            self.assertTrue(printing.send_images_to_windows_printer(["a.png"]))

        self.assertEqual([kwargs["timeout"] for _command, kwargs in record.calls], [60, 30])

    def test_batch_script_keeps_per_image_settings_of_single_page_script(self):
        record = self.run_recorder()
        with nt_printing(), mock.patch.object(printing.subprocess, "run", side_effect=record.fake_run):
            self.assertTrue(printing.send_image_to_windows_printer(
                "label.png", printer_name="Zebra ZD", label_width_mm=58, label_height_mm=40,
            ))

        self.assertEqual(len(record.calls), 1)
        self.assertEqual(record.calls[0][1]["timeout"], 30)
        script_lines = [line.strip() for line in record.scripts[0].splitlines()]
        image_path = printing.powershell_quote(os.path.abspath("label.png"))
        expected = [
            "Add-Type -AssemblyName System.Drawing",
            "$image = [System.Drawing.Image]::FromFile($imagePath)",
            "$printDocument = New-Object System.Drawing.Printing.PrintDocument",
            "$printDocument.PrinterSettings.PrinterName = 'Zebra ZD'",
            '$printDocument.DocumentName = "TakSklad summary"',
            '$printDocument.DefaultPageSettings.PaperSize = New-Object System.Drawing.Printing.PaperSize("Label58x40", 228, 157)',
            "$printDocument.DefaultPageSettings.Margins = New-Object System.Drawing.Printing.Margins(0, 0, 0, 0)",
            "$printDocument.OriginAtMargins = $false",
            "if (-not $printDocument.PrinterSettings.IsValid) {",
            'throw "Printer is not valid: $($printDocument.PrinterSettings.PrinterName)"',
            'Write-Output "TakSklad printer: $($printDocument.PrinterSettings.PrinterName)"',
            "$printDocument.add_PrintPage({",
            "param($sender, $event)",
            "$event.Graphics.DrawImage($image, $event.PageBounds)",
            "$event.HasMorePages = $false",
            "$printDocument.Print()",
            "} finally {",
            "$image.Dispose()",
            "$printDocument.Dispose()",
        ]
        for line in expected:
            self.assertIn(line, script_lines)
        self.assertTrue(any(line.startswith(image_path) for line in script_lines))

    def test_per_image_calls_live_inside_foreach_and_add_type_before_it(self):
        record = self.run_recorder()
        paths = ["a.png", "b.png", "c.png"]
        with nt_printing(), mock.patch.object(printing.subprocess, "run", side_effect=record.fake_run):
            self.assertTrue(printing.send_images_to_windows_printer(paths, printer_name="Zebra"))

        loop_header = "foreach ($imagePath in $imagePaths) {"
        script = record.scripts[0]
        self.assertEqual(script.count(loop_header), 1)
        before_loop, loop_body = script.split(loop_header)

        self.assertEqual(before_loop.count("Add-Type -AssemblyName System.Drawing"), 1)
        self.assertNotIn("Add-Type", loop_body)
        for path in paths:
            self.assertIn(printing.powershell_quote(os.path.abspath(path)), before_loop)
            self.assertNotIn(printing.powershell_quote(os.path.abspath(path)), loop_body)
        for per_image in (
            "[System.Drawing.Image]::FromFile($imagePath)",
            "New-Object System.Drawing.Printing.PrintDocument",
            "$printDocument.PrinterSettings.PrinterName = 'Zebra'",
            "$printDocument.DefaultPageSettings.PaperSize = New-Object",
            "$printDocument.add_PrintPage({",
            "$event.Graphics.DrawImage($image, $event.PageBounds)",
            "$event.HasMorePages = $false",
            "$printDocument.Print()",
            "$image.Dispose()",
            "$printDocument.Dispose()",
        ):
            self.assertNotIn(per_image, before_loop)
            self.assertEqual(loop_body.count(per_image), 1, per_image)
        # цикл закрывается последней строкой скрипта: после него ничего нет
        self.assertEqual(loop_body.rstrip().splitlines()[-1], "}")
        self.assertLess(loop_body.index("} finally {"), loop_body.rindex("}"))

    def test_default_printer_name_is_not_forced_in_script(self):
        record = self.run_recorder()
        with nt_printing(), mock.patch.object(printing.subprocess, "run", side_effect=record.fake_run):
            printing.send_images_to_windows_printer(["a.png"], printer_name="Термопринтер")
            printing.send_images_to_windows_printer(["a.png"], printer_name="")

        for script in record.scripts:
            self.assertNotIn("PrinterSettings.PrinterName =", script)

    def test_batch_log_is_one_line_with_all_files(self):
        record = self.run_recorder()
        with nt_printing(), mock.patch.object(
            printing.subprocess, "run", side_effect=record.fake_run
        ), mock.patch.object(printing.logging, "info") as log_info:
            printing.send_images_to_windows_printer(["a.png", "b.png", "c.png"], printer_name="Zebra")

        log_info.assert_called_once()
        rendered = log_info.call_args.args[0] % log_info.call_args.args[1:]
        for name in ("a.png", "b.png", "c.png"):
            self.assertIn(name, rendered)

    def test_script_file_is_removed_when_powershell_fails(self):
        record = self.run_recorder(error=subprocess.CalledProcessError(1, "powershell"))
        with nt_printing(), mock.patch.object(
            printing.subprocess, "run", side_effect=record.fake_run
        ), mock.patch.object(printing.logging, "exception"):
            with self.assertRaises(subprocess.CalledProcessError):
                printing.send_images_to_windows_printer(["a.png", "b.png"])
            self.assertFalse(printing.send_images_to_printer(["a.png", "b.png"]))

        self.assertEqual(len(record.script_paths), 2)
        for script_path in record.script_paths:
            self.assertFalse(os.path.exists(script_path))

    def test_send_images_to_printer_uses_batch_function_on_windows(self):
        with nt_printing(), mock.patch.object(
            printing, "send_images_to_windows_printer", return_value=True
        ) as batch, mock.patch.object(printing, "send_image_to_printer") as single:
            result = printing.send_images_to_printer(
                ["a.png", "b.png"], printer_name="Zebra", label_width_mm=58, label_height_mm=40,
            )

        self.assertTrue(result)
        single.assert_not_called()
        batch.assert_called_once_with(
            ["a.png", "b.png"], printer_name="Zebra", label_width_mm=58, label_height_mm=40,
        )

    def test_send_images_to_printer_returns_false_when_windows_batch_fails(self):
        with nt_printing(), mock.patch.object(
            printing, "send_images_to_windows_printer", side_effect=RuntimeError("boom")
        ), mock.patch.object(printing.logging, "exception"):
            self.assertFalse(printing.send_images_to_printer(["a.png"]))

    def test_send_images_to_printer_sends_one_by_one_outside_windows(self):
        sent = []

        def fake_send(file_path, printer_name="", label_width_mm=None, label_height_mm=None):
            sent.append((file_path, printer_name, label_width_mm, label_height_mm))
            return file_path != "bad.png"

        with posix_printing(), mock.patch.object(printing, "send_image_to_printer", side_effect=fake_send):
            self.assertTrue(printing.send_images_to_printer(["a.png", "b.png"], printer_name="P"))
            self.assertEqual([item[0] for item in sent], ["a.png", "b.png"])
            sent.clear()
            self.assertFalse(printing.send_images_to_printer(["a.png", "bad.png", "c.png"]))
            self.assertEqual([item[0] for item in sent], ["a.png", "bad.png"])

    def test_send_failures_are_logged_with_same_text_in_both_send_functions(self):
        failed = subprocess.CalledProcessError(1, "lp", output="out-text", stderr="err-text")
        message = "Не удалось отправить сводку напрямую на печать"

        with posix_printing(), mock.patch.object(printing.subprocess, "run", side_effect=failed), \
                mock.patch.object(printing.logging, "exception") as log_exception:
            self.assertFalse(printing.send_image_to_printer("a.png"))
        with nt_printing(), mock.patch.object(
            printing, "send_images_to_windows_printer", side_effect=failed
        ), mock.patch.object(printing.logging, "exception") as log_exception_batch:
            self.assertFalse(printing.send_images_to_printer(["a.png"]))
        for log in (log_exception, log_exception_batch):
            log.assert_called_once_with(message + ": stdout=%s stderr=%s", "out-text", "err-text")

        with posix_printing(), mock.patch.object(printing.subprocess, "run", side_effect=RuntimeError("boom")), \
                mock.patch.object(printing.logging, "exception") as log_exception:
            self.assertFalse(printing.send_image_to_printer("a.png"))
        with nt_printing(), mock.patch.object(
            printing, "send_images_to_windows_printer", side_effect=RuntimeError("boom")
        ), mock.patch.object(printing.logging, "exception") as log_exception_batch:
            self.assertFalse(printing.send_images_to_printer(["a.png"]))
        for log in (log_exception, log_exception_batch):
            log.assert_called_once_with(message)

    def test_send_images_to_printer_with_no_files_sends_nothing(self):
        with nt_printing(), mock.patch.object(printing.subprocess, "run") as run:
            self.assertFalse(printing.send_images_to_printer([]))

        run.assert_not_called()

    def test_all_pages_are_rendered_before_single_send(self):
        events = []
        original_save = Image.Image.save

        def recording_save(image, *args, **kwargs):
            events.append("render")
            return original_save(image, *args, **kwargs)

        def fake_send_images(file_paths, **kwargs):
            events.append(("send", list(file_paths)))
            return True

        with mock.patch.object(Image.Image, "save", recording_save), mock.patch.object(
            printing, "send_images_to_printer", side_effect=fake_send_images
        ) as send:
            result = printing.print_summary("Tashkent", self.products(7), print_settings=self.SETTINGS)

        self.created_files.extend(result or [])
        self.assertEqual(events[:3], ["render", "render", "render"])
        self.assertEqual(events[3], ("send", result))
        self.assertEqual(len(events), 4)
        send.assert_called_once()
        self.assertEqual(send.call_args.kwargs["printer_name"], "Test Printer")
        self.assertEqual(send.call_args.kwargs["label_width_mm"], 100)
        self.assertEqual(send.call_args.kwargs["label_height_mm"], 100)


class PrintFailureInvalidatesPrinterCacheTests(unittest.TestCase):
    SETTINGS = BatchPrintingTests.SETTINGS

    def setUp(self):
        printing.invalidate_printer_cache()
        self.addCleanup(printing.invalidate_printer_cache)
        self.created_files = []
        self.addCleanup(self._remove_created_files)

    def _remove_created_files(self):
        for file_path in self.created_files:
            try:
                os.remove(file_path)
            except OSError:
                pass

    def product(self):
        return [{
            "Клиент": "Test Client",
            "Торговый представитель": "Test Rep",
            "Товары": "Chapman Brown OP 20",
            "Отсканировано": 1,
            "Кол-во ШТ в блоке": 10,
        }]

    def fill_cache(self):
        with nt_printing(), mock.patch.object(
            printing.subprocess, "run", return_value=completed("Zebra\n")
        ) as run:
            printing.list_available_printers()
        self.assertEqual(run.call_count, 1)

    def printer_list_relaunches(self):
        with nt_printing(), mock.patch.object(
            printing.subprocess, "run", return_value=completed("Zebra\n")
        ) as run:
            printing.list_available_printers()
        return run.call_count == 1

    def test_failed_send_resets_cache(self):
        self.fill_cache()
        with mock.patch.object(printing, "send_images_to_printer", return_value=False):
            result = printing.print_summary("Tashkent", self.product(), print_settings=self.SETTINGS)

        self.assertIsNone(result)
        self.assertTrue(self.printer_list_relaunches())

    def test_error_while_rendering_resets_cache(self):
        self.fill_cache()
        with mock.patch.object(printing, "mm_to_px", side_effect=RuntimeError("boom")), \
                mock.patch.object(printing.logging, "exception"):
            result = printing.print_summary("Tashkent", self.product(), print_settings=self.SETTINGS)

        self.assertIsNone(result)
        self.assertTrue(self.printer_list_relaunches())

    def test_windows_powershell_failure_resets_cache(self):
        self.fill_cache()
        error = subprocess.CalledProcessError(1, "powershell")
        with nt_printing(), mock.patch.object(printing.subprocess, "run", side_effect=error), \
                mock.patch.object(printing.logging, "exception"):
            result = printing.print_summary("Tashkent", self.product(), print_settings=self.SETTINGS)

        self.assertIsNone(result)
        self.assertTrue(self.printer_list_relaunches())

    def test_successful_print_keeps_cache(self):
        self.fill_cache()
        with mock.patch.object(printing, "send_images_to_printer", return_value=True):
            result = printing.print_summary("Tashkent", self.product(), print_settings=self.SETTINGS)

        self.created_files.extend(result or [])
        self.assertTrue(result)
        self.assertFalse(self.printer_list_relaunches())


if __name__ == "__main__":
    unittest.main()
