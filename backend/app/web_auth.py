import base64
import hashlib
import hmac
import json
import secrets
import time


SESSION_COOKIE_NAME = "taksklad_web_session"


class WebAuthError(Exception):
    pass


def authenticate_web_user(settings, login, password):
    if not settings.web_auth_enabled:
        raise WebAuthError("web auth is not configured")
    if not constant_time_equals(normalize_login(login), settings.web_login):
        raise WebAuthError("invalid credentials")
    if not verify_password(str(password or ""), settings.web_password_hash):
        raise WebAuthError("invalid credentials")
    return settings.web_login


def create_session_token(settings, login, now=None):
    secret = session_secret(settings)
    now = int(now or time.time())
    payload = {
        "sub": normalize_login(login),
        "iat": now,
        "exp": now + int(settings.web_session_ttl_seconds),
        "nonce": secrets.token_urlsafe(12),
    }
    payload_part = base64url_encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    signature = sign_payload(secret, payload_part)
    return f"{payload_part}.{signature}"


def verify_session_token(settings, token, now=None):
    secret = session_secret(settings)
    text = str(token or "").strip()
    if not text or "." not in text:
        raise WebAuthError("missing session")
    payload_part, signature = text.rsplit(".", 1)
    expected_signature = sign_payload(secret, payload_part)
    if not constant_time_equals(signature, expected_signature):
        raise WebAuthError("invalid session signature")
    try:
        payload = json.loads(base64url_decode(payload_part).decode("utf-8"))
    except (ValueError, json.JSONDecodeError) as exc:
        raise WebAuthError("invalid session payload") from exc
    now = int(now or time.time())
    if int(payload.get("exp") or 0) < now:
        raise WebAuthError("session expired")
    login = normalize_login(payload.get("sub"))
    if not login or login != settings.web_login:
        raise WebAuthError("invalid session subject")
    return payload


def session_secret(settings):
    secret = str(settings.web_session_secret or "").strip()
    if not secret:
        raise WebAuthError("web session secret is not configured")
    return secret.encode("utf-8")


def sign_payload(secret, payload_part):
    digest = hmac.new(secret, payload_part.encode("utf-8"), hashlib.sha256).digest()
    return base64url_encode(digest)


def base64url_encode(value):
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def base64url_decode(value):
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(f"{value}{padding}")


def normalize_login(value):
    return "".join(ch for ch in str(value or "").strip() if ch.isdigit() or ch == "+")


def constant_time_equals(left, right):
    return hmac.compare_digest(str(left or ""), str(right or ""))


def hash_password(password, salt=None, iterations=260000):
    salt = salt or secrets.token_urlsafe(16)
    digest = hashlib.pbkdf2_hmac("sha256", str(password or "").encode("utf-8"), salt.encode("utf-8"), iterations)
    return f"pbkdf2_sha256${iterations}${salt}${base64url_encode(digest)}"


def verify_password(password, password_hash):
    try:
        algorithm, iterations_text, salt, expected = str(password_hash or "").split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        iterations = int(iterations_text)
    except (ValueError, TypeError):
        return False
    candidate = hash_password(password, salt=salt, iterations=iterations).rsplit("$", 1)[-1]
    return constant_time_equals(candidate, expected)
