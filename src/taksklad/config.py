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
    return os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, os.pardir))


def _int_env(name, default):
    try:
        return int(os.environ.get(name, str(default)) or default)
    except (TypeError, ValueError):
        return default


APP_DIR = get_app_dir()
CREDENTIALS_FILE = os.path.join(APP_DIR, "credentials.json")
TAKSKLAD_DATA_FILE = os.path.join(APP_DIR, "TakSklad_data.json")
# Логи приложения держим в подпапке docs/ рядом с changelog'ом и проектной
# документацией — единое место для всего, что относится к диагностике и
# истории проекта. Файлы .log .gitignored, .md остаются в git.
LOG_DIR = os.path.join(APP_DIR, "docs")
LOG_FILE = os.path.join(LOG_DIR, "TakSklad.log")
UPDATE_LOG_FILE = os.path.join(LOG_DIR, "TakSklad_update.log")
LOG_MAX_BYTES = _int_env("TAKSKLAD_LOG_MAX_BYTES", 5 * 1024 * 1024)
LOG_BACKUP_COUNT = _int_env("TAKSKLAD_LOG_BACKUP_COUNT", 5)
BACKUP_DIR = os.path.join(APP_DIR, "scan_backups")
REPORTS_DIR = os.path.join(APP_DIR, "reports")
PENDING_PRINTS_FILE = os.path.join(APP_DIR, "pending_prints.json")
PENDING_SAVES_FILE = os.path.join(APP_DIR, "pending_saves.json")
PENDING_TELEGRAM_FILE = os.path.join(APP_DIR, "pending_telegram.json")
PENDING_BACKEND_EVENTS_FILE = os.path.join(APP_DIR, "pending_backend_events.json")
TELEGRAM_STATE_FILE = os.path.join(APP_DIR, "telegram_state.json")
PRINT_SETTINGS_FILE = os.path.join(APP_DIR, "print_settings.json")
PRODUCT_CATALOG_FILE = os.path.join(APP_DIR, "product_catalog.json")
IMPORT_HISTORY_FILE = os.path.join(APP_DIR, "import_history.json")
TELEGRAM_SETTINGS_FILE = os.path.join(APP_DIR, "telegram_settings.json")
YANDEX_GEOCODER_KEY_FILE = os.path.join(APP_DIR, "yandex_geocoder_key.txt")
YANDEX_GEOCODER_ENV_VAR = "YANDEX_GEOCODER_API_KEY"

APP_VERSION = "2.0.0"
APP_BUILD_LABEL = os.environ.get("TAKSKLAD_BUILD_LABEL", "MVP 2.0").strip()
UPDATE_INFO_URL = os.environ.get(
    "TAKSKLAD_UPDATE_INFO_URL",
    "https://raw.githubusercontent.com/1fear/TakSklad/main/version.json",
).strip()
UPDATE_CHECK_TIMEOUT_SECONDS = 8
UPDATE_DOWNLOAD_TIMEOUT_SECONDS = 120
# Если установка обновления упала или пользователь отказался — не дёргать
# апдейтер для той же версии чаще одного раза в час. Иначе получаем цикл
# «упало → старый exe → снова проверка → снова упало».
UPDATE_RETRY_COOLDOWN_SECONDS = 60 * 60
GOOGLE_API_TIMEOUT_SECONDS = 30
GOOGLE_RETRY_COOLDOWN_SECONDS = 60
GOOGLE_BACKOFF_LOG_INTERVAL_SECONDS = 30
TELEGRAM_FILE_DOWNLOAD_TIMEOUT_SECONDS = 120
EXCEL_IMPORT_EXTENSIONS = {".xlsx", ".xlsm"}
TELEGRAM_SINGLE_LISTENER_LOCK_ENABLED = True
TELEGRAM_LOCK_SHEET_NAME = "_TakSklad_System"
TELEGRAM_LOCK_KEY = "telegram_poll"
TELEGRAM_LOCK_TTL_SECONDS = 60
TELEGRAM_LOCK_REFRESH_SECONDS = 20
TELEGRAM_LOCK_RETRY_SECONDS = 15

TAKSKLAD_BACKEND_ENABLED = os.environ.get("TAKSKLAD_BACKEND_ENABLED", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
    "да",
}
TAKSKLAD_BACKEND_READ_ORDERS_ENABLED = os.environ.get(
    "TAKSKLAD_BACKEND_READ_ORDERS_ENABLED",
    "",
).strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
    "да",
}
TAKSKLAD_BACKEND_BASE_URL = os.environ.get("TAKSKLAD_BACKEND_BASE_URL", "").strip().rstrip("/")
TAKSKLAD_BACKEND_API_TOKEN = os.environ.get("TAKSKLAD_BACKEND_API_TOKEN", "").strip()
TAKSKLAD_BACKEND_TIMEOUT_SECONDS = int(os.environ.get("TAKSKLAD_BACKEND_TIMEOUT_SECONDS", "8") or "8")

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
SKLADBOT_API_TIMEOUT_SECONDS = 8
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
SKLADBOT_SYNC_LOOKBACK_DAYS = 14
SKLADBOT_REQUESTS_LIMIT = 500
SKLADBOT_COMPLETED_DETAIL_LIMIT = 500
SKLADBOT_REQUEST_DELAY_SECONDS = 0.05
SKLADBOT_SYNC_INTERVAL_MS = 60 * 1000
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

BG_MAIN = "#f7f5df"
BG_CARD = "#ffffff"
FG_TEXT = "#111111"
FG_MUTED = "#5f5f5f"
ACCENT = "#111111"
SUCCESS = "#111111"
INFO = "#111111"
WARNING = "#F0E68C"
DANGER = "#8b1e1e"
ERROR_BG = "#fee2e2"
ERROR_FG = "#dc2626"
BORDER = "#d8d1a1"
DISABLED_BG = "#d9d6bf"
DISABLED_FG = "#8a8774"

__all__ = [name for name in globals() if name.isupper()] + ["get_app_dir"]
