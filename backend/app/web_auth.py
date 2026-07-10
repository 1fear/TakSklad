import base64
import hashlib
import hmac
import json
import secrets
import time
import uuid
from dataclasses import dataclass

from sqlalchemy import select


SESSION_COOKIE_NAME = "taksklad_web_session"
ROLE_ADMIN = "admin"
ROLE_LOGISTICS_SLOTS = "logistics_slots"
ROLE_OPERATOR = "operator"
PERMISSION_ADMIN_WRITE = "admin:write"
PERMISSION_CLIENT_POINTS_WRITE = "client_points:write"


@dataclass(frozen=True)
class AuthIdentity:
    login: str
    role: str
    user_id: uuid.UUID | None = None
    auth_version: int = 0


class WebAuthError(Exception):
    pass


def authenticate_web_user(settings, login, password, db=None):
    normalized_login = normalize_login(login)
    if not normalized_login:
        raise WebAuthError("invalid credentials")

    if settings.web_login and settings.web_password_hash and constant_time_equals(normalized_login, settings.web_login):
        if not verify_password(str(password or ""), settings.web_password_hash):
            raise WebAuthError("invalid credentials")
        return AuthIdentity(login=settings.web_login, role=ROLE_ADMIN)

    if db is not None:
        user = find_active_db_user(db, normalized_login)
        if user and user.password_hash and verify_password(str(password or ""), user.password_hash):
            return AuthIdentity(
                login=user.username,
                role=normalize_role(user.role),
                user_id=user.id,
                auth_version=int(getattr(user, "auth_version", 0) or 0),
            )

    if not settings.web_auth_enabled and db is None:
        raise WebAuthError("web auth is not configured")
    raise WebAuthError("invalid credentials")


def find_active_db_user(db, login):
    from .models import User

    return db.execute(
        select(User)
        .where(User.username == login)
        .where(User.is_active.is_(True))
    ).scalar_one_or_none()


def create_session_token(settings, login, role=ROLE_ADMIN, now=None):
    secret = session_secret(settings)
    now = int(now or time.time())
    payload = {
        "sub": normalize_login(login),
        "role": normalize_role(role),
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
    if not login:
        raise WebAuthError("invalid session subject")
    payload["sub"] = login
    payload["role"] = normalize_session_role(settings, payload)
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


def normalize_session_role(settings, payload):
    role = normalize_role(payload.get("role"))
    if role != ROLE_OPERATOR:
        return role
    if constant_time_equals(normalize_login(payload.get("sub")), settings.web_login):
        return ROLE_ADMIN
    return role


def normalize_role(value):
    role = str(value or "").strip().casefold().replace("-", "_")
    if role in {"admin", "administrator", "owner"}:
        return ROLE_ADMIN
    if role in {"logistics_slots", "logistics", "logistic_slots", "client_points"}:
        return ROLE_LOGISTICS_SLOTS
    return role or ROLE_OPERATOR


def role_permissions(role):
    normalized_role = normalize_role(role)
    if normalized_role == ROLE_ADMIN:
        return (PERMISSION_ADMIN_WRITE, PERMISSION_CLIENT_POINTS_WRITE)
    if normalized_role == ROLE_LOGISTICS_SLOTS:
        return (PERMISSION_CLIENT_POINTS_WRITE,)
    return ()


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
