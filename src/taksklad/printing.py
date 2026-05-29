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

def mm_to_px(mm_value):
    return int(mm_value / 25.4 * LABEL_DPI)

def powershell_quote(value):
    return "'" + str(value).replace("'", "''") + "'"

def send_image_to_windows_printer(file_path, printer_name=""):
    image_path = os.path.abspath(file_path)
    printer_name = normalize_text(printer_name)
    paper_width = int(round(LABEL_WIDTH_MM / 25.4 * 100))
    paper_height = int(round(LABEL_HEIGHT_MM / 25.4 * 100))
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
$printDocument.DefaultPageSettings.PaperSize = New-Object System.Drawing.Printing.PaperSize("Label100x100", {paper_width}, {paper_height})
$printDocument.DefaultPageSettings.Margins = New-Object System.Drawing.Printing.Margins(0, 0, 0, 0)
$printDocument.OriginAtMargins = $false
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
        subprocess.run(
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
        )
        return True
    finally:
        try:
            os.remove(ps_file.name)
        except OSError:
            pass

def send_image_to_printer(file_path, printer_name=""):
    try:
        if os.name == 'nt':
            return send_image_to_windows_printer(file_path, printer_name=printer_name)

        command = ["lp", "-o", f"media=Custom.{LABEL_WIDTH_MM}x{LABEL_HEIGHT_MM}mm", file_path]
        if normalize_text(printer_name) and printer_name != "Термопринтер":
            command[1:1] = ["-d", printer_name]
        subprocess.run(command, check=True, timeout=30)
        return True
    except Exception:
        logging.exception("Не удалось отправить сводку напрямую на печать")
        return False

def print_summary(address, all_products):
    try:
        if not all_products:
            return None

        width_px = mm_to_px(LABEL_WIDTH_MM)
        height_px = mm_to_px(LABEL_HEIGHT_MM)
        scale = LABEL_DPI / 96

        def s(value):
            return int(value * scale)

        products_per_page = 3
        pages = []
        for i in range(0, len(all_products), products_per_page):
            pages.append(all_products[i:i + products_per_page])

        print_settings = load_print_settings()
        printer_name = normalize_text(print_settings.get("printer_name"))
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

            draw.line([(x, y), (x + inner_width - s(16), y)], fill="#cccccc", width=max(1, s(1)))
            y += s(10)

            page_text = f"Стр. {page_idx + 1} из {len(pages)}"
            draw.text((width_px - margin - s(60), margin + s(8)), page_text, fill="gray", font=font_small)

            draw.text((x, y), "№", fill="black", font=font_bold)
            draw.text((x + s(25), y), "Товар", fill="black", font=font_bold)
            draw.text((x + s(200), y), "Блоков", fill="black", font=font_bold)
            draw.text((x + s(260), y), "ШТ", fill="black", font=font_bold)
            y += line_height + s(3)
            draw.line([(x, y - s(5)), (x + inner_width - s(16), y - s(5))], fill="#000000", width=max(1, s(1)))

            total_blocks = 0
            total_shields = 0

            for idx, product in enumerate(page_products):
                product_name = product.get('Товары', '')[:22]
                blocks = product.get('Отсканировано', 0)
                pieces_per_block = parse_int_value(product.get("Кол-во ШТ в блоке")) or DEFAULT_PIECES_PER_BLOCK
                shields = blocks * pieces_per_block
                total_blocks += blocks
                total_shields += shields

                draw.text((x, y), f"{idx + 1 + (page_idx * products_per_page)}", fill="black", font=font_text)
                draw.text((x + s(25), y), product_name, fill="black", font=font_text)
                draw.text((x + s(205), y), str(blocks), fill="black", font=font_text)
                draw.text((x + s(265), y), str(shields), fill="black", font=font_text)
                y += line_height

                if y > height_px - s(60) and idx < len(page_products) - 1:
                    draw.text((x, height_px - margin - s(12)), datetime.now().strftime("%d.%m.%Y %H:%M"), fill="gray", font=font_small)
                    break

            draw.line([(x, y), (x + inner_width - s(16), y)], fill="#cccccc", width=max(1, s(1)))
            y += s(8)

            draw.text((x, y), f"ИТОГО:", fill="black", font=font_bold)
            draw.text((x + s(205), y), str(total_blocks), fill="black", font=font_bold)
            draw.text((x + s(265), y), str(total_shields), fill="black", font=font_bold)

            draw.text((x, height_px - margin - s(12)), datetime.now().strftime("%d.%m.%Y %H:%M"), fill="gray", font=font_small)

            temp_file = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
            img.save(temp_file.name, dpi=(LABEL_DPI, LABEL_DPI))
            temp_file.close()
            printed_files.append(temp_file.name)

            if not send_image_to_printer(temp_file.name, printer_name=printer_name):
                return None

        return printed_files
    except Exception as e:
        logging.exception("Ошибка печати сводного листа")
        return None

