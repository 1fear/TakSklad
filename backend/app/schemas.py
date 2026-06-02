from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator


class HealthResponse(BaseModel):
    status: str
    service: str
    version: str
    environment: str


class AuthLoginRequest(BaseModel):
    login: str = Field(min_length=1)
    password: str = Field(min_length=1)


class AuthSessionRead(BaseModel):
    authenticated: bool
    login: str = ""
    expires_at: datetime | None = None


class OrderItemRead(BaseModel):
    id: str
    product: str
    quantity_pieces: int
    quantity_blocks: int
    scanned_blocks: int
    block_price: int = 0
    line_total: int = 0
    status: str
    scan_codes: list[str] = Field(default_factory=list)


class OrderRead(BaseModel):
    id: str
    order_date: date | None = None
    payment_type: str
    client: str
    address: str
    coordinates: str = ""
    representative: str | None = None
    status: str
    skladbot_request_number: str = ""
    skladbot_request_id: str = ""
    return_status: str = ""
    returned_at: str = ""
    return_reference: str = ""
    items: list[OrderItemRead] = Field(default_factory=list)


class AdminTableTotals(BaseModel):
    orders: int
    items: int
    active_orders: int
    archived_orders: int
    returned_orders: int
    planned_blocks: int
    scanned_blocks: int
    remaining_blocks: int
    total_price: int
    pending_google_exports: int


class AdminTableRow(BaseModel):
    order_id: str
    item_id: str
    order_date: date | None = None
    payment_type: str
    client: str
    address: str
    coordinates: str = ""
    representative: str | None = None
    order_status: str
    item_status: str
    status_bucket: str
    product: str
    quantity_pieces: int
    quantity_blocks: int
    scanned_blocks: int
    remaining_blocks: int
    scan_codes_count: int
    block_price: int = 0
    line_total: int = 0
    skladbot_request_number: str = ""
    skladbot_request_id: str = ""
    skladbot_status: str = ""
    source_file: str = ""
    google_sheet_status: str = ""
    google_sheet_row_number: int | None = None
    google_sheet_synced_at: str = ""
    pending_google_exports: int = 0
    return_status: str = ""
    returned_at: str = ""
    return_reference: str = ""
    created_at: datetime | None = None
    updated_at: datetime | None = None


class AdminActivityRead(BaseModel):
    id: str
    action: str
    entity_type: str = ""
    entity_id: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime | None = None


class AdminTableRead(BaseModel):
    generated_at: datetime
    totals: AdminTableTotals
    rows: list[AdminTableRow] = Field(default_factory=list)
    recent_activity: list[AdminActivityRead] = Field(default_factory=list)


class AdminOrderActionRequest(BaseModel):
    reason: str = Field(min_length=1)
    actor: str = "web"
    idempotency_key: str = ""
    expected_updated_at: str = ""
    dry_run: bool = False


class ScanCreate(BaseModel):
    order_item_id: str
    code: str = Field(min_length=1)
    workstation_id: str | None = None
    scanned_by: str | None = None
    scanned_at: datetime | None = None
    raw_payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("code")
    @classmethod
    def normalize_code(cls, value):
        code = str(value or "").strip(" \t\r\n")
        if not code:
            raise ValueError("Code must not be empty")
        if any(char in code for char in (" ", "\t", "\r", "\n", "\v", "\f")):
            raise ValueError("Code must not contain spaces or line breaks")
        return code


class ScanRead(BaseModel):
    id: str
    order_item_id: str
    code: str
    scanned_blocks: int
    item_status: str
    scanned_at: datetime


class ImportCreate(BaseModel):
    source: str = "excel"
    filename: str | None = None
    sha256: str | None = None
    rows: list[dict[str, Any]] = Field(default_factory=list)


class ImportRead(BaseModel):
    id: str
    source: str
    status: str
    rows_total: int
    rows_imported: int
    raw_payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class ImportResult(BaseModel):
    id: str
    source: str
    status: str
    rows_total: int
    rows_imported: int
    orders_created: int
    items_created: int
    duplicate_rows: int
    invalid_rows: int
    errors: list[str] = Field(default_factory=list)
    backend_address_updates: int = 0
    google_sheets_status: str = ""
    google_sheets_imported: int = 0
    google_sheets_duplicates: int = 0
    google_sheets_updated: int = 0
    google_sheets_error: str = ""


class DayReportTotals(BaseModel):
    orders: int
    completed_orders: int
    active_orders: int
    items: int
    completed_items: int
    planned_blocks: int
    scanned_blocks: int
    scanned_today: int
    remaining_blocks: int
    scan_codes: int
    total_price: int = 0


class DayReportPaymentGroup(BaseModel):
    payment_group: str
    payment_type: str
    orders: int
    planned_blocks: int
    scanned_blocks: int
    scanned_today: int
    remaining_blocks: int
    scan_codes: int
    total_price: int = 0


class DayReportOrder(BaseModel):
    id: str
    order_date: date | None = None
    payment_type: str
    payment_group: str
    client: str
    address: str
    coordinates: str = ""
    representative: str | None = None
    status: str
    skladbot_request_number: str = ""
    items: int
    completed_items: int
    planned_blocks: int
    scanned_blocks: int
    scanned_today: int
    remaining_blocks: int
    scan_codes: int
    total_price: int = 0


class DayReportRead(BaseModel):
    report_date: date
    source: str
    generated_at: datetime
    totals: DayReportTotals
    payment_groups: list[DayReportPaymentGroup] = Field(default_factory=list)
    orders: list[DayReportOrder] = Field(default_factory=list)
