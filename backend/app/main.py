import time
from urllib.parse import quote
from threading import Lock, Thread
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware

from .admin_service import build_admin_table
from .db import get_db
from .diagnostics_service import build_backend_diagnostics_log
from .event_queue_service import (
    EventQueueApiError,
    get_event_queue_detail as get_event_queue_detail_from_db,
    list_event_queue_diagnostics,
    retry_event_queue_event as retry_event_queue_event_in_db,
)
from .google_sheets_sync_worker import sync_google_sheet_to_backend
from .google_sheets_pending import process_pending_google_sheets_exports
from .health_service import build_readiness_report
from .incidents_service import (
    IncidentApiError,
    create_incident as create_incident_in_db,
    get_incident as get_incident_from_db,
    list_incidents as list_incidents_in_db,
    update_incident_status as update_incident_status_in_db,
)
from .imports_service import create_import as create_import_in_db
from .imports_service import list_imports as list_imports_in_db
from .kiz_reports_service import (
    build_kiz_date_range_report_xlsx,
    build_kiz_date_report_xlsx,
    build_kiz_source_file_report_xlsx,
    list_completed_kiz_dates,
    list_completed_kiz_source_files,
)
from .logistics_service import build_logistics_report_xlsx, list_logistics_dates
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
from .orders_service import ApiError, complete_order as complete_order_in_db
from .orders_service import create_scan as create_scan_in_db
from .orders_service import list_active_orders as list_active_orders_in_db
from .orders_service import list_returned_orders as list_returned_orders_in_db
from .orders_service import lookup_return_order as lookup_return_order_in_db
from .orders_service import mark_order_returned as mark_order_returned_in_db
from .orders_service import undo_scan as undo_scan_in_db
from .reconciliation_service import ReconciliationError, run_daily_reconciliation
from .reports_service import build_day_report
from .skladbot_request_dry_run import list_skladbot_dry_runs, rebuild_skladbot_dry_run
from .skladbot_worker import update_orders_from_skladbot
from .schemas import (
    AdminOrderActionRequest,
    AdminBulkOrderActionRequest,
    AdminBulkOrderActionResult,
    AdminTableRead,
    ActiveOrderDeleteResult,
    AuthLoginRequest,
    AuthSessionRead,
    DayReportRead,
    EventQueueDiagnosticsRead,
    EventQueueActionRequest,
    EventQueueEventRead,
    HealthResponse,
    ImportCreate,
    ImportRead,
    ImportResult,
    IncidentCreate,
    IncidentListRead,
    IncidentRead,
    IncidentStatusUpdate,
    OrderRead,
    ReadinessResponse,
    ReturnMarkRequest,
    ScanCreate,
    ScanRead,
    ScanUndo,
    SkladBotDryRunRead,
)
from .settings import APP_VERSION, load_settings
from .web_auth import (
    SESSION_COOKIE_NAME,
    WebAuthError,
    authenticate_web_user,
    create_session_token,
    verify_session_token,
)


settings = load_settings()
sync_sources_lock = Lock()
skladbot_sync_lock = Lock()
login_attempts_lock = Lock()
login_attempts = {}

app = FastAPI(
    title="TakSklad Backend API",
    version=APP_VERSION,
    docs_url="/docs",
    redoc_url="/redoc",
)


def configure_cors(app_instance: FastAPI, app_settings) -> None:
    if not app_settings.cors_origins:
        return

    app_instance.add_middleware(
        CORSMiddleware,
        allow_origins=list(app_settings.cors_origins),
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
    )


configure_cors(app, settings)


def is_valid_service_token(authorization: str | None) -> bool:
    if not settings.api_auth_enabled:
        return True
    expected = f"Bearer {settings.api_token}"
    return authorization == expected


def require_service_token(request: Request, authorization: str | None = Header(default=None)):
    if is_valid_service_token(authorization):
        return
    try:
        read_web_session(request)
        return
    except WebAuthError:
        pass
    if not settings.api_auth_enabled:
        return
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid service token or web session",
    )


@app.get("/health", response_model=HealthResponse)
def health():
    return {
        "status": "ok",
        "service": settings.service_name,
        "version": APP_VERSION,
        "environment": settings.environment,
    }


@app.get("/ready", response_model=ReadinessResponse)
def readiness(db=Depends(get_db)):
    return build_readiness_report(db, settings)


auth_api = APIRouter(prefix="/api/v1/auth")


def auth_session_read(payload):
    expires_at = datetime.fromtimestamp(int(payload.get("exp") or 0), timezone.utc)
    return AuthSessionRead(authenticated=True, login=payload.get("sub") or "", expires_at=expires_at)


def read_web_session(request: Request):
    return verify_session_token(settings, request.cookies.get(SESSION_COOKIE_NAME))


@auth_api.post("/login", response_model=AuthSessionRead)
def web_login(payload: AuthLoginRequest, request: Request, response: Response):
    login_key = login_attempt_key(request, payload.login)
    ensure_login_not_locked(login_key)
    try:
        login = authenticate_web_user(settings, payload.login, payload.password)
        token = create_session_token(settings, login)
        session_payload = verify_session_token(settings, token)
    except WebAuthError as exc:
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
    return auth_session_read(session_payload)


def login_attempt_key(request: Request, login):
    forwarded = request.headers.get("x-forwarded-for") or ""
    ip = forwarded.split(",", 1)[0].strip() or (request.client.host if request.client else "unknown")
    normalized_login = "".join(ch for ch in str(login or "").strip() if ch.isdigit() or ch == "+")
    return f"{ip}:{normalized_login}"


def ensure_login_not_locked(key):
    now = time.time()
    with login_attempts_lock:
        record = login_attempts.get(key) or {}
        locked_until = float(record.get("locked_until") or 0)
        if locked_until > now:
            raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Too many login attempts")


def register_login_failure(key):
    now = time.time()
    with login_attempts_lock:
        record = login_attempts.get(key) or {}
        window_start = float(record.get("window_start") or now)
        if now - window_start > settings.web_login_window_seconds:
            record = {"window_start": now, "count": 0, "locked_until": 0}
        record["count"] = int(record.get("count") or 0) + 1
        if record["count"] >= settings.web_login_max_attempts:
            record["locked_until"] = now + settings.web_login_lock_seconds
        login_attempts[key] = record


def clear_login_failures(key):
    with login_attempts_lock:
        login_attempts.pop(key, None)


@auth_api.post("/logout", response_model=AuthSessionRead)
def web_logout(response: Response):
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    return AuthSessionRead(authenticated=False)


@auth_api.get("/session", response_model=AuthSessionRead)
def web_session(request: Request):
    try:
        return auth_session_read(read_web_session(request))
    except WebAuthError:
        return AuthSessionRead(authenticated=False)


@auth_api.get("/check")
def web_auth_check(request: Request):
    try:
        read_web_session(request)
    except WebAuthError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated") from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


app.include_router(auth_api)


api = APIRouter(prefix="/api/v1", dependencies=[Depends(require_service_token)])


@api.get("/orders/active")
def list_active_orders(db=Depends(get_db)) -> list[OrderRead]:
    return list_active_orders_in_db(db)


@api.get("/admin/table", response_model=AdminTableRead)
def admin_table(limit: int = 5000, activity_limit: int = 30, db=Depends(get_db)):
    return build_admin_table(db, limit=limit, activity_limit=activity_limit)


@api.post("/admin/google/pending/retry")
def retry_pending_google_exports(limit: int = 50, db=Depends(get_db)):
    return process_pending_google_sheets_exports(db, limit=limit)


@api.get("/admin/events", response_model=EventQueueDiagnosticsRead)
def admin_event_queue(limit: int = 100, db=Depends(get_db)):
    return list_event_queue_diagnostics(db, limit=limit)


@api.get("/admin/events/{event_id}", response_model=EventQueueEventRead)
def admin_event_queue_detail(event_id: str, db=Depends(get_db)):
    try:
        return get_event_queue_detail_from_db(db, event_id)
    except EventQueueApiError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


@api.post("/admin/events/{event_id}/retry", response_model=EventQueueEventRead)
def admin_event_queue_retry(event_id: str, payload: EventQueueActionRequest, db=Depends(get_db)):
    try:
        return retry_event_queue_event_in_db(db, event_id, payload)
    except EventQueueApiError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


@api.post("/admin/incidents", response_model=IncidentRead, status_code=status.HTTP_201_CREATED)
def admin_create_incident(payload: IncidentCreate, db=Depends(get_db)):
    try:
        return create_incident_in_db(db, payload)
    except IncidentApiError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


@api.get("/admin/incidents", response_model=IncidentListRead)
def admin_incidents(
    status: str | None = None,
    severity: str | None = None,
    source: str | None = None,
    entity_type: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = 100,
    db=Depends(get_db),
):
    try:
        return list_incidents_in_db(
            db,
            status=status,
            severity=severity,
            source=source,
            entity_type=entity_type,
            date_from=date_from,
            date_to=date_to,
            limit=limit,
        )
    except IncidentApiError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


@api.get("/admin/incidents/{incident_id}", response_model=IncidentRead)
def admin_incident_detail(incident_id: str, db=Depends(get_db)):
    try:
        return get_incident_from_db(db, incident_id)
    except IncidentApiError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


@api.post("/admin/incidents/{incident_id}/status", response_model=IncidentRead)
def admin_update_incident_status(incident_id: str, payload: IncidentStatusUpdate, db=Depends(get_db)):
    try:
        return update_incident_status_in_db(db, incident_id, payload)
    except IncidentApiError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


@api.get("/readiness", response_model=ReadinessResponse)
def api_readiness(db=Depends(get_db)):
    return build_readiness_report(db, settings)


@api.post("/admin/orders/bulk/complete-without-kiz", response_model=AdminBulkOrderActionResult)
def complete_orders_without_kiz(payload: AdminBulkOrderActionRequest, db=Depends(get_db)):
    try:
        return complete_orders_without_kiz_in_db(db, payload)
    except ApiError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


@api.post("/admin/orders/{order_id}/archive-without-kiz", response_model=OrderRead)
def archive_order_without_kiz(order_id: str, payload: AdminOrderActionRequest, db=Depends(get_db)):
    try:
        return archive_order_without_kiz_in_db(db, order_id, payload)
    except ApiError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


@api.post("/admin/orders/{order_id}/cancel", response_model=OrderRead)
def cancel_order(order_id: str, payload: AdminOrderActionRequest, db=Depends(get_db)):
    try:
        return cancel_order_in_db(db, order_id, payload)
    except ApiError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


@api.post("/admin/orders/{order_id}/delete-active", response_model=ActiveOrderDeleteResult)
def delete_active_order(order_id: str, payload: AdminOrderActionRequest, db=Depends(get_db)):
    try:
        return delete_active_order_in_db(db, order_id, payload)
    except ApiError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


@api.post("/admin/orders/{order_id}/resync-google", response_model=OrderRead)
def resync_order_to_google(order_id: str, payload: AdminOrderActionRequest | None = None, db=Depends(get_db)):
    try:
        return resync_order_to_google_in_db(db, order_id, payload)
    except ApiError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


@api.post("/admin/orders/{order_id}/reset-rescan", response_model=OrderRead)
def reset_order_for_rescan(order_id: str, payload: AdminOrderActionRequest, db=Depends(get_db)):
    try:
        return reset_order_for_rescan_in_db(db, order_id, payload)
    except ApiError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


@api.post("/admin/orders/{order_id}/restore", response_model=OrderRead)
def restore_order(order_id: str, payload: AdminOrderActionRequest, db=Depends(get_db)):
    try:
        return restore_order_in_db(db, order_id, payload)
    except ApiError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


@api.post("/admin/orders/{order_id}/resync-skladbot", response_model=OrderRead)
def resync_order_skladbot(order_id: str, payload: AdminOrderActionRequest, db=Depends(get_db)):
    try:
        return resync_order_skladbot_in_db(db, order_id, payload)
    except ApiError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


@api.get("/admin/skladbot/dry-runs", response_model=list[SkladBotDryRunRead])
def admin_skladbot_dry_runs(import_id: str | None = None, db=Depends(get_db)):
    return list_skladbot_dry_runs(db, import_id=import_id)


@api.post("/admin/skladbot/dry-runs/{dry_run_id}/rebuild", response_model=list[SkladBotDryRunRead])
def admin_rebuild_skladbot_dry_run(dry_run_id: str, db=Depends(get_db)):
    try:
        return rebuild_skladbot_dry_run(db, dry_run_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@api.post("/sync/sources")
def sync_sources(skladbot: bool = True, wait_skladbot: bool = False, db=Depends(get_db)):
    if not sync_sources_lock.acquire(blocking=False):
        return {
            "status": "busy",
            "google_sheets": {"status": "skipped", "message": "Sync already in progress"},
            "skladbot": {"status": "skipped", "message": "Sync already in progress"},
        }

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
                skladbot_result = update_orders_from_skladbot()
                skladbot_result = {"status": "completed", **skladbot_result}
            except Exception as exc:
                skladbot_result = {"status": "error", "error": str(exc)}
                errors.append("skladbot")
        elif skladbot:
            skladbot_result = start_skladbot_sync_background()
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


def start_skladbot_sync_background():
    if not skladbot_sync_lock.acquire(blocking=False):
        return {"status": "busy", "message": "SkladBot sync already in progress"}

    def worker():
        try:
            update_orders_from_skladbot()
        finally:
            skladbot_sync_lock.release()

    Thread(target=worker, daemon=True).start()
    return {"status": "started", "message": "SkladBot sync started in background"}


@api.get("/returns", response_model=list[OrderRead])
def list_returns(limit: int = 50, db=Depends(get_db)):
    return list_returned_orders_in_db(db, limit=limit)


@api.post("/scans", response_model=ScanRead, status_code=status.HTTP_201_CREATED)
def create_scan(payload: ScanCreate, db=Depends(get_db)):
    try:
        return create_scan_in_db(db, payload)
    except ApiError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


@api.post("/scans/undo", response_model=ScanRead)
def undo_scan(payload: ScanUndo, db=Depends(get_db)):
    try:
        return undo_scan_in_db(db, payload)
    except ApiError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


@api.post("/orders/{order_id}/complete", response_model=OrderRead)
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


@api.post("/returns/{order_id}", response_model=OrderRead)
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


@api.post("/imports", response_model=ImportResult, status_code=status.HTTP_201_CREATED)
def create_import(payload: ImportCreate, db=Depends(get_db)):
    return create_import_in_db(db, payload)


@api.get("/imports", response_model=list[ImportRead])
def list_imports(db=Depends(get_db)):
    return list_imports_in_db(db)


@api.get("/reports/day", response_model=DayReportRead)
def day_report(report_date: str | None = None, db=Depends(get_db)):
    try:
        return build_day_report(db, report_date)
    except ApiError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


@api.get("/reports/reconciliation/day")
def reconciliation_day_report(report_date: str | None = None, db=Depends(get_db)):
    try:
        return run_daily_reconciliation(db=db, report_date=report_date, alert_chat_ids=[])
    except ReconciliationError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


@api.get("/reports/kiz/source-files")
def kiz_source_files(db=Depends(get_db)) -> list[dict]:
    return list_completed_kiz_source_files(db)


@api.get("/reports/kiz/dates")
def kiz_dates(db=Depends(get_db)) -> list[dict]:
    return list_completed_kiz_dates(db)


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
def logistics_dates(db=Depends(get_db)) -> list[str]:
    return list_logistics_dates(db)


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
    content, filename = build_backend_diagnostics_log(db, limit=limit)
    return Response(
        content=content,
        media_type="text/plain; charset=utf-8",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}",
            "X-TakSklad-Filename": quote(filename),
        },
    )


app.include_router(api)
