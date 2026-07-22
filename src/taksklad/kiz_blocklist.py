BLOCK_REASON_DEFAULT = "Код маркировки заблокирован. Отгрузка запрещена"

BLOCKED_KIZ_CODES = {
    "0104006396053947217)E>!8a93KYHFaPwn": "Код маркировки заблокирован. Отгрузка запрещена",
}


def _normalize(code):
    return str(code or "").strip(" \t\r\n")


def blocked_kiz_reason(code):
    normalized = _normalize(code)
    if not normalized:
        return ""
    return BLOCKED_KIZ_CODES.get(normalized, "")


def is_blocked_kiz(code):
    return bool(blocked_kiz_reason(code))
