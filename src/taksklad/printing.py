import logging
import os
import subprocess
import tempfile
from datetime import datetime

from PIL import Image, ImageDraw, ImageFont

from .config import (
    APP_NAME,
    DEFAULT_PIECES_PER_BLOCK,
    LABEL_DPI,
    LABEL_HEIGHT_MM,
    LABEL_WIDTH_MM,
)
from .storage import load_data_section, save_data_section
from .utils import normalize_text, parse_int_value

LABEL_SIZE_OPTIONS = (
    (100, 100),
    (100, 150),
    (75, 50),
    (58, 40),
)


def normalize_label_size(width, height):
    width = parse_int_value(width)
    height = parse_int_value(height)
    if (width, height) in LABEL_SIZE_OPTIONS:
        return width, height
    return LABEL_WIDTH_MM, LABEL_HEIGHT_MM


def label_size_to_text(width, height):
    width, height = normalize_label_size(width, height)
    return f"{width}x{height}"


def parse_label_size_text(value):
    text = normalize_text(value).lower().replace("х", "x").replace(" ", "")
    if "x" not in text:
        return LABEL_WIDTH_MM, LABEL_HEIGHT_MM
    width, height = text.split("x", 1)
    return normalize_label_size(width, height)


def list_available_printers():
    commands = []
    if os.name == "nt":
        commands.append([
            "powershell",
            "-NoProfile",
            "-Command",
            "Get-Printer | Select-Object -ExpandProperty Name",
        ])
        commands.append(["wmic", "printer", "get", "name"])
    else:
        commands.append(["lpstat", "-e"])

    for command in commands:
        try:
            completed = subprocess.run(command, capture_output=True, text=True, timeout=5, check=False)
        except Exception:
            continue
        if completed.returncode != 0:
            continue
        printers = []
        for line in completed.stdout.splitlines():
            name = normalize_text(line)
            if not name or name.lower() == "name":
                continue
            printers.append(name)
        if printers:
            return sorted(dict.fromkeys(printers))
    return []


def load_print_settings():
    defaults = {
        "printer_name": "Термопринтер",
        "label_width_mm": LABEL_WIDTH_MM,
        "label_height_mm": LABEL_HEIGHT_MM,
        "dpi": LABEL_DPI,
        "scale": "100%",
    }
    settings = load_data_section("print_settings", {})
    if isinstance(settings, dict):
        defaults.update({key: value for key, value in settings.items() if value not in (None, "")})
    return defaults

def save_print_settings(settings):
    return save_data_section("print_settings", settings)

def load_font(candidates, size):
    for path in candidates:
        try:
            return ImageFont.truetype(path, size=size)
        except:
            continue
    return ImageFont.load_default()

def wrap_text(text, max_chars):
    if len(text) <= max_chars:
        return [text]
    words = text.split()
    lines = []
    current_line = ""
    for word in words:
        if len(current_line) + len(word) + 1 <= max_chars:
            if current_line:
                current_line += " " + word
            else:
                current_line = word
        else:
            if current_line:
                lines.append(current_line)
            current_line = word
    if current_line:
        lines.append(current_line)
    return lines

def format_money(value):
    amount = parse_int_value(value)
    return f"{amount:,}".replace(",", " ") if amount else "0"

def product_total_price(product):
    explicit = parse_int_value(product.get("Сумма позиции")) or parse_int_value(product.get("Цена заказа"))
    if explicit:
        return explicit
    return parse_int_value(product.get("Отсканировано")) * 240000

def mm_to_px(mm_value, dpi=LABEL_DPI):
    return int(mm_value / 25.4 * dpi)

def powershell_quote(value):
    return "'" + str(value).replace("'", "''") + "'"

def send_image_to_windows_printer(file_path, printer_name="", label_width_mm=None, label_height_mm=None):
    image_path = os.path.abspath(file_path)
    printer_name = normalize_text(printer_name)
    label_width_mm, label_height_mm = normalize_label_size(label_width_mm, label_height_mm)
    paper_width = int(round(label_width_mm / 25.4 * 100))
    paper_height = int(round(label_height_mm / 25.4 * 100))
    paper_name = f"Label{label_width_mm}x{label_height_mm}"
    printer_line = ""
    if printer_name and printer_name != "Термопринтер":
        printer_line = f"$printDocument.PrinterSettings.PrinterName = {powershell_quote(printer_name)}"

    script = f"""
Add-Type -AssemblyName System.Drawing
$imagePath = {powershell_quote(image_path)}
$image = [System.Drawing.Image]::FromFile($imagePath)
$printDocument = New-Object System.Drawing.Printing.PrintDocument
{printer_line}
$printDocument.DocumentName = "{APP_NAME} summary"
$printDocument.DefaultPageSettings.PaperSize = New-Object System.Drawing.Printing.PaperSize("{paper_name}", {paper_width}, {paper_height})
$printDocument.DefaultPageSettings.Margins = New-Object System.Drawing.Printing.Margins(0, 0, 0, 0)
$printDocument.OriginAtMargins = $false
if (-not $printDocument.PrinterSettings.IsValid) {{
    throw "Printer is not valid: $($printDocument.PrinterSettings.PrinterName)"
}}
Write-Output "TakSklad printer: $($printDocument.PrinterSettings.PrinterName)"
$printDocument.add_PrintPage({{
    param($sender, $event)
    $event.Graphics.DrawImage($image, $event.PageBounds)
    $event.HasMorePages = $false
}})
try {{
    $printDocument.Print()
}} finally {{
    $image.Dispose()
    $printDocument.Dispose()
}}
"""
    ps_file = tempfile.NamedTemporaryFile(suffix=".ps1", delete=False, mode="w", encoding="utf-8")
    try:
        ps_file.write(script)
        ps_file.close()
        creationflags = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
        completed = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                ps_file.name,
            ],
            check=True,
            timeout=30,
            creationflags=creationflags,
            capture_output=True,
            text=True,
        )
        logging.info(
            "Сводка отправлена в Windows-печать: printer=%s, size=%sx%s, file=%s, stdout=%s, stderr=%s",
            printer_name or "Windows default",
            label_width_mm,
            label_height_mm,
            image_path,
            normalize_text(completed.stdout),
            normalize_text(completed.stderr),
        )
        return True
    finally:
        try:
            os.remove(ps_file.name)
        except OSError:
            pass

def send_image_to_printer(file_path, printer_name="", label_width_mm=None, label_height_mm=None):
    try:
        label_width_mm, label_height_mm = normalize_label_size(label_width_mm, label_height_mm)
        if os.name == 'nt':
            return send_image_to_windows_printer(
                file_path,
                printer_name=printer_name,
                label_width_mm=label_width_mm,
                label_height_mm=label_height_mm,
            )

        command = ["lp", "-o", f"media=Custom.{label_width_mm}x{label_height_mm}mm", file_path]
        if normalize_text(printer_name) and printer_name != "Термопринтер":
            command[1:1] = ["-d", printer_name]
        completed = subprocess.run(command, check=True, timeout=30, capture_output=True, text=True)
        logging.info(
            "Сводка отправлена на печать: command=%s stdout=%s stderr=%s",
            command,
            normalize_text(completed.stdout),
            normalize_text(completed.stderr),
        )
        return True
    except subprocess.CalledProcessError as exc:
        logging.exception(
            "Не удалось отправить сводку напрямую на печать: stdout=%s stderr=%s",
            normalize_text(getattr(exc, "stdout", "")),
            normalize_text(getattr(exc, "stderr", "")),
        )
        return False
    except Exception:
        logging.exception("Не удалось отправить сводку напрямую на печать")
        return False

def print_summary(address, all_products):
    try:
        if not all_products:
            return None

        print_settings = load_print_settings()
        label_width_mm, label_height_mm = normalize_label_size(
            print_settings.get("label_width_mm"),
            print_settings.get("label_height_mm"),
        )
        dpi = parse_int_value(print_settings.get("dpi")) or LABEL_DPI
        printer_name = normalize_text(print_settings.get("printer_name"))

        width_px = mm_to_px(label_width_mm, dpi=dpi)
        height_px = mm_to_px(label_height_mm, dpi=dpi)
        scale = dpi / 96

        def s(value):
            return int(value * scale)

        if label_height_mm >= 140:
            products_per_page = 5
        elif label_height_mm <= 50:
            products_per_page = 1
        else:
            products_per_page = 3
        pages = []
        for i in range(0, len(all_products), products_per_page):
            pages.append(all_products[i:i + products_per_page])

        printed_files = []

        for page_idx, page_products in enumerate(pages):
            img = Image.new("RGB", (width_px, height_px), "white")
            draw = ImageDraw.Draw(img)

            margin = s(10)
            inner_width = width_px - (margin * 2)
            inner_height = height_px - (margin * 2)

            draw.rectangle([margin, margin, width_px - margin, height_px - margin], outline="#333333", width=max(1, s(2)))

            font_title = load_font(["arialbd.ttf", "Arial Bold.ttf"], s(14))
            font_text = load_font(["arial.ttf", "Arial.ttf"], s(11))
            font_small = load_font(["arial.ttf", "Arial.ttf"], s(9))
            font_bold = load_font(["arialbd.ttf", "Arial Bold.ttf"], s(12))

            y = margin + s(8)
            x = margin + s(8)
            line_height = s(18)

            draw.text((x, y), f"СВОДНЫЙ ОТЧЁТ ПО АДРЕСУ", fill="black", font=font_title)
            y += line_height + s(5)

            addr_lines = wrap_text(address, 34)
            for line in addr_lines:
                draw.text((x, y), line, fill="black", font=font_small)
                y += line_height - s(2)
            y += s(5)

            if all_products:
                client = all_products[0].get('Клиент', '')
                draw.text((x, y), f"Клиент: {client[:35]}", fill="black", font=font_text)
                y += line_height

            if all_products:
                rep = all_products[0].get('Торговый представитель', '')
                draw.text((x, y), f"Торг.пред: {rep[:35]}", fill="black", font=font_text)
                y += line_height + s(5)

            total_order_price = sum(product_total_price(product) for product in all_products)
            if total_order_price:
                draw.text((x, y), f"Сумма заказа: {format_money(total_order_price)} сум", fill="black", font=font_bold)
                y += line_height + s(4)

            draw.line([(x, y), (x + inner_width - s(16), y)], fill="#cccccc", width=max(1, s(1)))
            y += s(10)

            page_text = f"Стр. {page_idx + 1} из {len(pages)}"
            draw.text((max(x, width_px - margin - s(60)), margin + s(8)), page_text, fill="gray", font=font_small)

            col_no = x
            col_product = x + max(s(22), int(inner_width * 0.08))
            col_blocks = x + int(inner_width * 0.62)
            col_pieces = x + int(inner_width * 0.80)
            product_chars = max(10, int((col_blocks - col_product) / max(1, s(6))))
            draw.text((x, y), "№", fill="black", font=font_bold)
            draw.text((col_product, y), "Товар", fill="black", font=font_bold)
            draw.text((col_blocks, y), "Блоков", fill="black", font=font_bold)
            draw.text((col_pieces, y), "ШТ", fill="black", font=font_bold)
            y += line_height + s(3)
            draw.line([(x, y - s(5)), (x + inner_width - s(16), y - s(5))], fill="#000000", width=max(1, s(1)))

            total_blocks = 0
            total_shields = 0

            for idx, product in enumerate(page_products):
                product_name = product.get('Товары', '')[:product_chars]
                blocks = product.get('Отсканировано', 0)
                pieces_per_block = parse_int_value(product.get("Кол-во ШТ в блоке")) or DEFAULT_PIECES_PER_BLOCK
                shields = blocks * pieces_per_block
                total_blocks += blocks
                total_shields += shields

                draw.text((col_no, y), f"{idx + 1 + (page_idx * products_per_page)}", fill="black", font=font_text)
                draw.text((col_product, y), product_name, fill="black", font=font_text)
                draw.text((col_blocks, y), str(blocks), fill="black", font=font_text)
                draw.text((col_pieces, y), str(shields), fill="black", font=font_text)
                y += line_height

                if y > height_px - s(60) and idx < len(page_products) - 1:
                    draw.text((x, height_px - margin - s(12)), datetime.now().strftime("%d.%m.%Y %H:%M"), fill="gray", font=font_small)
                    break

            draw.line([(x, y), (x + inner_width - s(16), y)], fill="#cccccc", width=max(1, s(1)))
            y += s(8)

            draw.text((x, y), f"ИТОГО:", fill="black", font=font_bold)
            draw.text((col_blocks, y), str(total_blocks), fill="black", font=font_bold)
            draw.text((col_pieces, y), str(total_shields), fill="black", font=font_bold)

            draw.text((x, height_px - margin - s(12)), datetime.now().strftime("%d.%m.%Y %H:%M"), fill="gray", font=font_small)

            temp_file = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
            img.save(temp_file.name, dpi=(dpi, dpi))
            temp_file.close()
            printed_files.append(temp_file.name)

            if not send_image_to_printer(
                temp_file.name,
                printer_name=printer_name,
                label_width_mm=label_width_mm,
                label_height_mm=label_height_mm,
            ):
                return None

        return printed_files
    except Exception as e:
        logging.exception("Ошибка печати сводного листа")
        return None
