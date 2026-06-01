import os
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit


APP_VERSION = "2.0.0"


@dataclass(frozen=True)
class Settings:
    service_name: str
    environment: str
    database_url: str
    api_token: str
    cors_origins: tuple[str, ...]
    timezone: str

    @property
    def api_auth_enabled(self):
        return bool(self.api_token)


def parse_csv(value):
    return tuple(part.strip() for part in str(value or "").split(",") if part.strip())


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
    )
