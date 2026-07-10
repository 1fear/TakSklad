"""Bounded XLSX loading and spreadsheet-output safety helpers.

This module mirrors ``backend.app.spreadsheet_safety`` because the desktop and
backend distributions package different source roots.
"""

from __future__ import annotations

import io
import os
import re
import stat
import unicodedata
import zipfile
from pathlib import Path
from xml.etree import ElementTree


MAX_XLSX_FILE_BYTES = 20 * 1024 * 1024
MAX_XLSX_COMPRESSED_BYTES = MAX_XLSX_FILE_BYTES
MAX_XLSX_UNCOMPRESSED_BYTES = 80 * 1024 * 1024
MAX_XLSX_ENTRY_BYTES = 48 * 1024 * 1024
MAX_XLSX_COMPRESSION_RATIO = 200
MAX_XLSX_ENTRIES = 2048
MAX_XLSX_DATA_ROWS = 5000
MAX_XLSX_ROWS = MAX_XLSX_DATA_ROWS + 1
MAX_XLSX_COLUMNS = 128
MAX_XLSX_CELL_CHARS = 16384
MAX_XLSX_FILENAME_CHARS = 128
MAX_XLSX_FILENAME_BYTES = 255
SUPPORTED_XLSX_EXTENSIONS = frozenset({".xlsx", ".xlsm"})

_CELL_REFERENCE = re.compile(r"^\$?([A-Za-z]+)\$?([1-9][0-9]*)$")
_WORKSHEET_PATTERN = re.compile(r"^xl/worksheets/[^/]+\.xml$")
_REQUIRED_MEMBERS = frozenset({"[Content_Types].xml", "xl/workbook.xml"})
_FORMULA_PREFIXES = ("=", "+", "-", "@")


class SpreadsheetSafetyError(ValueError):
    """A redacted, deterministic rejection raised before workbook parsing."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(f"spreadsheet_rejected:{code}")


def normalize_spreadsheet_filename(file_name: object) -> str:
    """Return a normalized leaf filename or reject it with a fixed error code."""

    if not isinstance(file_name, (str, os.PathLike)):
        raise SpreadsheetSafetyError("filename_invalid")
    normalized = unicodedata.normalize("NFKC", os.fspath(file_name)).strip()
    if not normalized or normalized in {".", ".."}:
        raise SpreadsheetSafetyError("filename_invalid")
    if len(normalized) > MAX_XLSX_FILENAME_CHARS or len(normalized.encode("utf-8")) > MAX_XLSX_FILENAME_BYTES:
        raise SpreadsheetSafetyError("filename_too_long")
    if any(unicodedata.category(character).startswith("C") for character in normalized):
        raise SpreadsheetSafetyError("filename_control_character")
    if "/" in normalized or "\\" in normalized or Path(normalized).name != normalized:
        raise SpreadsheetSafetyError("filename_traversal")
    if normalized.startswith(("~",)) or re.match(r"^[A-Za-z]:", normalized):
        raise SpreadsheetSafetyError("filename_traversal")
    if Path(normalized).suffix.casefold() not in SUPPORTED_XLSX_EXTENSIONS:
        raise SpreadsheetSafetyError("filename_extension")
    return normalized


def _read_bounded_file(file_path: str | os.PathLike[str]) -> bytes:
    try:
        size = os.path.getsize(file_path)
    except (OSError, TypeError, ValueError) as exc:
        raise SpreadsheetSafetyError("file_unreadable") from exc
    if size <= 0:
        raise SpreadsheetSafetyError("file_empty")
    if size > MAX_XLSX_FILE_BYTES:
        raise SpreadsheetSafetyError("compressed_size_exceeded")
    try:
        with open(file_path, "rb") as handle:
            content = handle.read(MAX_XLSX_FILE_BYTES + 1)
    except OSError as exc:
        raise SpreadsheetSafetyError("file_unreadable") from exc
    if len(content) > MAX_XLSX_FILE_BYTES:
        raise SpreadsheetSafetyError("compressed_size_exceeded")
    return content


def _safe_member_name(name: str) -> str:
    normalized = unicodedata.normalize("NFKC", name)
    if not normalized or len(normalized) > 512:
        raise SpreadsheetSafetyError("archive_path_invalid")
    if "\\" in normalized or normalized.startswith("/") or "//" in normalized:
        raise SpreadsheetSafetyError("archive_path_traversal")
    if any(unicodedata.category(character).startswith("C") for character in normalized):
        raise SpreadsheetSafetyError("archive_path_invalid")
    leafless = normalized[:-1] if normalized.endswith("/") else normalized
    parts = leafless.split("/")
    if not leafless or any(part in {"", ".", ".."} for part in parts):
        raise SpreadsheetSafetyError("archive_path_traversal")
    if re.match(r"^[A-Za-z]:", parts[0]):
        raise SpreadsheetSafetyError("archive_path_traversal")
    return normalized


def _column_number(letters: str) -> int:
    result = 0
    for letter in letters.upper():
        if not "A" <= letter <= "Z":
            raise SpreadsheetSafetyError("worksheet_reference_invalid")
        result = result * 26 + ord(letter) - ord("A") + 1
        if result > MAX_XLSX_COLUMNS:
            raise SpreadsheetSafetyError("columns_exceeded")
    return result


def _validate_reference(reference: str) -> tuple[int, int]:
    match = _CELL_REFERENCE.fullmatch(reference or "")
    if match is None:
        raise SpreadsheetSafetyError("worksheet_reference_invalid")
    column = _column_number(match.group(1))
    row = int(match.group(2))
    if row > MAX_XLSX_ROWS:
        raise SpreadsheetSafetyError("rows_exceeded")
    return row, column


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _inspect_shared_strings(archive: zipfile.ZipFile, member: str) -> None:
    current_length: int | None = None
    try:
        with archive.open(member) as stream:
            for event, element in ElementTree.iterparse(stream, events=("start", "end")):
                tag = _local_name(element.tag)
                if event == "start" and tag == "si":
                    current_length = 0
                elif event == "end" and tag == "t" and current_length is not None:
                    current_length += len(element.text or "")
                    if current_length > MAX_XLSX_CELL_CHARS:
                        raise SpreadsheetSafetyError("cell_length_exceeded")
                elif event == "end" and tag == "si":
                    current_length = None
                if event == "end":
                    element.clear()
    except SpreadsheetSafetyError:
        raise
    except (ElementTree.ParseError, OSError, RuntimeError, zipfile.BadZipFile) as exc:
        raise SpreadsheetSafetyError("archive_xml_invalid") from exc


def _inspect_worksheet(archive: zipfile.ZipFile, member: str) -> None:
    row_elements = 0
    cells_in_row = 0
    cell_length: int | None = None
    try:
        with archive.open(member) as stream:
            for event, element in ElementTree.iterparse(stream, events=("start", "end")):
                tag = _local_name(element.tag)
                if event == "start" and tag == "dimension":
                    endpoint = (element.attrib.get("ref") or "").split(":")[-1]
                    if endpoint:
                        _validate_reference(endpoint)
                elif event == "start" and tag == "row":
                    row_elements += 1
                    cells_in_row = 0
                    if row_elements > MAX_XLSX_ROWS:
                        raise SpreadsheetSafetyError("rows_exceeded")
                    row_reference = element.attrib.get("r")
                    if row_reference:
                        try:
                            if int(row_reference) > MAX_XLSX_ROWS:
                                raise SpreadsheetSafetyError("rows_exceeded")
                        except ValueError as exc:
                            raise SpreadsheetSafetyError("worksheet_reference_invalid") from exc
                elif event == "start" and tag == "c":
                    cells_in_row += 1
                    if cells_in_row > MAX_XLSX_COLUMNS:
                        raise SpreadsheetSafetyError("columns_exceeded")
                    reference = element.attrib.get("r")
                    if reference:
                        _validate_reference(reference)
                    cell_length = 0
                elif event == "end" and tag in {"t", "v", "f"} and cell_length is not None:
                    cell_length += len(element.text or "")
                    if cell_length > MAX_XLSX_CELL_CHARS:
                        raise SpreadsheetSafetyError("cell_length_exceeded")
                elif event == "end" and tag == "c":
                    cell_length = None
                if event == "end":
                    element.clear()
    except SpreadsheetSafetyError:
        raise
    except (ElementTree.ParseError, OSError, RuntimeError, zipfile.BadZipFile) as exc:
        raise SpreadsheetSafetyError("archive_xml_invalid") from exc


def _inspect_xlsx_content(content: bytes) -> dict[str, int | float]:
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            members = archive.infolist()
            if not members:
                raise SpreadsheetSafetyError("archive_empty")
            if len(members) > MAX_XLSX_ENTRIES:
                raise SpreadsheetSafetyError("archive_entries_exceeded")

            names: set[str] = set()
            compressed_total = 0
            uncompressed_total = 0
            worksheet_names: list[str] = []
            for info in members:
                safe_name = _safe_member_name(info.filename)
                if safe_name in names:
                    raise SpreadsheetSafetyError("archive_duplicate_entry")
                names.add(safe_name)
                if info.flag_bits & 0x1:
                    raise SpreadsheetSafetyError("archive_encrypted")
                mode = (info.external_attr >> 16) & 0xFFFF
                if mode and stat.S_ISLNK(mode):
                    raise SpreadsheetSafetyError("archive_path_invalid")
                if info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
                    raise SpreadsheetSafetyError("archive_compression_invalid")
                if info.file_size > MAX_XLSX_ENTRY_BYTES:
                    raise SpreadsheetSafetyError("archive_entry_size_exceeded")
                compressed_total += info.compress_size
                uncompressed_total += info.file_size
                if compressed_total > MAX_XLSX_COMPRESSED_BYTES:
                    raise SpreadsheetSafetyError("compressed_size_exceeded")
                if uncompressed_total > MAX_XLSX_UNCOMPRESSED_BYTES:
                    raise SpreadsheetSafetyError("uncompressed_size_exceeded")
                if info.file_size >= 64 * 1024:
                    entry_ratio = info.file_size / max(info.compress_size, 1)
                    if entry_ratio > MAX_XLSX_COMPRESSION_RATIO:
                        raise SpreadsheetSafetyError("compression_ratio_exceeded")
                if _WORKSHEET_PATTERN.fullmatch(safe_name):
                    worksheet_names.append(info.filename)

            if not _REQUIRED_MEMBERS.issubset(names) or not worksheet_names:
                raise SpreadsheetSafetyError("archive_structure_invalid")
            ratio = uncompressed_total / max(compressed_total, 1)
            if ratio > MAX_XLSX_COMPRESSION_RATIO:
                raise SpreadsheetSafetyError("compression_ratio_exceeded")

            if "xl/sharedStrings.xml" in names:
                _inspect_shared_strings(archive, "xl/sharedStrings.xml")
            for worksheet_name in worksheet_names:
                _inspect_worksheet(archive, worksheet_name)
    except SpreadsheetSafetyError:
        raise
    except (OSError, RuntimeError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        raise SpreadsheetSafetyError("archive_invalid") from exc

    return {
        "compressed_bytes": len(content),
        "uncompressed_bytes": uncompressed_total,
        "entries": len(members),
        "compression_ratio": ratio,
    }


def inspect_xlsx_archive(file_path: str | os.PathLike[str]) -> dict[str, int | float]:
    """Inspect ZIP metadata and bounded workbook XML without invoking openpyxl."""

    return _inspect_xlsx_content(_read_bounded_file(file_path))


def load_safe_workbook(
    file_path: str | os.PathLike[str],
    *,
    file_name: object | None = None,
    data_only: bool = True,
    read_only: bool = True,
):
    """Validate an XLSX/XLSM and only then pass the same bounded bytes to openpyxl."""

    normalize_spreadsheet_filename(file_name if file_name is not None else Path(file_path).name)
    content = _read_bounded_file(file_path)
    _inspect_xlsx_content(content)
    try:
        import openpyxl

        return openpyxl.load_workbook(io.BytesIO(content), data_only=data_only, read_only=read_only)
    except Exception as exc:
        raise SpreadsheetSafetyError("workbook_parse_failed") from exc


def force_workbook_text_literals(workbook) -> int:
    """Mark formula-prefix strings as text before an XLSX writer saves them."""

    changed = 0
    for worksheet in workbook.worksheets:
        for row in worksheet.iter_rows():
            for cell in row:
                if isinstance(cell.value, str) and cell.value.startswith(_FORMULA_PREFIXES):
                    cell.data_type = "s"
                    changed += 1
    return changed
