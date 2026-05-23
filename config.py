import os
import sys

SPREADSHEET_ID = "1soHrN7Iqd3jk9iLGdUGK9APxVfRBwWXHxoI8x2Hsh1o"
SHEET_NAME = "data"
CHAPMAN_DATA_SHEET_NAME = "Данные"
APP_NAME = "TakSklad"
APP_EXECUTABLE_NAME = "TakSklad.exe"


def get_app_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


APP_DIR = get_app_dir()
CREDENTIALS_FILE = os.path.join(APP_DIR, "credentials.json")
TAKSKLAD_DATA_FILE = os.path.join(APP_DIR, "TakSklad_data.json")
LOG_FILE = os.path.join(APP_DIR, "TakSklad.log")
BACKUP_DIR = os.path.join(APP_DIR, "scan_backups")
REPORTS_DIR = os.path.join(APP_DIR, "reports")
PENDING_PRINTS_FILE = os.path.join(APP_DIR, "pending_prints.json")
PENDING_SAVES_FILE = os.path.join(APP_DIR, "pending_saves.json")
PENDING_TELEGRAM_FILE = os.path.join(APP_DIR, "pending_telegram.json")
TELEGRAM_STATE_FILE = os.path.join(APP_DIR, "telegram_state.json")
PRINT_SETTINGS_FILE = os.path.join(APP_DIR, "print_settings.json")
PRODUCT_CATALOG_FILE = os.path.join(APP_DIR, "product_catalog.json")
IMPORT_HISTORY_FILE = os.path.join(APP_DIR, "import_history.json")
TELEGRAM_SETTINGS_FILE = os.path.join(APP_DIR, "telegram_settings.json")
YANDEX_GEOCODER_KEY_FILE = os.path.join(APP_DIR, "yandex_geocoder_key.txt")
YANDEX_GEOCODER_API_KEY = "7c455cc8-0cda-46da-ac5c-e32297c2fec0"

APP_VERSION = "1.1.6"
UPDATE_INFO_URL = os.environ.get(
    "PKIS_UPDATE_INFO_URL",
    "https://raw.githubusercontent.com/1fear/pKIS/main/version.json",
).strip()
UPDATE_CHECK_TIMEOUT_SECONDS = 8
UPDATE_DOWNLOAD_TIMEOUT_SECONDS = 120
TELEGRAM_FILE_DOWNLOAD_TIMEOUT_SECONDS = 120
EXCEL_IMPORT_EXTENSIONS = {".xlsx", ".xlsm"}

ORDER_DATE_COLUMN = "Дата отгрузки"
LEGACY_ORDER_DATE_COLUMN = "Дата получения заказа"

REQUIRED_COLUMNS = [
    ORDER_DATE_COLUMN,
    "Тип оплаты",
    "Клиент",
    "Адрес",
    "Торговый представитель",
    "Товары",
    "Кол-во ШТ",
    "Кол-во блок",
    "Отсканированные коды",
]

STATUS_COLUMN = "Статус"
STATUS_NOT_COMPLETED = "Не выполнено"
STATUS_COMPLETED = "Выполнено"

WORKING_COLUMNS = REQUIRED_COLUMNS + [STATUS_COLUMN]

SERVICE_COLUMNS = [
    "ID заказа",
    "ID импорта",
    "Источник файла",
    "Строка файла",
    "Дата импорта",
]

SERVICE_COLUMN_START_INDEX = 26  # AA, zero-based

SOURCE_REQUIRED_ALIASES = {
    "client": ["ФИО или Наименование торговой точки", "Клиент", "Юр. лицо", "Юр лицо", "Наименование"],
    "payment": ["Тип оплаты", "Оплата"],
    "product": ["Наименование Товара", "Товары", "Товар", "Номенклатура"],
    "quantity": ["Кол-во", "Количество", "Кол-во ШТ", "Количество ШТ"],
}

SOURCE_OPTIONAL_ALIASES = {
    "date": ["Дата доставки", "Дата отгрузки", "Дата получения заказа", "Дата заказа", "Дата"],
    "address": ["Адрес доставки", "Адрес"],
    "coords": ["Координаты", "Координаты доставки"],
    "representative": ["Торговый представитель", "ТП", "Менеджер", "Номер телефона"],
    "inn": ["ИНН клиента", "ИНН Клиента", "ИНН", "ИНН контрагента"],
    "lead_status": ["Статус заказа(Тип лида)", "Статус заказа (Тип лида)", "Тип лида"],
}

CHAPMAN_DATA_LEAD_STATUS = "Отгрузка клиенту"
CHAPMAN_DATA_VISIBLE_COLUMN_COUNT = 24
CHAPMAN_DATA_FORMAT_COLUMN_COUNT = 34

DEFAULT_PIECES_PER_BLOCK = 10

LABEL_WIDTH_MM = 100
LABEL_HEIGHT_MM = 100
LABEL_DPI = 203
KIZ_MIN_LENGTH = 20
KIZ_MAX_LENGTH = 120

BG_MAIN = "#f5f7fa"
BG_CARD = "#ffffff"
FG_TEXT = "#1a1f2e"
FG_MUTED = "#6b7280"
ACCENT = "#4f46e5"
SUCCESS = "#10b981"
INFO = "#3b82f6"
WARNING = "#f59e0b"
DANGER = "#ef4444"
ERROR_BG = "#fee2e2"
ERROR_FG = "#dc2626"
BORDER = "#e5e7eb"
DISABLED_BG = "#e5e7eb"
DISABLED_FG = "#94a3b8"

__all__ = [name for name in globals() if name.isupper()] + ["get_app_dir"]
