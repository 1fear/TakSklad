from urllib.parse import quote
from threading import Lock, Thread

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Response, status
from fastapi.middleware.cors import CORSMiddleware

from .db import get_db
from .diagnostics_service import build_backend_diagnostics_log
from .google_sheets_sync_worker import sync_google_sheet_to_backend
from .google_sheets_pending import process_pending_google_sheets_exports
from .imports_service import create_import as create_import_in_db
from .imports_service import list_imports as list_imports_in_db
from .kiz_reports_service import build_kiz_source_file_report_xlsx, list_completed_kiz_source_files
from .logistics_service import build_logistics_report_xlsx, list_logistics_dates
from .orders_service import ApiError, complete_order as complete_order_in_db
from .orders_service import create_scan as create_scan_in_db
from .orders_service import list_active_orders as list_active_orders_in_db
from .orders_service import list_returned_orders as list_returned_orders_in_db
from .orders_service import lookup_return_order as lookup_return_order_in_db
from .orders_service import mark_order_returned as mark_order_returned_in_db
from .reports_service import build_day_report
from .skladbot_worker import update_orders_from_skladbot
from .schemas import (
    DayReportRead,
    HealthResponse,
    ImportCreate,
    ImportRead,
    ImportResult,
    OrderRead,
    ScanCreate,
    ScanRead,
)
from .settings import APP_VERSION, load_settings


settings = load_settings()
sync_sources_lock = Lock()
skladbot_sync_lock = Lock()

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


def require_service_token(authorization: str | None = Header(default=None)):
    if not settings.api_auth_enabled:
        return
    expected = f"Bearer {settings.api_token}"
    if authorization != expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid service token",
        )


@app.get("/health", response_model=HealthResponse)
def health():
    return {
        "status": "ok",
        "service": settings.service_name,
        "version": APP_VERSION,
        "environment": settings.environment,
    }


api = APIRouter(prefix="/api/v1", dependencies=[Depends(require_service_token)])


@api.get("/orders/active")
def list_active_orders(db=Depends(get_db)) -> list[OrderRead]:
    return list_active_orders_in_db(db)


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

        try:
            google_sheets_result = sync_google_sheet_to_backend(db)
            google_sheets_result = {"status": "completed", **google_sheets_result}
        except Exception as exc:
            google_sheets_result = {"status": "error", "error": str(exc)}
            errors.append("google_sheets")

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
def mark_return(order_id: str, payload: dict | None = None, db=Depends(get_db)):
    payload = payload or {}
    try:
        return mark_order_returned_in_db(
            db,
            order_id,
            return_reference=payload.get("return_reference") or "",
            returned_by=payload.get("returned_by") or "desktop",
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


@api.get("/reports/kiz/source-files")
def kiz_source_files(db=Depends(get_db)) -> list[dict]:
    return list_completed_kiz_source_files(db)


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
