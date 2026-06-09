from tkinter import filedialog, messagebox

from .backend_client import backend_enabled, import_orders
from .catalog import load_product_catalog
from .config import APP_NAME, BG_MAIN, FG_MUTED
from .excel_import import append_import_records, parse_excel_order_files, prepare_excel_import
from .utils import parse_int_value


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
            if backend_enabled():
                parsed = parse_excel_order_files(list(file_paths))
                records = parsed.get("records", [])
                parsed["new_records"] = records
                parsed["duplicate_records"] = []
                parsed["clients_count"] = len({record.get("Клиент") for record in records})
                parsed["products_count"] = len({record.get("Товары") for record in records})
                parsed["blocks_count"] = sum(parse_int_value(record.get("Кол-во блок")) for record in records)
                parsed["quantity_count"] = sum(parse_int_value(record.get("Кол-во ШТ")) for record in records)
                parsed["backend_import"] = True
                return parsed
            return prepare_excel_import(list(file_paths))

        def on_success(preview):
            self.clear_busy()
            self.safe_config(self.import_btn, state="normal")
            self.safe_config(self.refresh_btn, state="normal")

            errors = preview.get("errors", [])
            warnings = preview.get("warnings", [])
            new_records = preview.get("new_records", [])
            duplicate_records = preview.get("duplicate_records", [])
            source_duplicate_rows = preview.get("source_duplicate_rows_count", 0)

            if not new_records:
                details = [
                    f"Файлов проверено: {preview.get('files_count', 0)}",
                    f"Строк в файлах: {preview.get('source_rows_count', 0)}",
                    f"Адресов получено из координат: {preview.get('geocoded_count', 0)}",
                    f"Координат без адреса: {preview.get('geocode_failed_count', 0)}",
                    f"Повторных строк в Excel: {source_duplicate_rows}",
                    f"Дублей в таблице найдено: {len(duplicate_records)}",
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
            ]
            if errors:
                message_lines.extend(["", "Ошибки в отдельных файлах:", "\n".join(errors[:5])])
            if warnings:
                message_lines.extend(["", "Предупреждения:", "\n".join(warnings[:5])])
            target_name = "backend" if preview.get("backend_import") else "Google Sheets"
            message_lines.extend(["", f"Загрузить новые позиции в {target_name}?"])

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
        target_name = "backend" if backend_enabled() else "Google Sheets"
        self.set_busy(f"⏳ Загружаю заказы в {target_name}...")
        self.safe_config(self.import_btn, state="disabled")
        self.safe_config(self.refresh_btn, state="disabled")

        def work():
            if backend_enabled():
                imported_sources = sorted({record.get("Источник файла", "") for record in records if record.get("Источник файла")})
                result = import_orders(records, filename=", ".join(imported_sources[:5]))
                result = {
                    "imported": result.get("rows_imported", 0),
                    "duplicates": result.get("duplicate_rows", 0),
                    "backend": result,
                }
            else:
                result = append_import_records(records)
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
