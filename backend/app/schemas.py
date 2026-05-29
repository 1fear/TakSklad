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
    status: str


class OrderRead(BaseModel):
    id: str
    order_date: date | None = None
    payment_type: str
    client: str
    address: str
    representative: str | None = None
    status: str
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


class NotImplementedResponse(BaseModel):
    detail: str
