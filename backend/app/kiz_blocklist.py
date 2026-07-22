import os

BLOCK_REASON_DEFAULT = "Код маркировки заблокирован. Отгрузка запрещена"

BLOCKED_KIZ_CODES = {
    "0104006396053947217)E>!8a93KYHFaPwn": "Код маркировки заблокирован. Отгрузка запрещена",
}

ENV_BLOCKED_CODES = "TAKSKLAD_BLOCKED_KIZ_CODES"


def _normalize(code):
    return str(code or "").strip(" \t\r\n")


def env_blocked_codes(environ=None):
    environ = os.environ if environ is None else environ
    raw_value = str(environ.get(ENV_BLOCKED_CODES, "") or "")
    return tuple(part.strip() for part in raw_value.split(",") if part.strip())


def blocked_kiz_reason(code, environ=None):
    normalized = _normalize(code)
    if not normalized:
        return ""
    if normalized in BLOCKED_KIZ_CODES:
        return BLOCKED_KIZ_CODES[normalized]
    if normalized in env_blocked_codes(environ):
        return BLOCK_REASON_DEFAULT
    return ""


def is_blocked_kiz(code, environ=None):
    return bool(blocked_kiz_reason(code, environ))
