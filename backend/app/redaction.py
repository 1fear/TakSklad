import re


UUID_PATTERN = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)
SECRET_PATTERNS = (
    (re.compile(r"\b(bot\d+:)[A-Za-z0-9_-]+\b", re.IGNORECASE), r"\1***"),
    (re.compile(r"(Bearer\s+)[A-Za-z0-9._~+/=-]+", re.IGNORECASE), r"\1***"),
    (
        re.compile(
            r"(token|password|secret|authorization)([\"'=:\s]+)([^\"'\s,}]+)",
            re.IGNORECASE,
        ),
        r"\1\2***",
    ),
    (re.compile(r"\b010[0-9A-Za-z`'+/=:_-]{16,}"), "***"),
)


def redact_secrets(value):
    text = str(value or "")
    protected_uuids = []

    def protect_uuid(match):
        placeholder = f"__TAKSKLAD_UUID_{len(protected_uuids)}__"
        protected_uuids.append((placeholder, match.group(0)))
        return placeholder

    text = UUID_PATTERN.sub(protect_uuid, text)
    for pattern, replacement in SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    for placeholder, uuid_value in protected_uuids:
        text = text.replace(placeholder, uuid_value)
    return text
