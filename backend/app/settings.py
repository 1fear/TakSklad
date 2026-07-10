import ipaddress
import os
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit


APP_VERSION = "2.0.25"
VALID_ENVIRONMENTS = frozenset({"local", "test", "production"})
MIN_SESSION_SECRET_BYTES = 32
MIN_SESSION_SECRET_DISTINCT_CHARACTERS = 8


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
    google_to_backend_sync_enabled: bool

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
    return Settings(
        service_name=environ.get("TAKSKLAD_SERVICE_NAME", "taksklad-backend"),
        environment=str(raw_environment or "local").strip() or "local",
        environment_explicit=bool(str(raw_environment or "").strip()),
        database_url=environ.get(
            "DATABASE_URL",
            "postgresql+psycopg://taksklad:taksklad@postgres:5432/taksklad",
        ),
        api_token=environ.get("TAKSKLAD_API_TOKEN", "").strip(),
        cors_origins=parse_csv(environ.get("TAKSKLAD_CORS_ORIGINS", "")),
        timezone=environ.get("TAKSKLAD_TIMEZONE", "Asia/Tashkent").strip() or "Asia/Tashkent",
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
        google_to_backend_sync_enabled=parse_bool(
            environ.get("TAKSKLAD_GOOGLE_TO_BACKEND_SYNC_ENABLED"),
            default=False,
        ),
    )


def validate_backend_settings(settings):
    errors = []
    environment = str(settings.environment or "").strip().casefold()
    if not settings.environment_explicit or environment not in VALID_ENVIRONMENTS:
        errors.append("TAKSKLAD_ENV")

    has_web_login = bool(settings.web_login)
    has_web_password = bool(settings.web_password_hash)
    if has_web_login != has_web_password:
        errors.extend(("TAKSKLAD_WEB_LOGIN", "TAKSKLAD_WEB_PASSWORD_HASH"))

    auth_enabled = bool(settings.api_auth_enabled or settings.web_auth_enabled)
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

    for cidr in settings.trusted_proxy_cidrs:
        try:
            ipaddress.ip_network(cidr, strict=False)
        except ValueError:
            errors.append("TAKSKLAD_TRUSTED_PROXY_CIDRS")
            break

    if errors:
        raise ConfigurationError(errors)
    return settings
