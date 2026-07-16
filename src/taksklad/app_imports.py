from tkinter import filedialog, messagebox

from .backend_client import backend_configured, import_orders, preview_import_orders
from .catalog import load_product_catalog
from .config import APP_NAME, BG_MAIN, FG_MUTED
from .excel_import import parse_excel_order_files
from .utils import parse_int_value


def source_filename_for_records(records):
    imported_sources = sorted({record.get("Источник файла", "") for record in records if record.get("Источник файла")})
    return ", ".join(imported_sources[:5])


def apply_backend_import_preview(parsed, preview_result):
    records = parsed.get("records", [])
    duplicate_numbers = {
        parse_int_value(number)
        for number in preview_result.get("duplicate_row_numbers", [])
        if parse_int_value(number) > 0
    }
    invalid_numbers = {
        parse_int_value(number)
        for number in preview_result.get("invalid_row_numbers", [])
        if parse_int_value(number) > 0
    }
    blocked_numbers = duplicate_numbers | invalid_numbers
    new_records = []
    duplicate_records = []

    for index, record in enumerate(records, start=1):
        if index in duplicate_numbers:
            duplicate_records.append(record)
        elif index not in blocked_numbers:
            new_records.append(record)

    errors = list(parsed.get("errors", []))
    for error in preview_result.get("errors", []):
        errors.append(f"backend preview: {error}")

    parsed["errors"] = errors
    parsed["new_records"] = new_records
    parsed["duplicate_records"] = duplicate_records
    parsed["clients_count"] = len({record.get("Клиент") for record in new_records})
    parsed["products_count"] = len({record.get("Товары") for record in new_records})
    parsed["blocks_count"] = sum(parse_int_value(record.get("Кол-во блок")) for record in new_records)
    parsed["quantity_count"] = sum(parse_int_value(record.get("Кол-во ШТ")) for record in new_records)
    parsed["backend_import"] = True
    parsed["backend_preview"] = preview_result
    parsed["backend_invalid_rows_count"] = len(invalid_numbers)
    return parsed


class ImportActionsMixin:
    def import_excel_orders(self):
        if not self.ensure_update_allowed():
            return

        if self.operation_in_progress:
            self.show_busy_error()
            return

        file_paths = filedialog.askopenfilenames(
            title="Выберите Excel-файлы заказов",
            filetypes=[
                ("Excel files", "*.xlsx *.xlsm"),
                ("All files", "*.*"),
            ],
        )
        if not file_paths:
            return

        self.set_busy("⏳ Проверяю Excel-файлы перед импортом...")
        self.safe_config(self.import_btn, state="disabled")
        self.safe_config(self.refresh_btn, state="disabled")

        def work():
            if not backend_configured():
                raise RuntimeError("Backend не настроен. Импорт заблокирован")
            parsed = parse_excel_order_files(list(file_paths))
            records = parsed.get("records", [])
            preview_result = preview_import_orders(records, filename=source_filename_for_records(records))
            return apply_backend_import_preview(parsed, preview_result)

        def on_success(preview):
            self.clear_busy()
            self.safe_config(self.import_btn, state="normal")
            self.safe_config(self.refresh_btn, state="normal")

            errors = preview.get("errors", [])
            warnings = preview.get("warnings", [])
            new_records = preview.get("new_records", [])
            duplicate_records = preview.get("duplicate_records", [])
            source_duplicate_rows = preview.get("source_duplicate_rows_count", 0)
            backend_invalid_rows = preview.get("backend_invalid_rows_count", 0)

            if not new_records:
                details = [
                    f"Файлов проверено: {preview.get('files_count', 0)}",
                    f"Строк в файлах: {preview.get('source_rows_count', 0)}",
                    f"Адресов получено из координат: {preview.get('geocoded_count', 0)}",
                    f"Координат без адреса: {preview.get('geocode_failed_count', 0)}",
                    f"Повторных строк в Excel: {source_duplicate_rows}",
                    f"Дублей в таблице найдено: {len(duplicate_records)}",
                    f"Строк не принято backend: {backend_invalid_rows}",
                ]
                if errors:
                    details.append("\nОшибки:\n" + "\n".join(errors[:6]))
                if warnings:
                    details.append("\nПредупреждения:\n" + "\n".join(warnings[:6]))
                self.show_warning("Новых заказов для загрузки нет.\n\n" + "\n".join(details))
                return

            message_lines = [
                "Проверка Excel завершена.",
                "",
                f"Файлов: {preview.get('files_count', 0)}",
                f"Строк в файлах: {preview.get('source_rows_count', 0)}",
                f"Новых позиций после проверки: {len(new_records)}",
                f"Клиентов: {preview.get('clients_count', 0)}",
                f"Товаров: {preview.get('products_count', 0)}",
                f"ШТ всего: {preview.get('quantity_count', 0)}",
                f"Блоков к сканированию: {preview.get('blocks_count', 0)}",
                f"Адресов получено из координат: {preview.get('geocoded_count', 0)}",
                f"Координат без адреса: {preview.get('geocode_failed_count', 0)}",
                f"Повторных строк в Excel пропущено: {source_duplicate_rows}",
                f"Повторных позиций в таблице пропущено: {len(duplicate_records)}",
                f"Строк не принято backend: {backend_invalid_rows}",
            ]
            if errors:
                message_lines.extend(["", "Ошибки в отдельных файлах:", "\n".join(errors[:5])])
            if warnings:
                message_lines.extend(["", "Предупреждения:", "\n".join(warnings[:5])])
            message_lines.extend(["", "Загрузить новые позиции в backend?"])

            if not messagebox.askyesno("Подтверждение импорта", "\n".join(message_lines)):
                self.status_var.set("Импорт отменён")
                return

            self.commit_excel_import(new_records)

        def on_error(exc):
            self.show_critical_error("Не удалось проверить Excel-файлы", exc)

        def on_finally():
            self.clear_busy()
            self.safe_config(self.import_btn, state="normal")
            self.safe_config(self.refresh_btn, state="normal")

        self.run_background(
            "Не удалось проверить Excel-файлы",
            work,
            on_success=on_success,
            on_error=on_error,
            on_finally=on_finally,
        )

    def commit_excel_import(self, records):
        self.set_busy("⏳ Загружаю заказы в backend...")
        self.safe_config(self.import_btn, state="disabled")
        self.safe_config(self.refresh_btn, state="disabled")

        def work():
            if not backend_configured():
                raise RuntimeError("Backend не настроен. Импорт заблокирован")
            result = import_orders(records, filename=source_filename_for_records(records))
            result = {
                "imported": result.get("rows_imported", 0),
                "duplicates": result.get("duplicate_rows", 0),
                "backend": result,
            }
            loaded = self.fetch_sheet_data_after_import()
            return result, loaded

        def on_success(result):
            import_result, loaded = result
            self.product_catalog = load_product_catalog()
            self.apply_loaded_data(loaded, show_empty_warning=False)
            self.reset_current_selection()
            self.refresh_legal_list()
            self.show_info(
                f"Загружено позиций: {import_result.get('imported', 0)}\n"
                f"Повторно пропущено: {import_result.get('duplicates', 0)}",
            )
            imported_sources = sorted({record.get("Источник файла", "") for record in records if record.get("Источник файла")})
            imported_blocks = sum(parse_int_value(record.get("Кол-во блок")) for record in records)
            if imported_sources:
                self.send_telegram_alert_async(
                    f"{APP_NAME}: импортирован документ\n\n"
                    f"Документы: {', '.join(imported_sources[:5])}\n"
                    f"Позиций загружено: {import_result.get('imported', 0)}\n"
                    f"План КИЗ: {imported_blocks}\n\n"
                    "Документ доступен в разделе «Документы по импорту».",
                    with_keyboard=True,
                )
            self.status_var.set("✅ Excel-заказы загружены")
            self.status_label.config(bg=BG_MAIN, fg=FG_MUTED)

        def on_error(exc):
            self.show_critical_error("Не удалось загрузить Excel-заказы", exc)

        def on_finally():
            self.clear_busy()
            self.safe_config(self.import_btn, state="normal")
            self.safe_config(self.refresh_btn, state="normal")

        self.run_background(
            "Не удалось загрузить Excel-заказы",
            work,
            on_success=on_success,
            on_error=on_error,
            on_finally=on_finally,
        )
