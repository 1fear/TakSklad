import json
import os
import sys

from taksklad.secret_store import BACKEND_API_TOKEN_SECRET, SecretStoreError, load_secret

SPREADSHEET_ID = "1hisRZ667qEhsRTfoPzv4r78naYhc9kdzhkmUKvZEUr8"
SHEET_NAME = "data"
ARCHIVE_SHEET_NAME = "Архив"
RETURNS_SHEET_NAME = "Возвраты"
APP_NAME = "TakSklad"
APP_EXECUTABLE_NAME = "TakSklad.exe"
APP_RELEASE_ZIP_NAME = "TakSklad-windows-x64.zip"


def get_app_dir():
    if getattr(sys, "frozen", False):
        executable_dir = os.path.dirname(sys.executable)
        if (
            sys.platform == "darwin"
            and os.path.basename(executable_dir) == "MacOS"
            and os.path.basename(os.path.dirname(executable_dir)) == "Contents"
        ):
            return os.path.abspath(
                os.path.join(executable_dir, os.pardir, os.pardir, os.pardir)
            )
        return executable_dir
    return os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, os.pardir))


def _int_env(name, default):
    try:
        return int(os.environ.get(name, str(default)) or default)
    except (TypeError, ValueError):
        return default


def _bool_text(value):
    return str(value or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
        "да",
    }


def _load_runtime_config(app_dir):
    path = os.path.join(app_dir, ".env.taksklad-vds-2.0.generated.json")
    try:
        if not os.path.exists(path):
            return {}
        with open(path, "r", encoding="utf-8") as config_file:
            data = json.load(config_file)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _runtime_config_value(runtime_config, *names):
    for name in names:
        value = runtime_config.get(name)
        if value not in (None, ""):
            return str(value).strip()
    return ""


def _string_setting(runtime_config, env_name, *runtime_names, default=""):
    env_value = os.environ.get(env_name)
    if env_value is not None:
        return env_value.strip()
    runtime_value = _runtime_config_value(runtime_config, *runtime_names)
    return runtime_value or default


def _bool_setting(runtime_config, env_name, *runtime_names, default=False):
    env_value = os.environ.get(env_name)
    if env_value is not None:
        return _bool_text(env_value)
    runtime_value = _runtime_config_value(runtime_config, *runtime_names)
    if runtime_value:
        return _bool_text(runtime_value)
    return bool(default)


def _int_setting(runtime_config, env_name, *runtime_names, default=0):
    env_value = os.environ.get(env_name)
    if env_value is not None:
        try:
            return int(env_value or default)
        except (TypeError, ValueError):
            return default
    runtime_value = _runtime_config_value(runtime_config, *runtime_names)
    if runtime_value:
        try:
            return int(runtime_value)
        except (TypeError, ValueError):
            return default
    return default


APP_DIR = get_app_dir()
RUNTIME_CONFIG_FILE = os.path.join(APP_DIR, ".env.taksklad-vds-2.0.generated.json")
RUNTIME_CONFIG = _load_runtime_config(APP_DIR) if getattr(sys, "frozen", False) else {}
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

APP_VERSION = "2.0.33"
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

try:
    TAKSKLAD_BACKEND_API_TOKEN = (load_secret(BACKEND_API_TOKEN_SECRET) or "").strip()
except SecretStoreError:
    if getattr(sys, "frozen", False) and os.name != "nt":
        raise
    TAKSKLAD_BACKEND_API_TOKEN = ""
TAKSKLAD_BACKEND_BASE_URL = _string_setting(
    RUNTIME_CONFIG,
    "TAKSKLAD_BACKEND_BASE_URL",
    "TAKSKLAD_BACKEND_BASE_URL",
    default="https://api.taksklad.uz" if TAKSKLAD_BACKEND_API_TOKEN else "",
).rstrip("/")
TAKSKLAD_BACKEND_ENABLED = _bool_setting(
    RUNTIME_CONFIG,
    "TAKSKLAD_BACKEND_ENABLED",
    "TAKSKLAD_BACKEND_ENABLED",
    default=bool(TAKSKLAD_BACKEND_API_TOKEN),
)
TAKSKLAD_BACKEND_READ_ORDERS_ENABLED = _bool_setting(
    RUNTIME_CONFIG,
    "TAKSKLAD_BACKEND_READ_ORDERS_ENABLED",
    "TAKSKLAD_BACKEND_READ_ORDERS_ENABLED",
    default=TAKSKLAD_BACKEND_ENABLED,
)
TAKSKLAD_BACKEND_ONLY_REFRESH = _bool_setting(
    RUNTIME_CONFIG,
    "TAKSKLAD_BACKEND_ONLY_REFRESH",
    "TAKSKLAD_BACKEND_ONLY_REFRESH",
    default=False,
)
TAKSKLAD_BACKEND_EMERGENCY_GOOGLE_FALLBACK_ENABLED = _bool_setting(
    RUNTIME_CONFIG,
    "TAKSKLAD_BACKEND_EMERGENCY_GOOGLE_FALLBACK_ENABLED",
    "TAKSKLAD_BACKEND_EMERGENCY_GOOGLE_FALLBACK_ENABLED",
    default=False,
)
TELEGRAM_DESKTOP_POLLING_ENABLED = _bool_setting(
    RUNTIME_CONFIG,
    "TELEGRAM_DESKTOP_POLLING_ENABLED",
    "TELEGRAM_DESKTOP_POLLING_ENABLED",
    default=not TAKSKLAD_BACKEND_ENABLED,
)
TAKSKLAD_BACKEND_TIMEOUT_SECONDS = _int_setting(
    RUNTIME_CONFIG,
    "TAKSKLAD_BACKEND_TIMEOUT_SECONDS",
    "TAKSKLAD_BACKEND_TIMEOUT_SECONDS",
    default=8,
)

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
SKLADBOT_REQUEST_DELAY_SECONDS = 2.0
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

BG_MAIN = "#f4f1e8"
BG_CARD = "#fffdf7"
FG_TEXT = "#2e2c28"
FG_MUTED = "#777066"
ACCENT = "#b28224"
SUCCESS = "#2f8a4a"
INFO = "#3a6b8f"
WARNING = "#d8b64c"
DANGER = "#b7483c"
ERROR_BG = "#f8ded9"
ERROR_FG = "#b7483c"
BORDER = "#d8d0bf"
DISABLED_BG = "#e0d7b9"
DISABLED_FG = "#8f8878"

__all__ = [name for name in globals() if name.isupper()] + ["get_app_dir"]
