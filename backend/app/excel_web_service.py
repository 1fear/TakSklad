import tempfile
from pathlib import Path
from urllib.parse import unquote

from .excel_importer import excel_file_to_import_payload
from .schemas import ImportCreate
from .spreadsheet_safety import MAX_XLSX_FILE_BYTES, SpreadsheetSafetyError, normalize_spreadsheet_filename


def parse_raw_excel_upload(content: bytes, filename: str, *, source="web") -> tuple[ImportCreate, dict]:
    safe_name = normalize_spreadsheet_filename(unquote(str(filename or "")))
    if not content:
        raise SpreadsheetSafetyError("file_empty")
    if len(content) > MAX_XLSX_FILE_BYTES:
        raise SpreadsheetSafetyError("file_size_exceeded")
    suffix = Path(safe_name).suffix.lower()
    with tempfile.NamedTemporaryFile(prefix="taksklad-web-import-", suffix=suffix) as handle:
        handle.write(content)
        handle.flush()
        parsed = excel_file_to_import_payload(handle.name, file_name=safe_name, source=source)
    meta = dict(parsed.pop("meta", {}) or {})
    return ImportCreate(**parsed), meta
