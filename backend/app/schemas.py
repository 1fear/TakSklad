from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from .input_safety import (
    MAX_IMPORT_PAYLOAD_BYTES,
    MAX_IMPORT_ROWS,
    InputSafetyError,
    json_encoded_size,
    normalize_upload_filename,
    validate_bounded_json_payload,
    validate_import_row,
)


ImportFieldName = Literal[
    "Дата заказа", "Дата отгрузки", "Дата получения заказа", "order_date", "date",
    "Тип оплаты", "payment_type", "payment", "Клиент", "client", "Адрес", "address",
    "Координаты", "coordinates", "Торговый представитель", "representative", "Товары", "product",
    "Кол-во ШТ", "quantity_pieces", "quantity", "Кол-во блок", "Кол-во блоков", "quantity_blocks", "blocks",
    "_pieces_per_block", "pieces_per_block", "Цена за блок", "block_price", "Цена из файла", "unit_price",
    "Сумма из файла", "Сумма с переоценкой", "imported_line_total", "Сумма позиции", "line_total",
    "Сумма рассчитанная", "calculated_line_total", "Статус", "status", "ID заказа", "order_id", "external_id",
    "ID импорта", "import_id", "Источник файла", "source_file", "Строка файла", "source_row",
    "Ключ исходного документа", "source_batch_key", "Номер заявки SkladBot", "skladbot_request_number",
    "ID заявки SkladBot", "skladbot_request_id", "Отсканированные коды", "Дата импорта",
    "Smartup deal_id", "Smartup product_id", "Smartup status", "Smartup delivery_date original",
    "Smartup delivery_date adjusted", "Smartup delivery_date adjustment_reason",
    "Smartup delivery_date skipped_dates",
]
ImportScalar = str | int | float | bool | date | datetime | Decimal | None
ImportRow = dict[ImportFieldName, ImportScalar]


class HealthResponse(BaseModel):
    status: str
    service: str
    version: str
    commit_sha: str = "unknown"
    image_digest: str = "unknown"
    server_release_id: str = "unknown"
    desktop_api_contract: int = 1
    environment: str


class VersionResponse(BaseModel):
    service: str
    version: str
    commit_sha: str
    image_digest: str
    server_release_id: str
    desktop_api_contract: int
    environment: str


class DailyReportReadinessResponse(BaseModel):
    status: str = "unknown"
    due_date: str = ""
    missing_count: int = 0


class ReadinessResponse(BaseModel):
    generated_at: datetime
    ready: bool
    status: str
    service: str
    version: str
    commit_sha: str = "unknown"
    image_digest: str = "unknown"
    server_release_id: str = "unknown"
    desktop_api_contract: int = 1
    environment: str
    database: dict[str, Any] = Field(default_factory=dict)
    migrations: dict[str, Any] = Field(default_factory=dict)
    queue: dict[str, Any] = Field(default_factory=dict)
    imports: dict[str, Any] = Field(default_factory=dict)
    workers: dict[str, Any] = Field(default_factory=dict)
    daily_report: DailyReportReadinessResponse = Field(default_factory=DailyReportReadinessResponse)
    policy: dict[str, Any] = Field(default_factory=dict)


class AuthLoginRequest(BaseModel):
    login: str = Field(min_length=1)
    password: str = Field(min_length=1)


class AuthSessionRead(BaseModel):
    authenticated: bool
    login: str = ""
    role: str = ""
    permissions: list[str] = Field(default_factory=list)
    expires_at: datetime | None = None
    csrf_token: str = ""


class ScanEntryRead(BaseModel):
    code: str
    scan_type: str = "unit"
    block_quantity: int = 1
    scanned_at: datetime | None = None


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
    scan_entries: list[ScanEntryRead] = Field(default_factory=list)


class OrderRead(BaseModel):
    id: str
    order_date: date | None = None
    payment_type: str
    client: str
    address: str
    coordinates: str = ""
    representative: str | None = None
    status: str
    smartup_id: str = ""
    skladbot_request_number: str = ""
    skladbot_request_id: str = ""
    skladbot_return_request_number: str = ""
    skladbot_return_request_id: str = ""
    skladbot_return_status: str = ""
    return_status: str = ""
    returned_at: str = ""
    return_reference: str = ""
    items: list[OrderItemRead] = Field(default_factory=list)


class ReturnConfirmedItem(BaseModel):
    item_id: str = ""
    product: str = ""
    sku: str = ""
    quantity_blocks: int
    quantity_pieces: int = 0


class ReturnMarkRequest(BaseModel):
    return_reference: str = ""
    returned_by: str = "desktop"
    confirmed_items: list[ReturnConfirmedItem] = Field(default_factory=list)


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
    smartup_id: str = ""
    skladbot_request_number: str = ""
    skladbot_request_id: str = ""
    skladbot_status: str = ""
    skladbot_return_request_number: str = ""
    skladbot_return_request_id: str = ""
    skladbot_return_status: str = ""
    source_file: str = ""
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
    actor_subject: str = ""
    actor_user_id: str = ""
    actor_service_principal_id: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime | None = None


class AdminOrderCapabilityRead(BaseModel):
    order_id: str
    items_count: int = 0
    planned_blocks: int = 0
    scanned_blocks: int = 0
    scan_codes_count: int = 0
    allowed: dict[str, bool] = Field(default_factory=dict)
    disabled_reasons: dict[str, str] = Field(default_factory=dict)


class AdminTableRead(BaseModel):
    generated_at: datetime
    totals: AdminTableTotals
    rows: list[AdminTableRow] = Field(default_factory=list)
    recent_activity: list[AdminActivityRead] = Field(default_factory=list)
    limit: int = 5000
    offset: int = 0
    row_count: int = 0
    total_rows: int = 0
    has_more: bool = False
    next_cursor: str = ""
    order_capabilities: dict[str, AdminOrderCapabilityRead] = Field(default_factory=dict)


class AdminOrderActionRequest(BaseModel):
    reason: str = ""
    actor: str = "web"
    source: str = ""
    idempotency_key: str = ""
    expected_updated_at: str = ""
    dry_run: bool = False


class AdminBulkOrderActionRequest(BaseModel):
    order_ids: list[str] = Field(min_length=1, max_length=500)
    reason: str = ""
    actor: str = "web"
    source: str = ""
    idempotency_key: str = ""
    expected_updated_at_by_order: dict[str, str] = Field(default_factory=dict)
    dry_run: bool = False


class AdminBulkOrderActionError(BaseModel):
    order_id: str
    message: str


class AdminBulkOrderActionResult(BaseModel):
    requested: int
    completed: int
    failed: int
    errors: list[AdminBulkOrderActionError] = Field(default_factory=list)
    dry_run: bool = False


class ActiveOrderDeleteResult(BaseModel):
    order_id: str
    deleted: bool = False
    dry_run: bool = False
    skladbot_request_number: str = ""
    skladbot_request_id: str = ""
    message: str = ""


class EventQueueEventRead(BaseModel):
    id: str
    event_type: str
    action: str = ""
    aggregate_type: str = ""
    aggregate_id: str = ""
    status: str
    attempts: int
    last_error: str = ""
    idempotency_key: str = ""
    next_attempt_at: str = ""
    available_at: datetime | None = None
    lease_owner: str = ""
    lease_expires_at: datetime | None = None
    completed_at: datetime | None = None
    payload_status: str = ""
    retryable: bool = False
    linked_order_id: str = ""
    linked_import_id: str = ""
    linked_entity_type: str = ""
    linked_entity_id: str = ""
    raw_payload: dict[str, Any] = Field(default_factory=dict)
    age_seconds: int = 0
    created_at: datetime | None = None
    updated_at: datetime | None = None


class EventQueueActionRequest(BaseModel):
    reason: str = ""
    actor: str = "web"
    source: str = ""
    idempotency_key: str = ""


class EventQueueDiagnosticsRead(BaseModel):
    generated_at: datetime
    summary: dict[str, Any] = Field(default_factory=dict)
    stale_processing: list[EventQueueEventRead] = Field(default_factory=list)
    recent_events: list[EventQueueEventRead] = Field(default_factory=list)
    limit: int = 50
    row_count: int = 0
    has_more: bool = False
    next_cursor: str = ""


class OperationsAttentionItemRead(BaseModel):
    category: str
    impact: str
    severity: str
    title: str
    count: int = 0
    oldest_age_seconds: int = 0
    next_action: str = ""
    details: list[str] = Field(default_factory=list)


class OperationsAttentionRead(BaseModel):
    generated_at: datetime
    status: str
    summary: dict[str, Any] = Field(default_factory=dict)
    items: list[OperationsAttentionItemRead] = Field(default_factory=list)
    readiness_status: str = ""
    shadow_diagnostics: dict[str, Any] = Field(default_factory=dict)
    telegram_summary: str = ""


class SmartupAutoImportRunRead(BaseModel):
    id: str
    status: str
    export_date: str = ""
    slot: str = ""
    part: int | None = None
    filename: str = ""
    export_path: str = ""
    audit_path: str = ""
    selected_orders: int = 0
    rows: int = 0
    delivery_dates: list[str] = Field(default_factory=list)
    imports_count: int = 0
    orders_created: int = 0
    items_created: int = 0
    duplicate_rows: int = 0
    status_change_submitted: int = 0
    skladbot_status: str = ""
    logistics_reports: list[dict[str, Any]] = Field(default_factory=list)
    error: str = ""
    created_at: datetime | None = None
    updated_at: datetime | None = None
    completed_at: str = ""
    failed_at: str = ""


class SmartupAutoImportHistoryRead(BaseModel):
    generated_at: datetime
    summary: dict[str, Any] = Field(default_factory=dict)
    runs: list[SmartupAutoImportRunRead] = Field(default_factory=list)
    events: list[EventQueueEventRead] = Field(default_factory=list)
    audit: list[AdminActivityRead] = Field(default_factory=list)
    limit: int = 50
    row_count: int = 0
    has_more: bool = False
    next_cursor: str = ""


class IncidentCreate(BaseModel):
    source: str = Field(min_length=1, max_length=128)
    severity: str = Field(default="warning", max_length=32)
    status: str = Field(default="open", max_length=32)
    title: str = Field(min_length=1, max_length=512)
    message: str = Field(default="", max_length=16_384)
    entity_type: str = Field(default="", max_length=128)
    entity_id: str = Field(default="", max_length=256)
    pending_event_id: str = Field(default="", max_length=256)
    order_id: str = Field(default="", max_length=256)
    order_item_id: str = Field(default="", max_length=256)
    import_id: str = Field(default="", max_length=256)
    scan_code_id: str = Field(default="", max_length=256)
    external_ref: str = Field(default="", max_length=512)
    raw_payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("raw_payload", mode="before")
    @classmethod
    def bound_raw_payload(cls, value):
        return validate_bounded_json_payload(value)


class IncidentStatusUpdate(BaseModel):
    status: str = Field(min_length=1)
    actor: str = "web"
    source: str = ""
    reason: str = Field(min_length=1)


class IncidentRead(BaseModel):
    id: str
    source: str
    severity: str
    status: str
    title: str
    message: str = ""
    entity_type: str = ""
    entity_id: str = ""
    pending_event_id: str = ""
    order_id: str = ""
    order_item_id: str = ""
    import_id: str = ""
    scan_code_id: str = ""
    external_ref: str = ""
    raw_payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime | None = None
    updated_at: datetime | None = None
    resolved_at: datetime | None = None


class IncidentListRead(BaseModel):
    items: list[IncidentRead] = Field(default_factory=list)
    summary: dict[str, Any] = Field(default_factory=dict)
    limit: int = 50
    row_count: int = 0
    has_more: bool = False
    next_cursor: str = ""


class ClientPointOrderProductRead(BaseModel):
    product: str
    positions_count: int = 0
    quantity_blocks: int = 0
    quantity_pieces: int = 0


class ClientPointOrderReferenceRead(BaseModel):
    order_id: str
    smartup_id: str = ""
    skladbot_request_number: str = ""
    skladbot_request_id: str = ""
    skladbot_return_request_number: str = ""
    skladbot_return_request_id: str = ""
    is_returned: bool = False


class ClientPointOrderDateRead(BaseModel):
    shipment_date: date | None = None
    payment_type: str = ""
    orders_count: int = 0
    returned_orders_count: int = 0
    positions_count: int = 0
    quantity_blocks: int = 0
    quantity_pieces: int = 0
    order_references: list[ClientPointOrderReferenceRead] = Field(default_factory=list)
    products: list[ClientPointOrderProductRead] = Field(default_factory=list)


class ClientPointOrderSummaryTotalsRead(BaseModel):
    orders_count: int = 0
    returned_orders_count: int = 0
    positions_count: int = 0
    quantity_blocks: int = 0
    quantity_pieces: int = 0


class ClientPointOrderSummaryRead(BaseModel):
    client_name: str
    normalized_client: str = ""
    totals: ClientPointOrderSummaryTotalsRead
    dates: list[ClientPointOrderDateRead] = Field(default_factory=list)


class ClientPointRead(BaseModel):
    id: str
    client_name: str
    point_name: str = ""
    address: str
    coordinates: str = ""
    representative: str = ""
    delivery_from: str = "10:00"
    delivery_to: str = "18:00"
    is_active: bool = True
    is_saved: bool = False
    source: str = ""
    has_custom_timeslot: bool = False
    orders_count: int = 0
    returned_orders_count: int = 0
    last_order_date: date | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class ClientPointTimeslotUpdate(BaseModel):
    client_name: str = Field(min_length=1)
    address: str = Field(min_length=1)
    point_name: str = ""
    coordinates: str = ""
    representative: str = ""
    delivery_from: str = "10:00"
    delivery_to: str = "18:00"
    is_active: bool = True
    actor: str = "web"
    reason: str = ""


class ScanCreate(BaseModel):
    order_item_id: str = Field(min_length=1, max_length=256)
    code: str = Field(min_length=1, max_length=512)
    workstation_id: str | None = Field(default=None, max_length=256)
    scanned_by: str | None = Field(default=None, max_length=256)
    scanned_at: datetime | None = None
    raw_payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("raw_payload", mode="before")
    @classmethod
    def bound_raw_payload(cls, value):
        return validate_bounded_json_payload(value)

    @field_validator("code")
    @classmethod
    def normalize_code(cls, value):
        code = str(value or "").strip(" \t\r\n")
        if not code:
            raise ValueError("Code must not be empty")
        if any(char in code for char in (" ", "\t", "\r", "\n", "\v", "\f")):
            raise ValueError("Code must not contain spaces or line breaks")
        return code


class ScanUndo(BaseModel):
    order_item_id: str
    code: str = Field(min_length=1)
    workstation_id: str | None = None
    actor: str = "desktop"

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
    scan_type: str = "unit"
    block_quantity: int = 1


class KizAvailabilityRead(BaseModel):
    code: str
    available: bool
    reason: str = ""
    latest_movement_type: str = ""
    latest_order_item_id: str = ""
    existing_order_item_id: str = ""


class ImportCreate(BaseModel):
    source: str = Field(default="excel", min_length=1, max_length=128)
    filename: str | None = Field(default=None, max_length=128)
    sha256: str | None = Field(default=None, max_length=64)
    telegram_chat_id: str | None = Field(default=None, max_length=128)
    telegram_event_id: str | None = Field(default=None, max_length=256)
    source_rows_count: int = Field(default=0, ge=0)
    skipped_rows_count: int = Field(default=0, ge=0)
    payment_groups: dict[str, int] = Field(default_factory=dict)
    rows: list[ImportRow] = Field(default_factory=list, max_length=MAX_IMPORT_ROWS)

    @field_validator("filename")
    @classmethod
    def safe_filename(cls, value):
        if value is None:
            return None
        return normalize_upload_filename(value)

    @field_validator("rows", mode="before")
    @classmethod
    def bound_rows(cls, value):
        if not isinstance(value, list):
            raise InputSafetyError("import_rows_type")
        if len(value) > MAX_IMPORT_ROWS:
            raise InputSafetyError("import_rows_exceeded")
        validated = [validate_import_row(row) for row in value]
        if json_encoded_size(validated) > MAX_IMPORT_PAYLOAD_BYTES:
            raise InputSafetyError("import_payload_bytes_exceeded")
        return validated


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
    resolved_order_ids: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    backend_address_updates: int = 0
    skladbot_dry_run_status: str = ""
    skladbot_dry_run_ready: int = 0
    skladbot_dry_run_blocked: int = 0
    skladbot_dry_run_already_linked: int = 0
    skladbot_dry_run_linked_mismatch: int = 0
    skladbot_dry_run_event_id: str = ""


class ImportPreviewResult(BaseModel):
    source: str
    status: str
    rows_total: int
    rows_importable: int
    orders_new: int
    items_new: int
    duplicate_rows: int
    invalid_rows: int
    duplicate_row_numbers: list[int] = Field(default_factory=list)
    invalid_row_numbers: list[int] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    backend_address_updates: int = 0


class ExcelImportPreviewResponse(BaseModel):
    preview: ImportPreviewResult
    filename: str
    sha256: str
    meta: dict[str, Any] = Field(default_factory=dict)


class ExcelImportCommitResponse(BaseModel):
    result: ImportResult
    filename: str
    sha256: str
    meta: dict[str, Any] = Field(default_factory=dict)


class SkladBotDryRunProductRead(BaseModel):
    product: str
    quantity_blocks: int
    product_data_id: int | None = None
    barcode: str = ""
    is_main_barcode: bool = False
    status: str
    error: str = ""


class SkladBotDryRunRead(BaseModel):
    id: str
    event_id: str
    import_id: str
    order_id: str
    client: str
    order_date: date | None = None
    payment_type: str
    address: str
    blocks: int
    status: str
    error: str = ""
    smartup_id: str = ""
    skladbot_request_number: str = ""
    skladbot_request_id: str = ""
    skladbot_return_request_number: str = ""
    skladbot_return_request_id: str = ""
    products: list[SkladBotDryRunProductRead] = Field(default_factory=list)
    payload: dict[str, Any] = Field(default_factory=dict)
    generated_at: datetime | None = None


class DayReportTotals(BaseModel):
    orders: int
    completed_orders: int
    active_orders: int
    returned_orders: int = 0
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


class DashboardDaySummaryRead(BaseModel):
    report_date: date
    source: str
    generated_at: datetime
    totals: DayReportTotals


class LogisticsCalendarDayRead(BaseModel):
    date: date
    weekday: int
    is_weekend: bool = False
    is_non_working: bool = False
    is_manual: bool = False
    reason: str = ""
    source: str = ""
    orders_count: int = 0
    active_orders: int = 0
    completed_orders: int = 0
    returned_orders: int = 0
    planned_blocks: int = 0
    clients: list[str] = Field(default_factory=list)


class LogisticsCalendarRead(BaseModel):
    generated_at: datetime
    month: str
    default_non_working_weekdays: list[int] = Field(default_factory=list)
    days: list[LogisticsCalendarDayRead] = Field(default_factory=list)


class LogisticsCalendarDayUpdate(BaseModel):
    service_date: date
    is_non_working: bool = True
    reason: str = ""
    actor: str = "web"
    source: str = "web"
