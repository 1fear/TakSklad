import uuid

from sqlalchemy import JSON, Boolean, Date, DateTime, ForeignKey, Index, Integer, String, Text, Uuid, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


JSON_TYPE = JSON().with_variant(JSONB, "postgresql")
UUID_TYPE = Uuid(as_uuid=True)


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[uuid.UUID] = mapped_column(UUID_TYPE, primary_key=True, default=uuid.uuid4)
    source: Mapped[str] = mapped_column(String(40), nullable=False, default="google_sheets")
    external_id: Mapped[str | None] = mapped_column(String(120))
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


class OrderItem(Base):
    __tablename__ = "order_items"

    id: Mapped[uuid.UUID] = mapped_column(UUID_TYPE, primary_key=True, default=uuid.uuid4)
    order_id: Mapped[uuid.UUID] = mapped_column(UUID_TYPE, ForeignKey("orders.id", ondelete="CASCADE"), nullable=False)
    product: Mapped[str] = mapped_column(String(255), nullable=False)
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
    __table_args__ = (Index("uq_pending_events_idempotency_key", "idempotency_key", unique=True),)

    id: Mapped[uuid.UUID] = mapped_column(UUID_TYPE, primary_key=True, default=uuid.uuid4)
    event_type: Mapped[str] = mapped_column(String(80), nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(180))
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    payload: Mapped[dict] = mapped_column(JSON_TYPE, nullable=False, default=dict)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[object] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID_TYPE, primary_key=True, default=uuid.uuid4)
    username: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    role: Mapped[str] = mapped_column(String(40), nullable=False, default="operator")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[uuid.UUID] = mapped_column(UUID_TYPE, primary_key=True, default=uuid.uuid4)
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID_TYPE, ForeignKey("users.id", ondelete="SET NULL"))
    action: Mapped[str] = mapped_column(String(120), nullable=False)
    entity_type: Mapped[str | None] = mapped_column(String(80))
    entity_id: Mapped[str | None] = mapped_column(String(120))
    payload: Mapped[dict] = mapped_column(JSON_TYPE, nullable=False, default=dict)
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True), server_default=func.now())
