from urllib.parse import quote

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Response, status
from fastapi.middleware.cors import CORSMiddleware

from .db import get_db
from .imports_service import create_import as create_import_in_db
from .imports_service import list_imports as list_imports_in_db
from .kiz_reports_service import build_kiz_source_file_report_xlsx, list_completed_kiz_source_files
from .logistics_service import build_logistics_report_xlsx, list_logistics_dates
from .orders_service import ApiError, complete_order as complete_order_in_db
from .orders_service import create_scan as create_scan_in_db
from .orders_service import list_active_orders as list_active_orders_in_db
from .reports_service import build_day_report
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
def kiz_source_file_report(source_file: str, db=Depends(get_db)):
    try:
        content, filename = build_kiz_source_file_report_xlsx(db, source_file)
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


app.include_router(api)
