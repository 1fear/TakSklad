import re


SECRET_PATTERNS = [
    re.compile(r"(bot\d+:[A-Za-z0-9_-]+)"),
    re.compile(r"(Bearer\s+)[A-Za-z0-9._~+/=-]+", re.IGNORECASE),
    re.compile(r"(token|password|secret|authorization)([\"'=:\s]+)([^\"'\s,}]+)", re.IGNORECASE),
    re.compile(r"\b010[0-9A-Za-z`'+/=:_-]{16,}"),
]


def redact_secrets(value):
    text = str(value or "")
    for pattern in SECRET_PATTERNS:
        if pattern.groups >= 3:
            text = pattern.sub(r"\1\2***", text)
        elif pattern.groups == 2:
            text = pattern.sub(r"\1***", text)
        elif pattern.groups == 1:
            text = pattern.sub(r"\1***", text)
        else:
            text = pattern.sub("***", text)
    return text
