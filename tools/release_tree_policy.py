"""Path-only policy shared by local and CI release-tree guards."""

from __future__ import annotations

import fnmatch
from pathlib import PurePosixPath


FORBIDDEN_DIRECTORY_NAMES = {
    "backup",
    "backups",
    "client-exports",
    "client_exports",
    "credentials",
    "exports",
    "outputs",
    "reports",
    "scan_backups",
    "secrets",
    "сверка",
}

FORBIDDEN_BASENAMES = {
    ".env",
    "local_secrets.md",
    "pending_backend_events.json",
    "pending_prints.json",
    "pending_saves.json",
    "pending_telegram.json",
    "telegram_settings.json",
    "telegram_state.json",
    "taksklad.log",
    "taksklad_data.json",
    "yandex_geocoder_key.txt",
    "пароли.md",
}

FORBIDDEN_BASENAME_PATTERNS = (
    ".env.*",
    "credentials_*.json",
    "google_sheet_backup_*.json",
    "taksklad_data_*.json",
)

RUNTIME_SURFACE_PREFIXES = (
    ".github/",
    "backend/app/",
    "backend/migrations/",
    "deploy/",
    "frontend/src/",
    "src/",
    "taksklad/",
    "tests/",
    "tools/",
)


def normalize_repo_path(path: str) -> str:
    normalized = path.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return str(PurePosixPath(normalized))


def forbidden_path_reason(path: str) -> str | None:
    """Return a stable policy reason without opening the referenced path."""

    normalized = normalize_repo_path(path)
    parts = tuple(part.casefold() for part in PurePosixPath(normalized).parts)
    if any(part in FORBIDDEN_DIRECTORY_NAMES for part in parts[:-1]):
        return "forbidden operational/client-data directory"

    basename = parts[-1] if parts else ""
    if basename in FORBIDDEN_DIRECTORY_NAMES:
        return "forbidden operational/client-data directory"
    if basename in FORBIDDEN_BASENAMES:
        return "forbidden secret/runtime filename"
    if any(fnmatch.fnmatch(basename, pattern) for pattern in FORBIDDEN_BASENAME_PATTERNS):
        return "forbidden secret/runtime filename pattern"
    return None


def is_runtime_surface(path: str) -> bool:
    normalized = normalize_repo_path(path)
    return any(normalized == prefix.rstrip("/") or normalized.startswith(prefix) for prefix in RUNTIME_SURFACE_PREFIXES)
