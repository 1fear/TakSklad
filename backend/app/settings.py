import os
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit


APP_VERSION = "2.0.15"


@dataclass(frozen=True)
class Settings:
    service_name: str
    environment: str
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
    google_to_backend_sync_enabled: bool

    @property
    def api_auth_enabled(self):
        return bool(self.api_token)

    @property
    def web_auth_enabled(self):
        return bool(self.web_login and self.web_password_hash)


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
    environ = environ or os.environ
    return Settings(
        service_name=environ.get("TAKSKLAD_SERVICE_NAME", "taksklad-backend"),
        environment=environ.get("TAKSKLAD_ENV", "local"),
        database_url=environ.get(
            "DATABASE_URL",
            "postgresql+psycopg://taksklad:taksklad@postgres:5432/taksklad",
        ),
        api_token=environ.get("TAKSKLAD_API_TOKEN", "").strip(),
        cors_origins=parse_csv(environ.get("TAKSKLAD_CORS_ORIGINS", "")),
        timezone=environ.get("TAKSKLAD_TIMEZONE", "Asia/Tashkent").strip() or "Asia/Tashkent",
        web_login=environ.get("TAKSKLAD_WEB_LOGIN", "").strip(),
        web_password_hash=environ.get("TAKSKLAD_WEB_PASSWORD_HASH", "").strip(),
        web_session_secret=(
            environ.get("TAKSKLAD_WEB_SESSION_SECRET", "").strip()
            or environ.get("TAKSKLAD_API_TOKEN", "").strip()
        ),
        web_session_ttl_seconds=max(300, parse_int(environ.get("TAKSKLAD_WEB_SESSION_TTL_SECONDS"), 86400)),
        web_cookie_secure=parse_bool(
            environ.get("TAKSKLAD_WEB_COOKIE_SECURE"),
            default=(environ.get("TAKSKLAD_ENV", "local").strip().casefold() != "local"),
        ),
        web_login_max_attempts=max(1, parse_int(environ.get("TAKSKLAD_WEB_LOGIN_MAX_ATTEMPTS"), 5)),
        web_login_window_seconds=max(30, parse_int(environ.get("TAKSKLAD_WEB_LOGIN_WINDOW_SECONDS"), 300)),
        web_login_lock_seconds=max(60, parse_int(environ.get("TAKSKLAD_WEB_LOGIN_LOCK_SECONDS"), 900)),
        google_to_backend_sync_enabled=parse_bool(
            environ.get("TAKSKLAD_GOOGLE_TO_BACKEND_SYNC_ENABLED"),
            default=False,
        ),
    )
