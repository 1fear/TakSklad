import os
import sys

SPREADSHEET_ID = "1hisRZ667qEhsRTfoPzv4r78naYhc9kdzhkmUKvZEUr8"
SHEET_NAME = "data"
APP_NAME = "TakSklad"
APP_EXECUTABLE_NAME = "TakSklad.exe"
APP_RELEASE_ZIP_NAME = "TakSklad-windows-x64.zip"


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

APP_VERSION = "1.1.12"
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

SKLADBOT_API_BASE_URL = os.environ.get("SKLADBOT_API_BASE_URL", "https://api.skladbot.ru/v1").strip()
SKLADBOT_REQUEST_NUMBER_COLUMN = "Номер заявки SkladBot"
SKLADBOT_REQUEST_ID_COLUMN = "ID заявки SkladBot"
SKLADBOT_STATUS_COLUMN = "Статус SkladBot"
SKLADBOT_CHECKED_AT_COLUMN = "Последняя проверка SkladBot"
SKLADBOT_STATUS_FOUND = "Найдено"
SKLADBOT_STATUS_NOT_FOUND = "Не найдено"
SKLADBOT_STATUS_MULTIPLE = "Несколько совпадений"
SKLADBOT_STATUS_SYNC_ERROR = "Ошибка синхронизации"
SKLADBOT_CUSTOMER_ID = 6211
SKLADBOT_CUSTOMER_NAME = "ООО Bastion Import Chapman MCHJ"
SKLADBOT_SHIPMENT_TYPE_ID = 3389
SKLADBOT_SHIPMENT_TYPE_NAME = "Отгрузка 3PL"
SKLADBOT_COMPLETED_LOOKBACK_DAYS = 2
SKLADBOT_REQUESTS_LIMIT = 500
SKLADBOT_COMPLETED_DETAIL_LIMIT = 500
SKLADBOT_REQUEST_DELAY_SECONDS = 0.05
SKLADBOT_SYNC_INTERVAL_MS = 10 * 60 * 1000
DAILY_REPORT_AUTO_SEND_HOUR = 23
DAILY_REPORT_AUTO_SEND_MINUTE = 55
DAILY_REPORT_CHECK_INTERVAL_MS = 5 * 60 * 1000

SERVICE_COLUMNS = [
    "ID заказа",
    "ID импорта",
    "Источник файла",
    "Строка файла",
    "Дата импорта",
    SKLADBOT_REQUEST_NUMBER_COLUMN,
    SKLADBOT_REQUEST_ID_COLUMN,
    SKLADBOT_STATUS_COLUMN,
    SKLADBOT_CHECKED_AT_COLUMN,
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
