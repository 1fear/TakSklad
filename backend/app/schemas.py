from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator


class HealthResponse(BaseModel):
    status: str
    service: str
    version: str
    environment: str


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
    items: list[OrderItemRead] = Field(default_factory=list)


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
        code = value.strip()
        if not code:
            raise ValueError("Code must not be empty")
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
