from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, status

from .db import get_db
from .orders_service import ApiError, complete_order as complete_order_in_db
from .orders_service import create_scan as create_scan_in_db
from .orders_service import list_active_orders as list_active_orders_in_db
from .schemas import HealthResponse, ImportCreate, OrderRead, ScanCreate, ScanRead
from .settings import APP_VERSION, load_settings


settings = load_settings()

app = FastAPI(
    title="TakSklad Backend API",
    version=APP_VERSION,
    docs_url="/docs",
    redoc_url="/redoc",
)


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


def not_implemented(feature):
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail=f"{feature} is defined in the MVP contract but not implemented yet.",
    )


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


@api.post("/imports")
def create_import(payload: ImportCreate):
    not_implemented("Excel import")


@api.get("/imports")
def list_imports():
    not_implemented("Import history")


@api.get("/reports/day")
def day_report(report_date: str | None = None):
    not_implemented("Day report")


app.include_router(api)
