import hashlib
import hmac
import ipaddress
import logging
from dataclasses import dataclass
from urllib.parse import quote
from threading import Lock, Thread
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError

from .access_policy import (
    AUTH_PROTECTED,
    SAFE_METHODS,
    route_policy,
)
from .admin_service import build_admin_table
from .audit_identity import AUDIT_ACTOR_INFO_KEY, bind_audit_actor
from .client_points_service import (
    ClientPointApiError,
    get_client_point_order_summary,
    list_client_points as list_client_points_in_db,
    update_client_point_timeslot as update_client_point_timeslot_in_db,
)
from .db import get_db
from .db_errors import database_error_response
from .diagnostics_service import build_backend_diagnostics_log
from .event_queue_service import (
    EventQueueApiError,
    get_event_queue_detail as get_event_queue_detail_from_db,
    list_event_queue_diagnostics,
    retry_event_queue_event as retry_event_queue_event_in_db,
)
from .google_sheets_sync_worker import sync_google_sheet_to_backend
from .google_sheets_pending import process_pending_google_sheets_exports
from .health_service import (
    build_readiness_report,
    public_readiness_report,
    readiness_http_status,
    runtime_build_identity,
)
from .incidents_service import (
    IncidentApiError,
    create_incident as create_incident_in_db,
    get_incident as get_incident_from_db,
    list_incidents as list_incidents_in_db,
    update_incident_status as update_incident_status_in_db,
)
from .imports_service import create_import as create_import_in_db
from .imports_service import list_imports_page as list_imports_page_in_db
from .imports_service import preview_import as preview_import_in_db
from .input_safety import MAX_REQUEST_BODY_BYTES, RequestBodyLimitMiddleware
from .auth_identities import (
    IdentityAuthError,
    authenticate_service_token,
    create_user_session,
    revoke_user_session,
    validate_user_session,
)
from .csrf import (
    CSRF_ERROR_DETAIL,
    CSRF_HEADER_NAME,
    ORIGIN_ERROR_DETAIL,
    browser_origin_matches,
    csrf_token_for_session,
    csrf_token_matches,
)
from .kiz_reports_service import (
    build_kiz_date_range_report_xlsx,
    build_kiz_date_report_xlsx,
    build_kiz_source_file_report_xlsx,
    list_completed_kiz_dates,
    list_completed_kiz_source_files,
)
from .logistics_service import build_logistics_report_xlsx, list_logistics_dates
from .logistics_calendar_service import (
    list_logistics_calendar as list_logistics_calendar_in_db,
    set_logistics_calendar_day as set_logistics_calendar_day_in_db,
)
from .order_actions_service import (
    archive_order_without_kiz as archive_order_without_kiz_in_db,
    cancel_order as cancel_order_in_db,
    complete_orders_without_kiz as complete_orders_without_kiz_in_db,
    delete_active_order as delete_active_order_in_db,
    reset_order_for_rescan as reset_order_for_rescan_in_db,
    restore_order as restore_order_in_db,
    resync_order_to_google as resync_order_to_google_in_db,
    resync_order_skladbot as resync_order_skladbot_in_db,
)
from .operations_service import build_operations_attention
from .pagination import CursorError, decode_cursor, encode_cursor, normalize_page_limit, set_pagination_headers
from .orders_service import ApiError, complete_order as complete_order_in_db
from .orders_service import create_scan as create_scan_in_db
from .orders_service import list_active_orders_page as list_active_orders_page_in_db
from .orders_service import list_returned_orders as list_returned_orders_in_db
from .orders_service import lookup_kiz_availability as lookup_kiz_availability_in_db
from .orders_service import lookup_return_order as lookup_return_order_in_db
from .orders_service import mark_order_returned as mark_order_returned_in_db
from .orders_service import undo_scan as undo_scan_in_db
from .reconciliation_service import ReconciliationError, preview_daily_reconciliation, run_daily_reconciliation
from .reports_service import build_dashboard_day_summary, build_day_report
from .skladbot_request_dry_run import list_skladbot_dry_runs, rebuild_skladbot_dry_run
from .skladbot_worker import update_orders_from_skladbot
from .smartup_auto_import_history_service import list_smartup_auto_import_history
from .schemas import (
    AdminOrderActionRequest,
    AdminBulkOrderActionRequest,
    AdminBulkOrderActionResult,
    AdminTableRead,
    ActiveOrderDeleteResult,
    AuthLoginRequest,
    AuthSessionRead,
    ClientPointOrderSummaryRead,
    ClientPointRead,
    ClientPointTimeslotUpdate,
    DashboardDaySummaryRead,
    DayReportRead,
    EventQueueDiagnosticsRead,
    EventQueueActionRequest,
    EventQueueEventRead,
    HealthResponse,
    ImportCreate,
    ImportPreviewResult,
    ImportRead,
    ImportResult,
    IncidentCreate,
    IncidentListRead,
    IncidentRead,
    IncidentStatusUpdate,
    KizAvailabilityRead,
    LogisticsCalendarDayRead,
    LogisticsCalendarDayUpdate,
    LogisticsCalendarRead,
    OrderRead,
    OperationsAttentionRead,
    ReadinessResponse,
    ReturnMarkRequest,
    ScanCreate,
    ScanRead,
    ScanUndo,
    SkladBotDryRunRead,
    SmartupAutoImportHistoryRead,
)
from .login_limiter import (
    BoundedTTLLoginLimiter,
    LoginLimiterCapacityExceeded,
    LoginRateLimited,
)
from .models import AuditLog, User
from .settings import APP_VERSION, load_settings, validate_backend_settings
from .web_auth import (
    ROLE_ADMIN,
    SESSION_COOKIE_NAME,
    WebAuthError,
    authenticate_web_user,
    create_session_token,
    normalize_role,
    role_permissions,
    verify_session_token,
)


settings = load_settings()
sync_sources_lock = Lock()
skladbot_sync_lock = Lock()
login_limiter = BoundedTTLLoginLimiter(
    max_entries=settings.web_login_limiter_max_entries,
    entry_ttl_seconds=settings.web_login_limiter_entry_ttl_seconds,
)

app = FastAPI(
    title="TakSklad Backend API",
    version=APP_VERSION,
    docs_url="/docs",
    redoc_url="/redoc",
)
app.add_middleware(RequestBodyLimitMiddleware, max_bytes=MAX_REQUEST_BODY_BYTES)


@app.exception_handler(RequestValidationError)
async def redacted_request_validation_error(_request: Request, _exc: RequestValidationError):
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"detail": "invalid_request"},
        headers={"Cache-Control": "no-store"},
    )


@app.exception_handler(CursorError)
async def invalid_cursor_error(_request: Request, _exc: CursorError):
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"detail": "invalid_cursor"},
        headers={"Cache-Control": "no-store"},
    )


@app.exception_handler(SQLAlchemyError)
async def sanitized_database_error(_request: Request, exc: SQLAlchemyError):
    return database_error_response(exc)


def configure_cors(app_instance: FastAPI, app_settings) -> None:
    if not app_settings.cors_origins:
        return

    app_instance.add_middleware(
        CORSMiddleware,
        allow_origins=list(app_settings.cors_origins),
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", CSRF_HEADER_NAME],
        expose_headers=["X-TakSklad-Next-Cursor", "X-TakSklad-Page-Limit"],
    )


configure_cors(app, settings)


def paginate_materialized(rows, *, scope, response, limit, cursor="", default=50, maximum=200):
    row_limit = normalize_page_limit(limit, default=default, maximum=maximum)
    offset = 0
    if cursor:
        try:
            (offset_value,) = decode_cursor(cursor, scope)
            offset = int(offset_value)
            if offset < 0:
                raise ValueError
        except (CursorError, TypeError, ValueError):
            raise CursorError("invalid_cursor") from None
    page = list(rows[offset:offset + row_limit])
    next_cursor = ""
    if offset + len(page) < len(rows):
        next_cursor = encode_cursor(scope, [offset + len(page)])
    set_pagination_headers(response, next_cursor=next_cursor, limit=row_limit)
    return page


@app.on_event("startup")
def validate_startup_configuration():
    validate_backend_settings(settings)


def legacy_auth_window_active(now=None) -> bool:
    if settings.legacy_auth_mode != "enforce":
        return False
    expiry_text = str(settings.legacy_auth_expires_at or "").strip()
    if not expiry_text:
        return settings.environment.strip().casefold() != "production"
    try:
        expiry = datetime.fromisoformat(expiry_text.replace("Z", "+00:00"))
    except ValueError:
        return False
    if expiry.tzinfo is None:
        return False
    current = now or datetime.now(timezone.utc)
    return current.astimezone(timezone.utc) < expiry.astimezone(timezone.utc)


def legacy_service_token_matches(authorization: str | None) -> bool:
    if not settings.api_auth_enabled:
        return False
    expected = f"Bearer {settings.api_token}"
    return hmac.compare_digest(str(authorization or ""), expected)


def is_valid_service_token(authorization: str | None) -> bool:
    return legacy_auth_window_active() and legacy_service_token_matches(authorization)


@dataclass(frozen=True)
class AuthContext:
    login: str
    role: str
    permissions: tuple[str, ...]
    source: str
    principal_id: str = ""
    token_id: str = ""
    session_id: str = ""
    user_id: str = ""


def bearer_token(authorization: str | None) -> str:
    scheme, separator, token = str(authorization or "").partition(" ")
    if not separator or scheme.casefold() != "bearer":
        return ""
    return token.strip()


def request_route_template(request: Request) -> str:
    scope = getattr(request, "scope", None)
    route = scope.get("route") if isinstance(scope, dict) else None
    return str(getattr(route, "path", "") or getattr(getattr(request, "url", None), "path", "") or "")


def policy_for_request(request: Request):
    policy = route_policy(request.method, request_route_template(request))
    if policy is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="API route authorization policy is missing",
        )
    return policy


def cache_auth_context(request: Request, context: AuthContext) -> AuthContext:
    state = getattr(request, "state", None)
    if state is not None:
        state.auth_context = context
    return context


def read_auth_context(request: Request, authorization: str | None = None, db=None):
    cached = getattr(getattr(request, "state", None), "auth_context", None)
    if isinstance(cached, AuthContext):
        return cached

    raw_bearer = bearer_token(authorization)
    if raw_bearer:
        if settings.identity_auth_enabled and db is not None and raw_bearer.startswith("tks."):
            try:
                verified = authenticate_service_token(
                    db,
                    raw_bearer,
                    touch_last_used=request.method.upper() not in SAFE_METHODS,
                )
                return cache_auth_context(request, AuthContext(
                    login=verified.principal_identifier,
                    role=verified.principal_kind,
                    permissions=tuple(sorted(verified.scopes)),
                    source="service-principal",
                    principal_id=str(verified.principal_id),
                    token_id=str(verified.token_id),
                ))
            except IdentityAuthError as exc:
                db.rollback()
                raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid service token") from exc

        if legacy_service_token_matches(authorization):
            if settings.legacy_auth_mode == "shadow":
                logging.warning("Legacy service credential matched in shadow-only mode")
            elif legacy_auth_window_active():
                return cache_auth_context(request, AuthContext(
                    login="legacy-service-token",
                    role=ROLE_ADMIN,
                    permissions=role_permissions(ROLE_ADMIN),
                    source="legacy-service-token",
                ))
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid service token")

    if settings.web_auth_enabled or settings.identity_auth_enabled:
        try:
            payload = read_web_session(
                request,
                db=db,
                touch_last_used=request.method.upper() not in SAFE_METHODS,
            )
            role = normalize_role(payload.get("role"))
            return cache_auth_context(request, AuthContext(
                login=payload.get("sub") or "",
                role=role,
                permissions=role_permissions(role),
                source="web-session",
                session_id=str(payload.get("sid") or ""),
                user_id=str(payload.get("uid") or ""),
            ))
        except (WebAuthError, IdentityAuthError):
            if db is not None:
                db.rollback()

    if (
        not settings.identity_auth_enabled
        and not settings.api_auth_enabled
        and not settings.web_auth_enabled
        and settings.anonymous_local_admin_enabled
    ):
        return cache_auth_context(request, AuthContext(
            login="local-dev",
            role=ROLE_ADMIN,
            permissions=role_permissions(ROLE_ADMIN),
            source="local-dev",
        ))
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid service token or web session",
    )


def require_service_token(
    request: Request,
    authorization: str | None = Header(default=None),
    db=Depends(get_db),
):
    policy = policy_for_request(request)
    if policy.authentication != AUTH_PROTECTED:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Invalid protected route policy")
    auth_context = read_auth_context(request, authorization, db=db)
    if auth_context.source == "service-principal":
        if not policy.service_scope or policy.service_scope not in auth_context.permissions:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Service principal scope denied")
    elif auth_context.source == "legacy-service-token":
        if not legacy_auth_window_active():
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Legacy service credential denied")
    elif not policy.web_permission or policy.web_permission not in auth_context.permissions:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not enough permissions")
    bind_audit_actor(db, auth_context)
    if policy.mutates and auth_context.source == "web-session":
        require_browser_request_security(request)
    return auth_context


def require_admin_write_permission(
    request: Request,
    authorization: str | None = Header(default=None),
    db=Depends(get_db),
):
    return require_service_token(request, authorization, db=db)


def require_client_points_write_permission(
    request: Request,
    authorization: str | None = Header(default=None),
    db=Depends(get_db),
):
    return require_service_token(request, authorization, db=db)


def require_browser_request_security(request: Request) -> None:
    session_token = request.cookies.get(SESSION_COOKIE_NAME)
    candidate = request.headers.get(CSRF_HEADER_NAME)
    if not browser_origin_matches(request, settings):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=ORIGIN_ERROR_DETAIL)
    if not csrf_token_matches(settings, session_token, candidate):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=CSRF_ERROR_DETAIL)


def require_browser_origin(request: Request) -> None:
    if not browser_origin_matches(request, settings):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=ORIGIN_ERROR_DETAIL)


@app.get("/health", response_model=HealthResponse)
def health():
    build_identity = runtime_build_identity()
    return {
        "status": "ok",
        "service": settings.service_name,
        "version": APP_VERSION,
        **build_identity,
        "environment": settings.environment,
    }


@app.get("/ready", response_model=ReadinessResponse)
def readiness(response: Response, db=Depends(get_db)):
    report = build_readiness_report(db, settings)
    response.status_code = readiness_http_status(report)
    return public_readiness_report(report)


auth_api = APIRouter(prefix="/api/v1/auth")


def prevent_auth_response_caching(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"


def auth_session_read(payload, session_token: str = ""):
    expires_at = datetime.fromtimestamp(int(payload.get("exp") or 0), timezone.utc)
    role = normalize_role(payload.get("role"))
    return AuthSessionRead(
        authenticated=True,
        login=payload.get("sub") or "",
        role=role,
        permissions=list(role_permissions(role)),
        expires_at=expires_at,
        csrf_token=csrf_token_for_session(settings, session_token),
    )


def read_web_session(request: Request, db=None, *, touch_last_used: bool = True):
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if settings.identity_auth_enabled and db is not None and str(token or "").startswith("tks."):
        verified = validate_user_session(db, token, touch_last_used=touch_last_used)
        return {
            "sub": verified.username,
            "role": verified.role,
            "exp": int(verified.expires_at.timestamp()),
            "sid": str(verified.session_id),
            "uid": str(verified.user_id),
            "av": verified.auth_version,
        }
    if not legacy_auth_window_active():
        raise WebAuthError("legacy web session is disabled")
    return verify_session_token(settings, token)


@auth_api.post("/login", response_model=AuthSessionRead)
def web_login(payload: AuthLoginRequest, request: Request, response: Response, db=Depends(get_db)):
    prevent_auth_response_caching(response)
    require_browser_origin(request)
    login_key = login_attempt_key(request, payload.login)
    ensure_login_not_locked(login_key)
    try:
        identity = authenticate_web_user(settings, payload.login, payload.password, db=db)
        if identity.user_id is not None and settings.identity_auth_enabled:
            user = db.get(User, identity.user_id)
            issued = create_user_session(
                db,
                user,
                expires_at=datetime.now(timezone.utc) + timedelta(seconds=settings.web_session_ttl_seconds),
            )
            token = issued.token
            verified = validate_user_session(db, token)
            db.commit()
            session_payload = {
                "sub": verified.username,
                "role": verified.role,
                "exp": int(verified.expires_at.timestamp()),
                "sid": str(verified.session_id),
                "uid": str(verified.user_id),
                "av": verified.auth_version,
            }
        else:
            if not legacy_auth_window_active():
                raise WebAuthError("legacy web auth is disabled")
            token = create_session_token(settings, identity.login, role=identity.role)
            session_payload = verify_session_token(settings, token)
    except (WebAuthError, IdentityAuthError) as exc:
        db.rollback()
        register_login_failure(login_key)
        if "configured" in str(exc):
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Web auth is not configured") from exc
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials") from exc

    clear_login_failures(login_key)
    response.set_cookie(
        SESSION_COOKIE_NAME,
        token,
        max_age=settings.web_session_ttl_seconds,
        path="/",
        httponly=True,
        secure=settings.web_cookie_secure,
        samesite="lax",
    )
    return auth_session_read(session_payload, token)


def login_attempt_key(request: Request, login):
    ip = client_identity(request, settings.trusted_proxy_cidrs)
    login_digest = hashlib.sha256()
    for character in str(login or ""):
        if character.isdigit() or character == "+":
            login_digest.update(character.encode("utf-8"))
    return f"{ip}:{login_digest.hexdigest()}"


def client_identity(request, trusted_proxy_cidrs=()):
    peer_text = str(request.client.host if request.client else "unknown").strip()
    try:
        peer = ipaddress.ip_address(peer_text)
    except ValueError:
        return "unknown"

    networks = []
    for cidr in trusted_proxy_cidrs or ():
        try:
            networks.append(ipaddress.ip_network(str(cidr), strict=False))
        except ValueError:
            return str(peer)

    def is_trusted(address):
        return any(address.version == network.version and address in network for network in networks)

    if not is_trusted(peer):
        return str(peer)

    parts = [part.strip() for part in str(request.headers.get("x-forwarded-for") or "").split(",")]
    if not parts or not parts[0] or len(parts) > 32:
        return str(peer)
    try:
        forwarded = [ipaddress.ip_address(part) for part in parts]
    except ValueError:
        return str(peer)

    for address in reversed(forwarded):
        if not is_trusted(address):
            return str(address)
    return str(forwarded[0])


def ensure_login_not_locked(key):
    try:
        login_limiter.ensure_not_locked(key)
    except (LoginRateLimited, LoginLimiterCapacityExceeded) as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many login attempts",
        ) from exc


def register_login_failure(key):
    try:
        login_limiter.register_failure(
            key,
            max_attempts=settings.web_login_max_attempts,
            window_seconds=settings.web_login_window_seconds,
            lock_seconds=settings.web_login_lock_seconds,
        )
    except (LoginRateLimited, LoginLimiterCapacityExceeded) as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many login attempts",
        ) from exc


def clear_login_failures(key):
    login_limiter.clear(key)


@auth_api.post("/logout", response_model=AuthSessionRead)
def web_logout(request: Request, response: Response, db=Depends(get_db)):
    prevent_auth_response_caching(response)
    token = request.cookies.get(SESSION_COOKIE_NAME)
    try:
        read_web_session(request, db=db, touch_last_used=False)
    except (WebAuthError, IdentityAuthError) as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated") from exc
    require_browser_request_security(request)
    if settings.identity_auth_enabled and str(token or "").startswith("tks."):
        try:
            revoke_user_session(db, token)
            db.commit()
        except IdentityAuthError:
            db.rollback()
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    return AuthSessionRead(authenticated=False)


@auth_api.get("/session", response_model=AuthSessionRead)
def web_session(request: Request, response: Response, db=Depends(get_db)):
    prevent_auth_response_caching(response)
    try:
        token = request.cookies.get(SESSION_COOKIE_NAME) or ""
        return auth_session_read(read_web_session(request, db=db, touch_last_used=False), token)
    except (WebAuthError, IdentityAuthError):
        db.rollback()
        return AuthSessionRead(authenticated=False)


@auth_api.get("/check")
def web_auth_check(request: Request, db=Depends(get_db)):
    try:
        read_web_session(request, db=db, touch_last_used=False)
    except (WebAuthError, IdentityAuthError) as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated") from exc
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    prevent_auth_response_caching(response)
    return response


app.include_router(auth_api)


api = APIRouter(prefix="/api/v1", dependencies=[Depends(require_service_token)])


@api.get("/orders/active")
def list_active_orders(
    response: Response,
    limit: int = 50,
    cursor: str = "",
    db=Depends(get_db),
) -> list[OrderRead]:
    rows, next_cursor, row_limit = list_active_orders_page_in_db(db, limit=limit, cursor=cursor)
    set_pagination_headers(response, next_cursor=next_cursor, limit=row_limit)
    return rows


@api.get("/admin/table", response_model=AdminTableRead)
def admin_table(
    response: Response,
    limit: int = 500,
    offset: int = 0,
    cursor: str = "",
    activity_limit: int = 30,
    status_bucket: str = "",
    shipment_date: str = "",
    search: str = "",
    scan_state: str = "",
    skladbot_filter: str = "",
    google_status: str = "",
    google_sheet_status: str = "",
    db=Depends(get_db),
):
    row_limit = normalize_page_limit(limit, default=500, maximum=500)
    row_offset = max(0, int(offset or 0))
    if cursor:
        if row_offset:
            raise CursorError("invalid_cursor")
    result = build_admin_table(
        db,
        limit=row_limit,
        offset=row_offset,
        activity_limit=activity_limit,
        status_bucket=status_bucket,
        shipment_date=shipment_date,
        search=search,
        scan_state=scan_state,
        skladbot_filter=skladbot_filter,
        google_status=google_sheet_status or google_status,
        cursor=cursor,
    )
    set_pagination_headers(response, next_cursor=result.next_cursor, limit=row_limit)
    return result


@api.get("/admin/dashboard/day-summary", response_model=DashboardDaySummaryRead)
def admin_dashboard_day_summary(report_date: str | None = None, db=Depends(get_db)):
    try:
        return build_dashboard_day_summary(db, report_date)
    except ApiError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


@api.get("/admin/client-points", response_model=list[ClientPointRead])
def admin_client_points(
    response: Response,
    query: str = "",
    custom_timeslot: bool | None = None,
    limit: int = 2000,
    cursor: str = "",
    db=Depends(get_db),
):
    row_limit = normalize_page_limit(limit, default=2000, maximum=2000)
    filters = {"query": query, "custom_timeslot": custom_timeslot}
    offset = 0
    if cursor:
        try:
            (offset_value,) = decode_cursor(cursor, "admin.client_points", filters=filters)
            offset = int(offset_value)
            if offset < 0:
                raise ValueError
        except (CursorError, TypeError, ValueError):
            raise CursorError("invalid_cursor") from None
    loaded = list_client_points_in_db(
        db,
        query=query,
        custom_timeslot=custom_timeslot,
        limit=row_limit + 1,
        offset=offset,
    )
    rows = loaded[:row_limit]
    next_cursor = ""
    if len(loaded) > row_limit:
        next_cursor = encode_cursor(
            "admin.client_points",
            [offset + len(rows)],
            filters=filters,
        )
    set_pagination_headers(response, next_cursor=next_cursor, limit=row_limit)
    return rows


@api.get("/admin/client-points/order-summary", response_model=ClientPointOrderSummaryRead)
def admin_client_point_order_summary(
    client_name: str,
    date_from: date | None = None,
    date_to: date | None = None,
    db=Depends(get_db),
):
    try:
        return get_client_point_order_summary(db, client_name=client_name, date_from=date_from, date_to=date_to)
    except ClientPointApiError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


@api.get("/admin/logistics-calendar", response_model=LogisticsCalendarRead)
def admin_logistics_calendar(month: str | None = None, db=Depends(get_db)):
    try:
        return list_logistics_calendar_in_db(db, month=month)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@api.post(
    "/admin/logistics-calendar/day",
    response_model=LogisticsCalendarDayRead,
    dependencies=[Depends(require_admin_write_permission)],
)
def admin_update_logistics_calendar_day(payload: LogisticsCalendarDayUpdate, db=Depends(get_db)):
    return set_logistics_calendar_day_in_db(db, payload)


@api.post(
    "/admin/client-points/timeslot",
    response_model=ClientPointRead,
    dependencies=[Depends(require_client_points_write_permission)],
)
def admin_update_client_point_timeslot(payload: ClientPointTimeslotUpdate, db=Depends(get_db)):
    try:
        return update_client_point_timeslot_in_db(db, payload)
    except ClientPointApiError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


@api.post("/admin/google/pending/retry", dependencies=[Depends(require_admin_write_permission)])
def retry_pending_google_exports(limit: int = 50, db=Depends(get_db)):
    return process_pending_google_sheets_exports(db, limit=limit)


@api.get("/admin/events", response_model=EventQueueDiagnosticsRead)
def admin_event_queue(
    response: Response,
    limit: int = 200,
    cursor: str = "",
    db=Depends(get_db),
):
    result = list_event_queue_diagnostics(
        db,
        limit=normalize_page_limit(limit, default=200, maximum=200),
        cursor=cursor,
    )
    set_pagination_headers(response, next_cursor=result["next_cursor"], limit=result["limit"])
    return result


@api.get("/admin/operations", response_model=OperationsAttentionRead)
def admin_operations(db=Depends(get_db)):
    return build_operations_attention(db, settings)


@api.get("/admin/smartup-auto-imports/history", response_model=SmartupAutoImportHistoryRead)
def admin_smartup_auto_import_history(
    response: Response,
    limit: int = 50,
    cursor: str = "",
    db=Depends(get_db),
):
    result = list_smartup_auto_import_history(
        db,
        limit=normalize_page_limit(limit),
        cursor=cursor,
    )
    set_pagination_headers(response, next_cursor=result["next_cursor"], limit=result["limit"])
    return result


@api.get("/admin/events/{event_id}", response_model=EventQueueEventRead)
def admin_event_queue_detail(event_id: str, db=Depends(get_db)):
    try:
        return get_event_queue_detail_from_db(db, event_id)
    except EventQueueApiError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


@api.post(
    "/admin/events/{event_id}/retry",
    response_model=EventQueueEventRead,
    dependencies=[Depends(require_admin_write_permission)],
)
def admin_event_queue_retry(event_id: str, payload: EventQueueActionRequest, db=Depends(get_db)):
    try:
        return retry_event_queue_event_in_db(db, event_id, payload)
    except EventQueueApiError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


@api.post(
    "/admin/incidents",
    response_model=IncidentRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_admin_write_permission)],
)
def admin_create_incident(payload: IncidentCreate, db=Depends(get_db)):
    try:
        return create_incident_in_db(db, payload)
    except IncidentApiError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


@api.get("/admin/incidents", response_model=IncidentListRead)
def admin_incidents(
    response: Response,
    status: str | None = None,
    severity: str | None = None,
    source: str | None = None,
    entity_type: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = 200,
    cursor: str = "",
    db=Depends(get_db),
):
    try:
        result = list_incidents_in_db(
            db,
            status=status,
            severity=severity,
            source=source,
            entity_type=entity_type,
            date_from=date_from,
            date_to=date_to,
            limit=normalize_page_limit(limit, default=200, maximum=200),
            cursor=cursor,
        )
        set_pagination_headers(
            response,
            next_cursor=result["next_cursor"],
            limit=result["limit"],
        )
        return result
    except IncidentApiError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


@api.get("/admin/incidents/{incident_id}", response_model=IncidentRead)
def admin_incident_detail(incident_id: str, db=Depends(get_db)):
    try:
        return get_incident_from_db(db, incident_id)
    except IncidentApiError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


@api.post(
    "/admin/incidents/{incident_id}/status",
    response_model=IncidentRead,
    dependencies=[Depends(require_admin_write_permission)],
)
def admin_update_incident_status(incident_id: str, payload: IncidentStatusUpdate, db=Depends(get_db)):
    try:
        return update_incident_status_in_db(db, incident_id, payload)
    except IncidentApiError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


@api.get(
    "/readiness",
    response_model=ReadinessResponse,
    dependencies=[Depends(require_service_token)],
)
def api_readiness(response: Response, db=Depends(get_db)):
    report = build_readiness_report(db, settings)
    response.status_code = readiness_http_status(report)
    return report


@api.post(
    "/admin/orders/bulk/complete-without-kiz",
    response_model=AdminBulkOrderActionResult,
    dependencies=[Depends(require_admin_write_permission)],
)
def complete_orders_without_kiz(payload: AdminBulkOrderActionRequest, db=Depends(get_db)):
    try:
        return complete_orders_without_kiz_in_db(db, payload)
    except ApiError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


@api.post(
    "/admin/orders/{order_id}/archive-without-kiz",
    response_model=OrderRead,
    dependencies=[Depends(require_admin_write_permission)],
)
def archive_order_without_kiz(order_id: str, payload: AdminOrderActionRequest, db=Depends(get_db)):
    try:
        return archive_order_without_kiz_in_db(db, order_id, payload)
    except ApiError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


@api.post(
    "/admin/orders/{order_id}/cancel",
    response_model=OrderRead,
    dependencies=[Depends(require_admin_write_permission)],
)
def cancel_order(order_id: str, payload: AdminOrderActionRequest, db=Depends(get_db)):
    try:
        return cancel_order_in_db(db, order_id, payload)
    except ApiError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


@api.post(
    "/admin/orders/{order_id}/delete-active",
    response_model=ActiveOrderDeleteResult,
    dependencies=[Depends(require_admin_write_permission)],
)
def delete_active_order(order_id: str, payload: AdminOrderActionRequest, db=Depends(get_db)):
    try:
        return delete_active_order_in_db(db, order_id, payload)
    except ApiError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


@api.post(
    "/admin/orders/{order_id}/resync-google",
    response_model=OrderRead,
    dependencies=[Depends(require_admin_write_permission)],
)
def resync_order_to_google(order_id: str, payload: AdminOrderActionRequest | None = None, db=Depends(get_db)):
    try:
        return resync_order_to_google_in_db(db, order_id, payload)
    except ApiError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


@api.post(
    "/admin/orders/{order_id}/reset-rescan",
    response_model=OrderRead,
    dependencies=[Depends(require_admin_write_permission)],
)
def reset_order_for_rescan(order_id: str, payload: AdminOrderActionRequest, db=Depends(get_db)):
    try:
        return reset_order_for_rescan_in_db(db, order_id, payload)
    except ApiError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


@api.post(
    "/admin/orders/{order_id}/restore",
    response_model=OrderRead,
    dependencies=[Depends(require_admin_write_permission)],
)
def restore_order(order_id: str, payload: AdminOrderActionRequest, db=Depends(get_db)):
    try:
        return restore_order_in_db(db, order_id, payload)
    except ApiError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


@api.post(
    "/admin/orders/{order_id}/resync-skladbot",
    response_model=OrderRead,
    dependencies=[Depends(require_admin_write_permission)],
)
def resync_order_skladbot(order_id: str, payload: AdminOrderActionRequest, db=Depends(get_db)):
    try:
        return resync_order_skladbot_in_db(db, order_id, payload)
    except ApiError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


@api.get("/admin/skladbot/dry-runs", response_model=list[SkladBotDryRunRead])
def admin_skladbot_dry_runs(
    response: Response,
    import_id: str | None = None,
    limit: int = 50,
    cursor: str = "",
    db=Depends(get_db),
):
    row_limit = normalize_page_limit(limit)
    filters = {"import_id": import_id or ""}
    offset = 0
    if cursor:
        try:
            (offset_value,) = decode_cursor(cursor, "admin.skladbot_dry_runs", filters=filters)
            offset = int(offset_value)
            if offset < 0:
                raise ValueError
        except (CursorError, TypeError, ValueError):
            raise CursorError("invalid_cursor") from None
    loaded = list_skladbot_dry_runs(
        db,
        import_id=import_id,
        limit=min(row_limit + 1, 200),
        offset=offset,
    )
    rows = loaded[:row_limit]
    next_cursor = ""
    if len(loaded) > row_limit:
        next_cursor = encode_cursor(
            "admin.skladbot_dry_runs",
            [offset + len(rows)],
            filters=filters,
        )
    set_pagination_headers(response, next_cursor=next_cursor, limit=row_limit)
    return rows


@api.post(
    "/admin/skladbot/dry-runs/{dry_run_id}/rebuild",
    response_model=list[SkladBotDryRunRead],
    dependencies=[Depends(require_admin_write_permission)],
)
def admin_rebuild_skladbot_dry_run(dry_run_id: str, db=Depends(get_db)):
    try:
        return rebuild_skladbot_dry_run(db, dry_run_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@api.post("/sync/sources", dependencies=[Depends(require_admin_write_permission)])
def sync_sources(skladbot: bool = True, wait_skladbot: bool = False, db=Depends(get_db)):
    if not sync_sources_lock.acquire(blocking=False):
        return {
            "status": "busy",
            "google_sheets": {"status": "skipped", "message": "Sync already in progress"},
            "skladbot": {"status": "skipped", "message": "Sync already in progress"},
        }

    audit_actor = db.info.get(AUDIT_ACTOR_INFO_KEY)
    if audit_actor is not None:
        db.add(AuditLog(
            action="sync_sources_requested",
            entity_type="sync",
            entity_id="sources",
            payload={"skladbot": bool(skladbot), "wait_skladbot": bool(wait_skladbot)},
        ))
        db.commit()
    errors = []
    try:
        try:
            pending_google_result = process_pending_google_sheets_exports(db)
            pending_google_result = {"status": "completed", **pending_google_result}
        except Exception as exc:
            pending_google_result = {"status": "error", "error": str(exc)}
            errors.append("google_sheets_pending")

        if settings.google_to_backend_sync_enabled:
            try:
                google_sheets_result = sync_google_sheet_to_backend(db)
                google_sheets_result = {"status": "completed", **google_sheets_result}
            except Exception as exc:
                google_sheets_result = {"status": "error", "error": str(exc)}
                errors.append("google_sheets")
        else:
            google_sheets_result = {
                "status": "skipped",
                "message": "Google -> backend sync is disabled; VDS/Postgres is source of truth",
            }

        if skladbot and wait_skladbot:
            try:
                skladbot_result = update_orders_from_skladbot(**({"audit_actor": audit_actor} if audit_actor else {}))
                skladbot_result = {"status": "completed", **skladbot_result}
            except Exception as exc:
                skladbot_result = {"status": "error", "error": str(exc)}
                errors.append("skladbot")
        elif skladbot:
            skladbot_result = start_skladbot_sync_background(audit_actor=audit_actor)
        else:
            skladbot_result = {"status": "skipped"}

        return {
            "status": "completed_with_errors" if errors else "completed",
            "errors": errors,
            "google_sheets_pending": pending_google_result,
            "google_sheets": google_sheets_result,
            "skladbot": skladbot_result,
        }
    finally:
        sync_sources_lock.release()


def start_skladbot_sync_background(audit_actor=None):
    if not skladbot_sync_lock.acquire(blocking=False):
        return {"status": "busy", "message": "SkladBot sync already in progress"}

    def worker():
        try:
            update_orders_from_skladbot(**({"audit_actor": audit_actor} if audit_actor else {}))
        finally:
            skladbot_sync_lock.release()

    Thread(target=worker, daemon=True).start()
    return {"status": "started", "message": "SkladBot sync started in background"}


@api.get("/returns", response_model=list[OrderRead])
def list_returns(
    response: Response,
    limit: int = 50,
    cursor: str = "",
    db=Depends(get_db),
):
    row_limit = normalize_page_limit(limit)
    offset = 0
    if cursor:
        try:
            (offset_value,) = decode_cursor(cursor, "returns")
            offset = int(offset_value)
            if offset < 0:
                raise ValueError
        except (CursorError, TypeError, ValueError):
            raise CursorError("invalid_cursor") from None
    loaded = list_returned_orders_in_db(db, limit=row_limit + 1, offset=offset)
    rows = loaded[:row_limit]
    next_cursor = encode_cursor("returns", [offset + len(rows)]) if len(loaded) > row_limit else ""
    set_pagination_headers(response, next_cursor=next_cursor, limit=row_limit)
    return rows


@api.post(
    "/scans",
    response_model=ScanRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_admin_write_permission)],
)
def create_scan(payload: ScanCreate, db=Depends(get_db)):
    try:
        return create_scan_in_db(db, payload)
    except ApiError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


@api.get("/kiz/availability", response_model=KizAvailabilityRead)
def lookup_kiz_availability(code: str, order_item_id: str = "", db=Depends(get_db)):
    try:
        return lookup_kiz_availability_in_db(db, code, order_item_id=order_item_id)
    except ApiError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


@api.post("/scans/undo", response_model=ScanRead, dependencies=[Depends(require_admin_write_permission)])
def undo_scan(payload: ScanUndo, db=Depends(get_db)):
    try:
        return undo_scan_in_db(db, payload)
    except ApiError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


@api.post("/orders/{order_id}/complete", response_model=OrderRead, dependencies=[Depends(require_admin_write_permission)])
def complete_order(order_id: str, db=Depends(get_db)):
    try:
        return complete_order_in_db(db, order_id)
    except ApiError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


@api.get("/returns/lookup", response_model=OrderRead)
def lookup_return(lookup: str, db=Depends(get_db)):
    try:
        return lookup_return_order_in_db(db, lookup)
    except ApiError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


@api.post("/returns/{order_id}", response_model=OrderRead, dependencies=[Depends(require_admin_write_permission)])
def mark_return(order_id: str, payload: ReturnMarkRequest, db=Depends(get_db)):
    try:
        return mark_order_returned_in_db(
            db,
            order_id,
            return_reference=payload.return_reference or "",
            returned_by=payload.returned_by or "desktop",
            confirmed_items=[item.model_dump() for item in payload.confirmed_items],
        )
    except ApiError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


@api.post(
    "/imports",
    response_model=ImportResult,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_admin_write_permission)],
)
def create_import(payload: ImportCreate, db=Depends(get_db)):
    return create_import_in_db(db, payload)


@api.post("/imports/preview", response_model=ImportPreviewResult, dependencies=[Depends(require_admin_write_permission)])
def preview_import(payload: ImportCreate, db=Depends(get_db)):
    return preview_import_in_db(db, payload)


@api.get("/imports", response_model=list[ImportRead])
def list_imports(
    response: Response,
    limit: int = 50,
    cursor: str = "",
    import_id: str = "",
    telegram_event_id: str = "",
    db=Depends(get_db),
):
    rows, next_cursor, row_limit = list_imports_page_in_db(
        db,
        limit=limit,
        cursor=cursor,
        import_id=import_id,
        telegram_event_id=telegram_event_id,
    )
    set_pagination_headers(response, next_cursor=next_cursor, limit=row_limit)
    return rows


@api.get("/reports/day", response_model=DayReportRead)
def day_report(report_date: str | None = None, db=Depends(get_db)):
    try:
        return build_day_report(db, report_date)
    except ApiError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


@api.get("/reports/reconciliation/day", dependencies=[Depends(require_admin_write_permission)])
def reconciliation_day_report(report_date: str | None = None, db=Depends(get_db)):
    try:
        return preview_daily_reconciliation(db=db, report_date=report_date)
    except ReconciliationError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


@api.post("/reports/reconciliation/day", dependencies=[Depends(require_admin_write_permission)])
def execute_reconciliation_day_report(report_date: str | None = None, db=Depends(get_db)):
    try:
        return run_daily_reconciliation(db=db, report_date=report_date, alert_chat_ids=[])
    except ReconciliationError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


@api.get("/reports/kiz/source-files")
def kiz_source_files(
    response: Response,
    limit: int = 50,
    cursor: str = "",
    db=Depends(get_db),
) -> list[dict]:
    return paginate_materialized(
        list_completed_kiz_source_files(db),
        scope="reports.kiz.source_files",
        response=response,
        limit=limit,
        cursor=cursor,
    )


@api.get("/reports/kiz/dates")
def kiz_dates(
    response: Response,
    limit: int = 90,
    cursor: str = "",
    db=Depends(get_db),
) -> list[dict]:
    return paginate_materialized(
        list_completed_kiz_dates(db),
        scope="reports.kiz.dates",
        response=response,
        limit=limit,
        cursor=cursor,
        default=90,
    )


@api.get("/reports/kiz/date")
def kiz_date_report(shipment_date: str, db=Depends(get_db)):
    try:
        content, filename = build_kiz_date_report_xlsx(db, shipment_date)
    except ApiError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}",
            "X-TakSklad-Filename": quote(filename),
        },
    )


@api.get("/reports/kiz/range")
def kiz_date_range_report(date_from: str, date_to: str, db=Depends(get_db)):
    try:
        content, filename = build_kiz_date_range_report_xlsx(db, date_from, date_to)
    except ApiError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}",
            "X-TakSklad-Filename": quote(filename),
        },
    )


@api.get("/reports/kiz/source-file")
def kiz_source_file_report(source_file: str, source_key: str | None = None, db=Depends(get_db)):
    try:
        content, filename = build_kiz_source_file_report_xlsx(db, source_file, source_key or "")
    except ApiError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}",
            "X-TakSklad-Filename": quote(filename),
        },
    )


@api.get("/logistics/dates")
def logistics_dates(
    response: Response,
    limit: int = 31,
    cursor: str = "",
    db=Depends(get_db),
) -> list[str]:
    return paginate_materialized(
        list_logistics_dates(db),
        scope="logistics.dates",
        response=response,
        limit=limit,
        cursor=cursor,
        default=31,
    )


@api.get("/logistics/report")
def logistics_report(shipment_date: str, db=Depends(get_db)):
    try:
        content, filename = build_logistics_report_xlsx(db, shipment_date)
    except ApiError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}",
            "X-TakSklad-Filename": quote(filename),
        },
    )


@api.get("/diagnostics/logs")
def diagnostics_logs(limit: int = 100, db=Depends(get_db)):
    content, filename = build_backend_diagnostics_log(
        db,
        limit=normalize_page_limit(limit, default=100, maximum=500),
    )
    return Response(
        content=content,
        media_type="text/plain; charset=utf-8",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}",
            "X-TakSklad-Filename": quote(filename),
        },
    )


app.include_router(api)
