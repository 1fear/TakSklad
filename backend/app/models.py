import uuid

from sqlalchemy import JSON, Boolean, CheckConstraint, Date, DateTime, ForeignKey, Index, Integer, String, Text, Uuid, UniqueConstraint, event, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


JSON_TYPE = JSON().with_variant(JSONB, "postgresql")
UUID_TYPE = Uuid(as_uuid=True)


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (
        Index("idx_orders_import_order_key_status", "import_order_key", "status"),
        Index("idx_orders_import_source_order_key_status", "import_source_order_key", "status"),
        Index(
            "idx_orders_active_page",
            "order_date",
            "created_at",
            "id",
            postgresql_where=text(
                "status NOT IN ('completed','done','closed','returned','archived_no_kiz','cancelled')"
            ),
        ),
        CheckConstraint(
            "status IN ('not_completed','completed','done','closed','returned','archived_no_kiz','cancelled')",
            name="ck_orders_supported_status",
        ),
        CheckConstraint(
            "(import_order_key IS NULL OR trim(import_order_key) <> '') AND "
            "(import_source_order_key IS NULL OR trim(import_source_order_key) <> '')",
            name="ck_orders_import_keys_nonblank",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID_TYPE, primary_key=True, default=uuid.uuid4)
    source: Mapped[str] = mapped_column(String(40), nullable=False, default="google_sheets")
    external_id: Mapped[str | None] = mapped_column(String(120))
    import_order_key: Mapped[str | None] = mapped_column(String(120))
    import_source_order_key: Mapped[str | None] = mapped_column(String(120))
    order_date: Mapped[object | None] = mapped_column(Date)
    payment_type: Mapped[str] = mapped_column(String(120), nullable=False)
    client: Mapped[str] = mapped_column(String(255), nullable=False)
    address: Mapped[str] = mapped_column(Text, nullable=False)
    representative: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="not_completed")
    raw_payload: Mapped[dict] = mapped_column(JSON_TYPE, nullable=False, default=dict)
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[object] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    items: Mapped[list["OrderItem"]] = relationship(back_populates="order", cascade="all, delete-orphan")
    fulfillment_links: Mapped[list["SmartupFulfillmentOrder"]] = relationship(back_populates="order")


class OrderItem(Base):
    __tablename__ = "order_items"
    __table_args__ = (
        Index("idx_order_items_import_item_key", "import_item_key"),
        Index("idx_order_items_source_import_key", "source_import_key"),
        CheckConstraint(
            "quantity_pieces >= 0 AND quantity_blocks >= 0 AND scanned_blocks >= 0",
            name="ck_order_items_quantities_nonnegative",
        ),
        CheckConstraint(
            "pieces_per_block IS NULL OR pieces_per_block > 0",
            name="ck_order_items_pieces_per_block_positive",
        ),
        CheckConstraint("scanned_blocks <= quantity_blocks", name="ck_order_items_scanned_within_plan"),
        CheckConstraint(
            "status IN ('not_completed','completed','done','closed','returned','removed_from_google_sheet',"
            "'archived_no_kiz','cancelled')",
            name="ck_order_items_supported_status",
        ),
        CheckConstraint(
            "(source_import_id IS NULL AND source_import_key IS NULL) OR "
            "(source_import_id IS NOT NULL AND source_import_key IS NOT NULL)",
            name="ck_order_items_source_identity_pair",
        ),
        CheckConstraint(
            "(import_item_key IS NULL OR trim(import_item_key) <> '') AND "
            "(source_import_key IS NULL OR trim(source_import_key) <> '')",
            name="ck_order_items_import_keys_nonblank",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID_TYPE, primary_key=True, default=uuid.uuid4)
    order_id: Mapped[uuid.UUID] = mapped_column(UUID_TYPE, ForeignKey("orders.id", ondelete="CASCADE"), nullable=False)
    product: Mapped[str] = mapped_column(String(255), nullable=False)
    import_item_key: Mapped[str | None] = mapped_column(String(64))
    source_import_key: Mapped[str | None] = mapped_column(String(64))
    source_import_id: Mapped[str | None] = mapped_column(Text)
    source_batch_key: Mapped[str | None] = mapped_column(Text)
    quantity_pieces: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    quantity_blocks: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    pieces_per_block: Mapped[int | None] = mapped_column(Integer)
    scanned_blocks: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    requires_kiz: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="not_completed")
    raw_payload: Mapped[dict] = mapped_column(JSON_TYPE, nullable=False, default=dict)
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[object] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    order: Mapped[Order] = relationship(back_populates="items")
    scan_codes: Mapped[list["ScanCode"]] = relationship(back_populates="order_item", cascade="all, delete-orphan")


class ScanCode(Base):
    __tablename__ = "scan_codes"
    __table_args__ = (
        Index("idx_scan_codes_code", "code"),
        Index("idx_scan_codes_code_order_item_id", "code", "order_item_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID_TYPE, primary_key=True, default=uuid.uuid4)
    order_item_id: Mapped[uuid.UUID] = mapped_column(UUID_TYPE, ForeignKey("order_items.id", ondelete="CASCADE"), nullable=False)
    code: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(String(40), nullable=False, default="desktop")
    workstation_id: Mapped[str | None] = mapped_column(String(120))
    scanned_by: Mapped[str | None] = mapped_column(String(120))
    scanned_at: Mapped[object] = mapped_column(DateTime(timezone=True), server_default=func.now())
    raw_payload: Mapped[dict] = mapped_column(JSON_TYPE, nullable=False, default=dict)

    order_item: Mapped[OrderItem] = relationship(back_populates="scan_codes")


class KizCode(Base):
    __tablename__ = "kiz_codes"
    __table_args__ = (UniqueConstraint("code", name="uq_kiz_codes_code"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID_TYPE, primary_key=True, default=uuid.uuid4)
    code: Mapped[str] = mapped_column(Text, nullable=False)
    first_seen_at: Mapped[object] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[object] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    movements: Mapped[list["KizMovement"]] = relationship(back_populates="kiz_code")


class KizMovement(Base):
    __tablename__ = "kiz_movements"
    __table_args__ = (
        Index("idx_kiz_movements_kiz_id_occurred_at", "kiz_id", "occurred_at"),
        Index("idx_kiz_movements_order_id", "order_id"),
        Index("idx_kiz_movements_order_item_id", "order_item_id"),
        Index("idx_kiz_movements_scan_code_id", "scan_code_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID_TYPE, primary_key=True, default=uuid.uuid4)
    kiz_id: Mapped[uuid.UUID] = mapped_column(UUID_TYPE, ForeignKey("kiz_codes.id", ondelete="CASCADE"), nullable=False)
    movement_type: Mapped[str] = mapped_column(String(40), nullable=False)
    order_id: Mapped[uuid.UUID | None] = mapped_column(UUID_TYPE, ForeignKey("orders.id", ondelete="SET NULL"))
    order_item_id: Mapped[uuid.UUID | None] = mapped_column(UUID_TYPE, ForeignKey("order_items.id", ondelete="SET NULL"))
    scan_code_id: Mapped[uuid.UUID | None] = mapped_column(UUID_TYPE, ForeignKey("scan_codes.id", ondelete="SET NULL"))
    return_reference: Mapped[str | None] = mapped_column(String(120))
    source: Mapped[str] = mapped_column(String(40), nullable=False, default="backend")
    actor: Mapped[str | None] = mapped_column(String(120))
    workstation_id: Mapped[str | None] = mapped_column(String(120))
    occurred_at: Mapped[object] = mapped_column(DateTime(timezone=True), server_default=func.now())
    raw_payload: Mapped[dict] = mapped_column(JSON_TYPE, nullable=False, default=dict)

    kiz_code: Mapped[KizCode] = relationship(back_populates="movements")


class ImportJob(Base):
    __tablename__ = "imports"
    __table_args__ = (
        CheckConstraint(
            "status IN ('created','completed','completed_with_errors','failed')",
            name="ck_imports_supported_status",
        ),
        CheckConstraint(
            "rows_total >= 0 AND rows_imported >= 0 AND rows_imported <= rows_total",
            name="ck_imports_row_counts",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID_TYPE, primary_key=True, default=uuid.uuid4)
    source: Mapped[str] = mapped_column(String(40), nullable=False, default="excel")
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="created")
    rows_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rows_imported: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    raw_payload: Mapped[dict] = mapped_column(JSON_TYPE, nullable=False, default=dict)
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ImportFile(Base):
    __tablename__ = "import_files"
    __table_args__ = (UniqueConstraint("sha256", name="uq_import_files_sha256"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID_TYPE, primary_key=True, default=uuid.uuid4)
    import_id: Mapped[uuid.UUID | None] = mapped_column(UUID_TYPE, ForeignKey("imports.id", ondelete="SET NULL"))
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PendingEvent(Base):
    __tablename__ = "pending_events"
    __table_args__ = (
        Index("idx_pending_events_status_created_at", "status", "created_at", "id"),
        Index("idx_pending_events_status_updated_at", "status", "updated_at", "id"),
        Index("idx_pending_events_type_status_created_at", "event_type", "status", "created_at", "id"),
        Index("idx_pending_events_type_status_updated_at", "event_type", "status", "updated_at", "id"),
        Index("idx_pending_events_updated_created_at", "updated_at", "created_at", "id"),
        Index(
            "idx_pending_events_claim_ordered",
            "event_type",
            "available_at",
            "created_at",
            "id",
        ),
        Index("idx_pending_events_lease_expiry", "status", "lease_expires_at", "id"),
        Index(
            "idx_pending_events_action_aggregate_status",
            "action", "aggregate_type", "aggregate_id", "status", "created_at", "id",
        ),
        Index("uq_pending_events_idempotency_key", "idempotency_key", unique=True),
        CheckConstraint(
            "status IN ('pending','failed','error','processing','completed','blocked','dead','cancelled',"
            "'active','waiting_shipment_date','waiting_date_choice')",
            name="ck_pending_events_supported_status",
        ),
        CheckConstraint("attempts >= 0", name="ck_pending_events_attempts_nonnegative"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID_TYPE, primary_key=True, default=uuid.uuid4)
    event_type: Mapped[str] = mapped_column(String(80), nullable=False)
    action: Mapped[str | None] = mapped_column(String(80))
    aggregate_type: Mapped[str | None] = mapped_column(String(80))
    aggregate_id: Mapped[str | None] = mapped_column(String(180))
    idempotency_key: Mapped[str | None] = mapped_column(String(180))
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    payload: Mapped[dict] = mapped_column(JSON_TYPE, nullable=False, default=dict)
    last_error: Mapped[str | None] = mapped_column(Text)
    available_at: Mapped[object] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    lease_owner: Mapped[str | None] = mapped_column(String(160))
    lease_expires_at: Mapped[object | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[object | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[object] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


@event.listens_for(PendingEvent, "before_insert")
@event.listens_for(PendingEvent, "before_update")
def _persist_pending_event_correlation(_mapper, _connection, target: PendingEvent) -> None:
    """Cover direct/legacy producers at the ORM persistence boundary."""
    from .observability_context import pending_event_correlation_id

    pending_event_correlation_id(target)


class SmartupFulfillment(Base):
    __tablename__ = "smartup_fulfillments"
    __table_args__ = (
        UniqueConstraint(
            "source_scope",
            "deal_id",
            "request_type",
            "revision",
            name="uq_smartup_fulfillments_business_identity",
        ),
        UniqueConstraint("workflow_key", name="uq_smartup_fulfillments_workflow_key"),
        UniqueConstraint("legacy_saga_event_id", name="uq_smartup_fulfillments_legacy_saga_event"),
        Index("idx_smartup_fulfillments_state_available", "state", "available_at", "id"),
        Index("idx_smartup_fulfillments_deal", "source_scope", "deal_id", "revision"),
        CheckConstraint(
            "state IN ("
            "'local_ready','smartup_write_started','smartup_confirmed','skladbot_create_queued',"
            "'skladbot_post_started','skladbot_created','smartup_ambiguous','skladbot_ambiguous',"
            "'blocked_validation','blocked_stock','payload_mismatch','manual_review','cancelled'"
            ")",
            name="ck_smartup_fulfillments_supported_state",
        ),
        CheckConstraint("revision > 0", name="ck_smartup_fulfillments_revision_positive"),
        CheckConstraint(
            "retry_attempts >= 0 AND reconciliation_attempts >= 0",
            name="ck_smartup_fulfillments_attempts_nonnegative",
        ),
        CheckConstraint("length(payload_hash) = 64", name="ck_smartup_fulfillments_payload_hash_length"),
        CheckConstraint(
            "trim(workflow_key) <> '' AND trim(source_scope) <> '' AND trim(deal_id) <> '' "
            "AND trim(request_type) <> '' AND trim(target_status) <> ''",
            name="ck_smartup_fulfillments_identity_nonblank",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID_TYPE, primary_key=True, default=uuid.uuid4)
    workflow_key: Mapped[str] = mapped_column(String(180), nullable=False)
    source_scope: Mapped[str] = mapped_column(String(160), nullable=False)
    deal_id: Mapped[str] = mapped_column(String(180), nullable=False)
    request_type: Mapped[str] = mapped_column(String(60), nullable=False, default="shipment")
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    target_status: Mapped[str] = mapped_column(String(80), nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(40), nullable=False, default="local_ready")
    retry_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reconciliation_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    available_at: Mapped[object] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    state_changed_at: Mapped[object] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    canonical_import_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID_TYPE,
        ForeignKey("imports.id", ondelete="SET NULL"),
    )
    legacy_saga_event_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID_TYPE,
        ForeignKey("pending_events.id", ondelete="SET NULL"),
    )
    last_error: Mapped[str | None] = mapped_column(Text)
    raw_payload: Mapped[dict] = mapped_column(JSON_TYPE, nullable=False, default=dict)
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[object] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    order_links: Mapped[list["SmartupFulfillmentOrder"]] = relationship(
        back_populates="fulfillment",
        cascade="all, delete-orphan",
    )


class SmartupFulfillmentOrder(Base):
    __tablename__ = "smartup_fulfillment_orders"
    __table_args__ = (
        UniqueConstraint("fulfillment_id", "order_id", name="uq_smartup_fulfillment_orders_mapping"),
        UniqueConstraint("skladbot_event_id", name="uq_smartup_fulfillment_orders_skladbot_event"),
        UniqueConstraint("remote_request_id", name="uq_smartup_fulfillment_orders_remote_request"),
        Index("idx_smartup_fulfillment_orders_order", "order_id", "fulfillment_id"),
        CheckConstraint(
            "(remote_request_id IS NULL OR trim(remote_request_id) <> '')",
            name="ck_smartup_fulfillment_orders_remote_request_nonblank",
        ),
        CheckConstraint(
            "state IN ('pending','create_queued','post_started','created','ambiguous','blocked_stock','manual_review')",
            name="ck_smartup_fulfillment_orders_supported_state",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID_TYPE, primary_key=True, default=uuid.uuid4)
    fulfillment_id: Mapped[uuid.UUID] = mapped_column(
        UUID_TYPE,
        ForeignKey("smartup_fulfillments.id", ondelete="CASCADE"),
        nullable=False,
    )
    order_id: Mapped[uuid.UUID] = mapped_column(
        UUID_TYPE,
        ForeignKey("orders.id", ondelete="RESTRICT"),
        nullable=False,
    )
    skladbot_event_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID_TYPE,
        ForeignKey("pending_events.id", ondelete="SET NULL"),
    )
    remote_request_id: Mapped[str | None] = mapped_column(String(180))
    state: Mapped[str] = mapped_column(String(40), nullable=False, default="pending")
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[object] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    fulfillment: Mapped[SmartupFulfillment] = relationship(back_populates="order_links")
    order: Mapped[Order] = relationship(back_populates="fulfillment_links")


class WorkerHeartbeat(Base):
    __tablename__ = "worker_heartbeats"
    __table_args__ = (
        CheckConstraint("interval_seconds > 0", name="ck_worker_heartbeats_interval_positive"),
        CheckConstraint("grace_seconds >= 0", name="ck_worker_heartbeats_grace_nonnegative"),
        CheckConstraint(
            "status IN ('running','success','failed')",
            name="ck_worker_heartbeats_supported_status",
        ),
    )

    worker_name: Mapped[str] = mapped_column(String(80), primary_key=True)
    interval_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    grace_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=15, server_default="15")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="running")
    correlation_id: Mapped[str] = mapped_column(String(36), nullable=False)
    last_cycle_started_at: Mapped[object] = mapped_column(DateTime(timezone=True), nullable=False)
    last_success_at: Mapped[object | None] = mapped_column(DateTime(timezone=True))
    last_failure_at: Mapped[object | None] = mapped_column(DateTime(timezone=True))
    last_error_class: Mapped[str | None] = mapped_column(String(80))
    updated_at: Mapped[object] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class Incident(Base):
    __tablename__ = "incidents"
    __table_args__ = (
        Index("idx_incidents_status_severity", "status", "severity"),
        Index("idx_incidents_source", "source"),
        Index("idx_incidents_entity_type", "entity_type"),
        Index("idx_incidents_created_at", "created_at"),
        Index("idx_incidents_pending_event_id", "pending_event_id"),
        Index("idx_incidents_order_id", "order_id"),
        Index("idx_incidents_order_item_id", "order_item_id"),
        Index("idx_incidents_import_id", "import_id"),
        Index("idx_incidents_scan_code_id", "scan_code_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID_TYPE, primary_key=True, default=uuid.uuid4)
    source: Mapped[str] = mapped_column(String(80), nullable=False)
    severity: Mapped[str] = mapped_column(String(40), nullable=False, default="warning")
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="open")
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    message: Mapped[str | None] = mapped_column(Text)
    entity_type: Mapped[str | None] = mapped_column(String(80))
    entity_id: Mapped[str | None] = mapped_column(String(120))
    pending_event_id: Mapped[uuid.UUID | None] = mapped_column(UUID_TYPE, ForeignKey("pending_events.id", ondelete="SET NULL"))
    order_id: Mapped[uuid.UUID | None] = mapped_column(UUID_TYPE, ForeignKey("orders.id", ondelete="SET NULL"))
    order_item_id: Mapped[uuid.UUID | None] = mapped_column(UUID_TYPE, ForeignKey("order_items.id", ondelete="SET NULL"))
    import_id: Mapped[uuid.UUID | None] = mapped_column(UUID_TYPE, ForeignKey("imports.id", ondelete="SET NULL"))
    scan_code_id: Mapped[uuid.UUID | None] = mapped_column(UUID_TYPE, ForeignKey("scan_codes.id", ondelete="SET NULL"))
    external_ref: Mapped[str | None] = mapped_column(String(180))
    raw_payload: Mapped[dict] = mapped_column(JSON_TYPE, nullable=False, default=dict)
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[object] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    resolved_at: Mapped[object | None] = mapped_column(DateTime(timezone=True))


class ClientPoint(Base):
    __tablename__ = "client_points"
    __table_args__ = (
        UniqueConstraint("normalized_client", "normalized_address", name="uq_client_points_normalized"),
        Index("idx_client_points_normalized", "normalized_client", "normalized_address"),
        Index("idx_client_points_timeslot", "delivery_from", "delivery_to"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID_TYPE, primary_key=True, default=uuid.uuid4)
    client_name: Mapped[str] = mapped_column(String(255), nullable=False)
    point_name: Mapped[str | None] = mapped_column(String(255))
    address: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_client: Mapped[str] = mapped_column(String(255), nullable=False)
    normalized_address: Mapped[str] = mapped_column(Text, nullable=False)
    coordinates: Mapped[str | None] = mapped_column(Text)
    representative: Mapped[str | None] = mapped_column(String(255))
    delivery_from: Mapped[str] = mapped_column(String(5), nullable=False, default="10:00")
    delivery_to: Mapped[str] = mapped_column(String(5), nullable=False, default="18:00")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    raw_payload: Mapped[dict] = mapped_column(JSON_TYPE, nullable=False, default=dict)
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[object] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class LogisticsCalendarDay(Base):
    __tablename__ = "logistics_calendar_days"
    __table_args__ = (
        UniqueConstraint("service_date", name="uq_logistics_calendar_days_service_date"),
        Index("idx_logistics_calendar_days_service_date", "service_date"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID_TYPE, primary_key=True, default=uuid.uuid4)
    service_date: Mapped[object] = mapped_column(Date, nullable=False)
    is_non_working: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    reason: Mapped[str | None] = mapped_column(String(255))
    source: Mapped[str] = mapped_column(String(40), nullable=False, default="manual")
    actor: Mapped[str | None] = mapped_column(String(120))
    raw_payload: Mapped[dict] = mapped_column(JSON_TYPE, nullable=False, default=dict)
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[object] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class RepresentativeContact(Base):
    __tablename__ = "representative_contacts"
    __table_args__ = (
        UniqueConstraint("normalized_name", name="uq_representative_contacts_normalized_name"),
        Index("idx_representative_contacts_normalized_name", "normalized_name"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID_TYPE, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    normalized_name: Mapped[str] = mapped_column(String(255), nullable=False)
    work_phone: Mapped[str | None] = mapped_column(String(80))
    personal_phone: Mapped[str | None] = mapped_column(String(80))
    work_zone: Mapped[str | None] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    raw_payload: Mapped[dict] = mapped_column(JSON_TYPE, nullable=False, default=dict)
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[object] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("auth_version > 0", name="ck_users_auth_version_positive"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID_TYPE, primary_key=True, default=uuid.uuid4)
    username: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    password_hash: Mapped[str | None] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(40), nullable=False, default="operator")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    auth_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[object] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class AuthSession(Base):
    __tablename__ = "auth_sessions"
    __table_args__ = (
        UniqueConstraint("session_digest", name="uq_auth_sessions_session_digest"),
        Index("idx_auth_sessions_user_active", "user_id", "revoked_at", "expires_at"),
        Index("idx_auth_sessions_expires_at", "expires_at"),
        CheckConstraint("auth_version > 0", name="ck_auth_sessions_auth_version_positive"),
        CheckConstraint("trim(subject) <> ''", name="ck_auth_sessions_subject_nonblank"),
        CheckConstraint("trim(role) <> ''", name="ck_auth_sessions_role_nonblank"),
        CheckConstraint("length(auth_state_digest) = 64", name="ck_auth_sessions_auth_state_digest_length"),
        CheckConstraint("length(session_digest) = 64", name="ck_auth_sessions_session_digest_length"),
        CheckConstraint("expires_at > created_at", name="ck_auth_sessions_expiry_after_creation"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID_TYPE, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID_TYPE,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    subject: Mapped[str] = mapped_column(String(120), nullable=False)
    role: Mapped[str] = mapped_column(String(40), nullable=False)
    auth_version: Mapped[int] = mapped_column(Integer, nullable=False)
    auth_state_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    session_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[object] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[object | None] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[object | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ServicePrincipal(Base):
    __tablename__ = "service_principals"
    __table_args__ = (
        UniqueConstraint("identifier", name="uq_service_principals_identifier"),
        Index("idx_service_principals_kind_active", "kind", "is_active"),
        Index("idx_service_principals_expires_at", "expires_at"),
        CheckConstraint("trim(identifier) <> ''", name="ck_service_principals_identifier_nonblank"),
        CheckConstraint(
            "kind IN ('desktop','worker','acceptance')",
            name="ck_service_principals_supported_kind",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID_TYPE, primary_key=True, default=uuid.uuid4)
    identifier: Mapped[str] = mapped_column(String(120), nullable=False)
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    scopes: Mapped[list[str]] = mapped_column(JSON_TYPE, nullable=False, default=list)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    expires_at: Mapped[object | None] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[object | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[object] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class ServicePrincipalToken(Base):
    __tablename__ = "service_principal_tokens"
    __table_args__ = (
        UniqueConstraint("token_digest", name="uq_service_principal_tokens_token_digest"),
        Index(
            "idx_service_principal_tokens_principal_active",
            "principal_id",
            "revoked_at",
            "expires_at",
        ),
        Index("idx_service_principal_tokens_expires_at", "expires_at"),
        CheckConstraint("length(token_digest) = 64", name="ck_service_principal_tokens_digest_length"),
        CheckConstraint("expires_at > issued_at", name="ck_service_principal_tokens_expiry_after_issue"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID_TYPE, primary_key=True, default=uuid.uuid4)
    principal_id: Mapped[uuid.UUID] = mapped_column(
        UUID_TYPE,
        ForeignKey("service_principals.id", ondelete="CASCADE"),
        nullable=False,
    )
    token_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    issued_at: Mapped[object] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[object] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[object | None] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[object | None] = mapped_column(DateTime(timezone=True))
    replaced_by_token_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID_TYPE,
        ForeignKey("service_principal_tokens.id", ondelete="SET NULL"),
    )


class AuditLog(Base):
    __tablename__ = "audit_log"
    __table_args__ = (
        CheckConstraint(
            "actor_user_id IS NULL OR actor_service_principal_id IS NULL",
            name="ck_audit_log_single_authenticated_actor",
        ),
        CheckConstraint(
            "actor_subject IS NULL OR trim(actor_subject) <> ''",
            name="ck_audit_log_actor_subject_nonblank",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID_TYPE, primary_key=True, default=uuid.uuid4)
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID_TYPE, ForeignKey("users.id", ondelete="SET NULL"))
    actor_service_principal_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID_TYPE,
        ForeignKey("service_principals.id", ondelete="SET NULL"),
    )
    actor_subject: Mapped[str | None] = mapped_column(String(120))
    action: Mapped[str] = mapped_column(String(120), nullable=False)
    entity_type: Mapped[str | None] = mapped_column(String(80))
    entity_id: Mapped[str | None] = mapped_column(String(120))
    payload: Mapped[dict] = mapped_column(JSON_TYPE, nullable=False, default=dict)
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True), server_default=func.now())
