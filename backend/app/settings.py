import ipaddress
import os
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlsplit, urlunsplit

try:
    from .daily_report_config import (
        DailyReportConfigurationError,
        validate_daily_report_schedule_config,
    )
except ImportError:  # pragma: no cover - standalone settings verifier compatibility
    from backend.app.daily_report_config import (
        DailyReportConfigurationError,
        validate_daily_report_schedule_config,
    )


APP_VERSION = "2.0.50"
DESKTOP_API_CONTRACT = 1
VALID_ENVIRONMENTS = frozenset({"local", "test", "production"})
MIN_SESSION_SECRET_BYTES = 32
MIN_SESSION_SECRET_DISTINCT_CHARACTERS = 8
VALID_LEGACY_AUTH_MODES = frozenset({"enforce", "shadow", "disabled"})
MAX_SERVICE_TOKEN_ROTATION_OVERLAP_SECONDS = 3600
PRODUCTION_TRUSTED_PROXY_CIDRS = ("172.18.0.0/16",)
DATABASE_INTEGER_SETTINGS = {
    "TAKSKLAD_DB_POOL_SIZE": ("db_pool_size", 2, 1, 20),
    "TAKSKLAD_DB_MAX_OVERFLOW": ("db_max_overflow", 1, 0, 20),
    "TAKSKLAD_DB_POOL_TIMEOUT_SECONDS": ("db_pool_timeout_seconds", 2, 1, 30),
    "TAKSKLAD_DB_POOL_RECYCLE_SECONDS": ("db_pool_recycle_seconds", 1800, 60, 86400),
    "TAKSKLAD_DB_CONNECT_TIMEOUT_SECONDS": ("db_connect_timeout_seconds", 5, 1, 30),
    "TAKSKLAD_DB_STATEMENT_TIMEOUT_MS": ("db_statement_timeout_ms", 5000, 100, 60000),
    "TAKSKLAD_DB_LOCK_TIMEOUT_MS": ("db_lock_timeout_ms", 2000, 50, 10000),
    "TAKSKLAD_DB_IDLE_TRANSACTION_TIMEOUT_MS": (
        "db_idle_transaction_timeout_ms",
        10000,
        1000,
        300000,
    ),
}


class ConfigurationError(RuntimeError):
    def __init__(self, setting_names):
        self.setting_names = tuple(sorted({str(name) for name in setting_names if str(name)}))
        super().__init__("Invalid configuration: " + ", ".join(self.setting_names))


@dataclass(frozen=True)
class Settings:
    service_name: str
    environment: str
    environment_explicit: bool
    database_url: str
    db_pool_size: int
    db_max_overflow: int
    db_pool_timeout_seconds: int
    db_pool_recycle_seconds: int
    db_connect_timeout_seconds: int
    db_statement_timeout_ms: int
    db_lock_timeout_ms: int
    db_idle_transaction_timeout_ms: int
    api_token: str
    cors_origins: tuple[str, ...]
    timezone: str
    web_login: str
    web_password_hash: str
    web_session_secret: str
    web_session_ttl_seconds: int
    web_cookie_secure: bool
    web_login_max_attempts: int
    web_login_window_seconds: int
    web_login_lock_seconds: int
    insecure_local_anonymous: bool
    trusted_proxy_cidrs: tuple[str, ...]
    web_login_limiter_max_entries: int
    web_login_limiter_entry_ttl_seconds: int
    identity_auth_enabled: bool
    legacy_auth_mode: str
    legacy_auth_expires_at: str
    service_token_rotation_max_overlap_seconds: int
    worker_heartbeat_required_names: tuple[str, ...]
    skladbot_daily_report_enabled: bool
    skladbot_daily_report_chat_ids: tuple[str, ...]
    skladbot_daily_report_hour: int
    skladbot_daily_report_minute: int
    skladbot_daily_report_retry_minutes: int
    skladbot_daily_report_max_attempts: int
    skladbot_daily_report_grace_minutes: int
    skladbot_daily_report_lookback_days: int

    @property
    def api_auth_enabled(self):
        return bool(self.api_token)

    @property
    def web_auth_enabled(self):
        return bool(self.web_login and self.web_password_hash)

    @property
    def anonymous_local_admin_enabled(self):
        return bool(
            self.environment_explicit
            and self.environment.strip().casefold() == "local"
            and self.insecure_local_anonymous
        )


def parse_csv(value):
    return tuple(part.strip() for part in str(value or "").split(",") if part.strip())


def parse_bool(value, default=False):
    text = str(value or "").strip().casefold()
    if not text:
        return default
    return text in {"1", "true", "yes", "on", "да"}


def parse_int(value, default):
    try:
        return int(str(value or "").strip() or default)
    except ValueError:
        return default


def load_database_settings(environ):
    values = {}
    errors = []
    for setting_name, (field_name, default, minimum, maximum) in DATABASE_INTEGER_SETTINGS.items():
        raw_value = str(environ.get(setting_name, "") or "").strip()
        try:
            value = default if not raw_value else int(raw_value)
        except ValueError:
            errors.append(setting_name)
            continue
        if value < minimum or value > maximum:
            errors.append(setting_name)
            continue
        values[field_name] = value
    if errors:
        raise ConfigurationError(errors)
    if values["db_lock_timeout_ms"] >= values["db_statement_timeout_ms"]:
        raise ConfigurationError((
            "TAKSKLAD_DB_LOCK_TIMEOUT_MS",
            "TAKSKLAD_DB_STATEMENT_TIMEOUT_MS",
        ))
    return values


def mask_secret_url(url):
    parts = urlsplit(str(url or ""))
    if not parts.password:
        return str(url or "")
    username = parts.username or ""
    hostname = parts.hostname or ""
    port = f":{parts.port}" if parts.port else ""
    netloc = f"{username}:***@{hostname}{port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def load_settings(environ=None):
    environ = os.environ if environ is None else environ
    raw_environment = environ.get("TAKSKLAD_ENV", "")
    database_settings = load_database_settings(environ)
    try:
        daily_report_schedule = validate_daily_report_schedule_config(environ)
    except DailyReportConfigurationError as exc:
        raise ConfigurationError(exc.setting_names) from exc
    return Settings(
        service_name=environ.get("TAKSKLAD_SERVICE_NAME", "taksklad-backend"),
        environment=str(raw_environment or "local").strip() or "local",
        environment_explicit=bool(str(raw_environment or "").strip()),
        database_url=environ.get(
            "DATABASE_URL",
            "postgresql+psycopg://taksklad:taksklad@postgres:5432/taksklad",
        ),
        **database_settings,
        api_token=environ.get("TAKSKLAD_API_TOKEN", "").strip(),
        cors_origins=parse_csv(environ.get("TAKSKLAD_CORS_ORIGINS", "")),
        timezone=daily_report_schedule.timezone_name,
        web_login=environ.get("TAKSKLAD_WEB_LOGIN", "").strip(),
        web_password_hash=environ.get("TAKSKLAD_WEB_PASSWORD_HASH", "").strip(),
        web_session_secret=environ.get("TAKSKLAD_WEB_SESSION_SECRET", "").strip(),
        web_session_ttl_seconds=max(300, parse_int(environ.get("TAKSKLAD_WEB_SESSION_TTL_SECONDS"), 86400)),
        web_cookie_secure=parse_bool(
            environ.get("TAKSKLAD_WEB_COOKIE_SECURE"),
            default=(environ.get("TAKSKLAD_ENV", "local").strip().casefold() != "local"),
        ),
        web_login_max_attempts=max(1, parse_int(environ.get("TAKSKLAD_WEB_LOGIN_MAX_ATTEMPTS"), 5)),
        web_login_window_seconds=max(30, parse_int(environ.get("TAKSKLAD_WEB_LOGIN_WINDOW_SECONDS"), 300)),
        web_login_lock_seconds=max(60, parse_int(environ.get("TAKSKLAD_WEB_LOGIN_LOCK_SECONDS"), 900)),
        insecure_local_anonymous=parse_bool(
            environ.get("TAKSKLAD_INSECURE_LOCAL_ANONYMOUS"),
            default=False,
        ),
        trusted_proxy_cidrs=parse_csv(environ.get("TAKSKLAD_TRUSTED_PROXY_CIDRS", "")),
        web_login_limiter_max_entries=max(
            1,
            parse_int(environ.get("TAKSKLAD_WEB_LOGIN_LIMITER_MAX_ENTRIES"), 10000),
        ),
        web_login_limiter_entry_ttl_seconds=max(
            60,
            parse_int(environ.get("TAKSKLAD_WEB_LOGIN_LIMITER_ENTRY_TTL_SECONDS"), 3600),
        ),
        identity_auth_enabled=parse_bool(
            environ.get("TAKSKLAD_IDENTITY_AUTH_ENABLED"),
            default=False,
        ),
        legacy_auth_mode=str(environ.get("TAKSKLAD_LEGACY_AUTH_MODE", "enforce") or "enforce").strip().casefold(),
        legacy_auth_expires_at=str(environ.get("TAKSKLAD_LEGACY_AUTH_EXPIRES_AT", "") or "").strip(),
        service_token_rotation_max_overlap_seconds=max(
            1,
            parse_int(environ.get("TAKSKLAD_SERVICE_TOKEN_ROTATION_MAX_OVERLAP_SECONDS"), 900),
        ),
        worker_heartbeat_required_names=parse_csv(environ.get("TAKSKLAD_REQUIRED_WORKERS", "")),
        skladbot_daily_report_enabled=parse_bool(
            environ.get("SKLADBOT_DAILY_REPORT_ENABLED"),
            default=False,
        ),
        skladbot_daily_report_chat_ids=parse_csv(
            environ.get("SKLADBOT_DAILY_REPORT_CHAT_IDS", ""),
        ),
        skladbot_daily_report_hour=daily_report_schedule.hour,
        skladbot_daily_report_minute=daily_report_schedule.minute,
        skladbot_daily_report_retry_minutes=daily_report_schedule.retry_minutes,
        skladbot_daily_report_max_attempts=daily_report_schedule.max_attempts,
        skladbot_daily_report_grace_minutes=daily_report_schedule.grace_minutes,
        skladbot_daily_report_lookback_days=daily_report_schedule.lookback_days,
    )


def validate_backend_settings(settings):
    errors = []
    environment = str(settings.environment or "").strip().casefold()
    known_workers = {"skladbot", "smartup_auto_import", "telegram"}
    if any(name not in known_workers for name in settings.worker_heartbeat_required_names):
        errors.append("TAKSKLAD_REQUIRED_WORKERS")
    if not settings.environment_explicit or environment not in VALID_ENVIRONMENTS:
        errors.append("TAKSKLAD_ENV")

    has_web_login = bool(settings.web_login)
    has_web_password = bool(settings.web_password_hash)
    if has_web_login != has_web_password:
        errors.extend(("TAKSKLAD_WEB_LOGIN", "TAKSKLAD_WEB_PASSWORD_HASH"))

    legacy_auth_can_enforce = settings.legacy_auth_mode == "enforce"
    auth_enabled = bool(
        settings.identity_auth_enabled
        or (legacy_auth_can_enforce and (settings.api_auth_enabled or settings.web_auth_enabled))
    )
    if not auth_enabled and not settings.anonymous_local_admin_enabled:
        errors.append("TAKSKLAD_AUTH_MECHANISM")

    session_required = environment != "local" or settings.web_auth_enabled
    if session_required and not settings.web_session_secret:
        errors.append("TAKSKLAD_WEB_SESSION_SECRET")
    if settings.web_session_secret and (
        len(settings.web_session_secret.encode("utf-8")) < MIN_SESSION_SECRET_BYTES
        or len(set(settings.web_session_secret)) < MIN_SESSION_SECRET_DISTINCT_CHARACTERS
    ):
        errors.append("TAKSKLAD_WEB_SESSION_SECRET")
    if (
        settings.api_token
        and settings.web_session_secret
        and settings.api_token == settings.web_session_secret
    ):
        errors.append("TAKSKLAD_WEB_SESSION_SECRET")

    if (
        environment == "production"
        and settings.trusted_proxy_cidrs != PRODUCTION_TRUSTED_PROXY_CIDRS
    ):
        errors.append("TAKSKLAD_TRUSTED_PROXY_CIDRS")

    for cidr in settings.trusted_proxy_cidrs:
        try:
            ipaddress.ip_network(cidr, strict=False)
        except ValueError:
            errors.append("TAKSKLAD_TRUSTED_PROXY_CIDRS")
            break

    if settings.legacy_auth_mode not in VALID_LEGACY_AUTH_MODES:
        errors.append("TAKSKLAD_LEGACY_AUTH_MODE")
    if settings.legacy_auth_mode in {"shadow", "disabled"} and not settings.identity_auth_enabled:
        errors.append("TAKSKLAD_IDENTITY_AUTH_ENABLED")
    legacy_configured = bool(settings.api_auth_enabled or settings.web_auth_enabled)
    if environment == "production" and legacy_configured and settings.legacy_auth_mode != "disabled":
        if not settings.legacy_auth_expires_at:
            errors.append("TAKSKLAD_LEGACY_AUTH_EXPIRES_AT")
        else:
            try:
                legacy_expiry = datetime.fromisoformat(settings.legacy_auth_expires_at.replace("Z", "+00:00"))
                if legacy_expiry.tzinfo is None:
                    raise ValueError
            except ValueError:
                errors.append("TAKSKLAD_LEGACY_AUTH_EXPIRES_AT")
    if settings.service_token_rotation_max_overlap_seconds > MAX_SERVICE_TOKEN_ROTATION_OVERLAP_SECONDS:
        errors.append("TAKSKLAD_SERVICE_TOKEN_ROTATION_MAX_OVERLAP_SECONDS")

    if errors:
        raise ConfigurationError(errors)
    return settings
