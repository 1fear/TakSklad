import unittest
import uuid
from datetime import date, datetime, timedelta, timezone
from unittest import mock

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.google_sheets_exporter import build_import_record_row, build_import_sheet_header
from backend.app.google_sheets_sync_worker import (
    RETURN_DATE_COLUMN,
    RETURN_REFERENCE_COLUMN,
    RETURN_STATUS_COLUMN,
    RETURNED_BY_COLUMN,
    backend_sync_mutation_batch_size,
    merge_google_sheet_records,
    run_google_sheets_worker_cycle,
    split_codes,
    sync_google_sheet_to_backend,
)
from backend.app.models import AuditLog, Base, Order, OrderItem, ScanCode
from backend.app.orders_service import list_active_orders


class FakeSheet:
    def __init__(self, rows):
        self.rows = rows
        self.batch_updates = []

    def get_all_values(self):
        return self.rows

    def batch_update(self, updates, value_input_option=None):
        self.batch_updates.extend(updates)


class GoogleSheetsSyncWorkerTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)

    def tearDown(self):
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()

    def test_merge_prefers_active_data_row_over_archived_duplicate(self):
        active = {
            "source_import_id": "import-1",
            "source_order_id": "active-order-1",
            "order_date": date(2026, 7, 13),
            "source_sheet": "data",
        }
        archived_duplicate = {
            "source_import_id": "import-1",
            "source_order_id": "archived-order-1",
            "order_date": date(2026, 6, 10),
            "source_sheet": "Архив",
            "archived": True,
        }
        archived_only = {
            "source_import_id": "import-2",
            "source_order_id": "archived-order-2",
            "order_date": date(2026, 6, 11),
            "source_sheet": "Архив",
            "archived": True,
        }

        records = merge_google_sheet_records([active], [archived_duplicate, archived_only])

        self.assertEqual(records, [active, archived_only])

    def seed_order(
        self,
        *,
        order_status="not_completed",
        item_status="not_completed",
        quantity_blocks=15,
        scanned_blocks=0,
    ):
        with self.SessionLocal() as db:
            order = Order(
                payment_type="Перечисление",
                client="Old Client",
                address="Old Address",
                representative="Old Rep",
                order_date=date(2026, 5, 31),
                status=order_status,
                raw_payload={"source": "test"},
            )
            item = OrderItem(
                order=order,
                product="Chapman Brown OP 20",
                quantity_pieces=150,
                quantity_blocks=quantity_blocks,
                pieces_per_block=10,
                scanned_blocks=scanned_blocks,
                status=item_status,
                raw_payload={
                    "source_import_id": "import-1",
                    "source_order_id": "order-1",
                    "block_price": 240000,
                },
            )
            db.add_all([order, item])
            db.commit()
            return str(order.id), str(item.id)

    def make_sheet(self, **overrides):
        record = {
            "Дата отгрузки": "01.06.2026",
            "Тип оплаты": "Терминал",
            "Клиент": "New Client",
            "Адрес": "New Address",
            "Торговый представитель": "New Rep",
            "Товары": "Chapman Red OP 20",
            "Кол-во ШТ": 110,
            "Кол-во блок": 11,
            "Статус": "Не выполнено",
            "ID заказа": "order-1",
            "ID импорта": "import-1",
            "Источник файла": "orders.xlsx",
            "Строка файла": "2",
            "Номер заявки SkladBot": "SB-100",
            "ID заявки SkladBot": "100",
            "Статус SkladBot": "found",
        }
        record.update(overrides)
        return FakeSheet([build_import_sheet_header(), build_import_record_row(record)])

    def make_return_sheet(self):
        header = build_import_sheet_header()
        row = build_import_record_row({
            "Дата отгрузки": "01.06.2026",
            "Тип оплаты": "Перечисление",
            "Клиент": "Old Client",
            "Адрес": "Old Address",
            "Торговый представитель": "Old Rep",
            "Товары": "Chapman Brown OP 20",
            "Кол-во ШТ": 150,
            "Кол-во блок": 15,
            "Отсканированные коды": "0101\n0102",
            "Статус": "Выполнено",
            "ID заказа": "order-1",
            "ID импорта": "import-1",
            "Номер заявки SkladBot": "SB-100",
            "ID заявки SkladBot": "100",
            "Статус SkladBot": "found",
        })
        for column, value in (
            (RETURN_STATUS_COLUMN, "Возврат"),
            (RETURN_DATE_COLUMN, "31.05.2026 23:30:00"),
            (RETURN_REFERENCE_COLUMN, "SB-100"),
            (RETURNED_BY_COLUMN, "desktop-test"),
        ):
            header.append(column)
            row.append(value)
        return FakeSheet([header, row])

    def add_second_item(self, order_id, *, scanned_blocks=0):
        with self.SessionLocal() as db:
            order = db.get(Order, uuid.UUID(order_id))
            item = OrderItem(
                order=order,
                product="Chapman Gold SSL 100`20",
                quantity_pieces=20,
                quantity_blocks=2,
                pieces_per_block=10,
                scanned_blocks=scanned_blocks,
                status="not_completed",
                raw_payload={
                    "source_import_id": "import-2",
                    "source_order_id": "order-1",
                    "block_price": 240000,
                },
            )
            db.add(item)
            db.commit()
            return str(item.id)

    def test_sync_updates_active_backend_order_from_google_sheet_by_import_id(self):
        order_id, item_id = self.seed_order()

        with self.SessionLocal() as db:
            result = sync_google_sheet_to_backend(db, sheet=self.make_sheet())

        self.assertEqual(result["rows"], 1)
        self.assertEqual(result["matched"], 1)
        self.assertEqual(result["orders_updated"], 1)
        self.assertEqual(result["items_updated"], 1)
        self.assertEqual(result["conflicts"], 0)

        with self.SessionLocal() as db:
            order = db.get(Order, uuid.UUID(order_id))
            item = db.get(OrderItem, uuid.UUID(item_id))
            self.assertEqual(order.order_date, date(2026, 6, 1))
            self.assertEqual(order.payment_type, "Терминал")
            self.assertEqual(order.client, "New Client")
            self.assertEqual(order.address, "New Address")
            self.assertEqual(order.representative, "New Rep")
            self.assertEqual(order.raw_payload["skladbot_request_number"], "SB-100")
            self.assertEqual(order.raw_payload["skladbot_request_id"], "100")
            self.assertEqual(item.product, "Chapman Red OP 20")
            self.assertEqual(item.quantity_pieces, 110)
            self.assertEqual(item.quantity_blocks, 11)
            self.assertEqual(item.raw_payload["source_file"], "orders.xlsx")
            self.assertTrue(item.raw_payload["google_sheet_synced_at"])

    def test_worker_cycle_skips_backend_read_when_pending_exports_paused(self):
        pending_processor = mock.Mock(return_value={"status": "paused", "synced": 0, "failed": 0})
        backend_syncer = mock.Mock()

        pending_result, result, next_backend_sync_at = run_google_sheets_worker_cycle(
            mock.Mock(),
            backend_sync_enabled=True,
            next_backend_sync_at=0,
            backend_sync_interval=300,
            rate_limit_cooldown=120,
            now_monotonic=100,
            pending_processor=pending_processor,
            cooldown_reader=mock.Mock(return_value=None),
            backend_syncer=backend_syncer,
        )

        self.assertEqual(pending_result["status"], "paused")
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "pending_export_paused")
        self.assertEqual(next_backend_sync_at, 220)
        backend_syncer.assert_not_called()

    def test_worker_cycle_cools_down_backend_read_after_rate_limit(self):
        pending_processor = mock.Mock(return_value={"status": "completed", "synced": 0, "failed": 0})
        backend_syncer = mock.Mock(side_effect=RuntimeError("APIError: [429] retry-after: 45"))

        with mock.patch("backend.app.google_sheets_sync_worker.logging.warning"):
            _pending_result, result, next_backend_sync_at = run_google_sheets_worker_cycle(
                mock.Mock(),
                backend_sync_enabled=True,
                next_backend_sync_at=0,
                backend_sync_interval=300,
                rate_limit_cooldown=120,
                now_monotonic=100,
                pending_processor=pending_processor,
                cooldown_reader=mock.Mock(return_value=None),
                backend_syncer=backend_syncer,
            )

        self.assertEqual(result["status"], "paused")
        self.assertEqual(result["reason"], "rate_limited")
        self.assertIn("429", result["error"])
        self.assertEqual(next_backend_sync_at, 145)
        backend_syncer.assert_called_once()

    def test_worker_cycle_skips_backend_read_during_persistent_export_cooldown(self):
        pending_processor = mock.Mock(return_value={"status": "completed", "synced": 0, "failed": 0})
        backend_syncer = mock.Mock()
        cooldown_reader = mock.Mock(return_value=datetime.now(timezone.utc) + timedelta(seconds=90))

        _pending_result, result, next_backend_sync_at = run_google_sheets_worker_cycle(
            mock.Mock(),
            backend_sync_enabled=True,
            next_backend_sync_at=0,
            backend_sync_interval=300,
            rate_limit_cooldown=120,
            now_monotonic=100,
            pending_processor=pending_processor,
            cooldown_reader=cooldown_reader,
            backend_syncer=backend_syncer,
        )

        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "pending_export_cooldown")
        self.assertGreaterEqual(next_backend_sync_at, 189)
        backend_syncer.assert_not_called()

    def test_worker_cycle_rolls_back_and_opens_circuit_after_postgres_lock_capacity_error(self):
        db = mock.Mock()
        pending_processor = mock.Mock(return_value={"status": "completed", "synced": 0, "failed": 0})
        backend_syncer = mock.Mock(side_effect=RuntimeError(
            "out of shared memory; You might need to increase max_locks_per_transaction"
        ))

        _pending_result, result, next_backend_sync_at = run_google_sheets_worker_cycle(
            db,
            backend_sync_enabled=True,
            next_backend_sync_at=0,
            backend_sync_interval=300,
            rate_limit_cooldown=120,
            database_error_cooldown=600,
            now_monotonic=100,
            pending_processor=pending_processor,
            cooldown_reader=mock.Mock(return_value=None),
            backend_syncer=backend_syncer,
        )

        self.assertEqual(result["status"], "paused")
        self.assertEqual(result["reason"], "postgres_lock_capacity")
        self.assertTrue(result["circuit_open"])
        self.assertEqual(next_backend_sync_at, 700)
        db.rollback.assert_called_once_with()
        db.add.assert_called_once()
        db.commit.assert_called_once_with()

    def test_worker_restart_honors_durable_open_circuit(self):
        now = datetime.now(timezone.utc)
        with self.SessionLocal() as db:
            db.add(AuditLog(
                action="google_sheets_backend_sync_circuit_open",
                entity_type="google_sheets",
                entity_id="data",
                created_at=now,
                payload={
                    "reason": "postgres_lock_capacity",
                    "opened_at": now.isoformat(),
                    "retry_at": (now + timedelta(minutes=15)).isoformat(),
                    "cooldown_seconds": 900,
                },
            ))
            db.commit()
            backend_syncer = mock.Mock()
            _pending, result, _next = run_google_sheets_worker_cycle(
                db,
                backend_sync_enabled=True,
                next_backend_sync_at=0,
                now_monotonic=0,
                pending_processor=mock.Mock(return_value={"status": "completed"}),
                cooldown_reader=mock.Mock(return_value=None),
                backend_syncer=backend_syncer,
            )

        self.assertEqual(result["reason"], "circuit_open")
        backend_syncer.assert_not_called()

    def test_half_open_circuit_closes_only_after_successful_probe(self):
        now = datetime.now(timezone.utc)
        with self.SessionLocal() as db:
            db.add(AuditLog(
                action="google_sheets_backend_sync_circuit_open",
                entity_type="google_sheets",
                entity_id="data",
                created_at=now - timedelta(minutes=20),
                payload={
                    "reason": "postgres_lock_capacity",
                    "opened_at": (now - timedelta(minutes=20)).isoformat(),
                    "retry_at": (now - timedelta(minutes=5)).isoformat(),
                    "cooldown_seconds": 900,
                },
            ))
            db.commit()
            backend_syncer = mock.Mock(return_value={"rows": 1, "matched": 1, "mutation_batches": 0})
            _pending, result, _next = run_google_sheets_worker_cycle(
                db,
                backend_sync_enabled=True,
                next_backend_sync_at=0,
                now_monotonic=0,
                pending_processor=mock.Mock(return_value={"status": "completed"}),
                cooldown_reader=mock.Mock(return_value=None),
                backend_syncer=backend_syncer,
            )
            actions = db.execute(
                select(AuditLog.action)
                .where(AuditLog.action.like("google_sheets_backend_sync_circuit_%"))
            ).scalars().all()

        self.assertEqual(result["matched"], 1)
        self.assertIn("google_sheets_backend_sync_circuit_closed", actions)

    def test_sync_skips_repeated_noop_summary_audit(self):
        self.seed_order()
        sheet = self.make_sheet()

        with self.SessionLocal() as db:
            first = sync_google_sheet_to_backend(db, sheet=sheet)
        with self.SessionLocal() as db:
            second = sync_google_sheet_to_backend(db, sheet=sheet)

        self.assertEqual(first["items_updated"], 1)
        self.assertEqual(second["orders_updated"], 0)
        self.assertEqual(second["items_updated"], 0)
        self.assertEqual(second["conflicts"], 0)
        with self.SessionLocal() as db:
            audits = db.execute(
                select(AuditLog).where(AuditLog.action == "google_sheets_backend_sync")
            ).scalars().all()
            self.assertEqual(len(audits), 1)

    def test_sync_deduplicates_repeated_conflict_audit(self):
        self.seed_order(scanned_blocks=12)
        stale_sheet = self.make_sheet(
            **{
                "Дата отгрузки": "31.05.2026",
                "Тип оплаты": "Перечисление",
                "Клиент": "Old Client",
                "Адрес": "Old Address",
                "Торговый представитель": "Old Rep",
                "Товары": "Chapman Brown OP 20",
                "Кол-во ШТ": 110,
                "Кол-во блок": 11,
                "Номер заявки SkladBot": "",
                "ID заявки SkladBot": "",
                "Статус SkladBot": "",
            }
        )

        with self.SessionLocal() as db:
            first = sync_google_sheet_to_backend(db, sheet=stale_sheet)
        with self.SessionLocal() as db:
            second = sync_google_sheet_to_backend(db, sheet=stale_sheet)

        self.assertEqual(first["conflicts"], 1)
        self.assertEqual(second["conflicts"], 1)
        with self.SessionLocal() as db:
            conflicts = db.execute(
                select(AuditLog).where(AuditLog.action == "google_sheets_backend_sync_conflict")
            ).scalars().all()
            summaries = db.execute(
                select(AuditLog).where(AuditLog.action == "google_sheets_backend_sync")
            ).scalars().all()
            self.assertEqual(len(conflicts), 1)
            self.assertEqual(len(summaries), 1)

    def test_sync_does_not_replace_real_backend_address_with_gps_from_google_sheet(self):
        order_id, _item_id = self.seed_order()

        with self.SessionLocal() as db:
            order = db.get(Order, uuid.UUID(order_id))
            order.address = "Ташкент, геокодированный адрес 1"
            db.commit()

        with self.SessionLocal() as db:
            result = sync_google_sheet_to_backend(
                db,
                sheet=self.make_sheet(
                    **{
                        "Адрес": "GPS: 41.311081,69.240562",
                        "Товары": "Chapman Brown OP 20",
                        "Кол-во ШТ": 150,
                        "Кол-во блок": 15,
                    }
                ),
            )

        self.assertEqual(result["matched"], 1)
        with self.SessionLocal() as db:
            order = db.get(Order, uuid.UUID(order_id))
            self.assertEqual(order.address, "Ташкент, геокодированный адрес 1")

    def test_sync_does_not_downgrade_existing_skladbot_link_from_stale_google_mirror(self):
        order_id, _item_id = self.seed_order()

        with self.SessionLocal() as db:
            order = db.get(Order, uuid.UUID(order_id))
            order.raw_payload = {
                **order.raw_payload,
                "skladbot_request_number": "WH-R-199186",
                "skladbot_request_id": "199186",
                "skladbot_status": "created",
                "skladbot_created_by_taksklad": True,
            }
            db.commit()

        with self.SessionLocal() as db:
            result = sync_google_sheet_to_backend(
                db,
                sheet=self.make_sheet(
                    **{
                        "Товары": "Chapman Brown OP 20",
                        "Кол-во ШТ": 150,
                        "Кол-во блок": 15,
                        "Номер заявки SkladBot": "",
                        "ID заявки SkladBot": "",
                        "Статус SkladBot": "Проверяется",
                    }
                ),
            )

        self.assertEqual(result["matched"], 1)
        with self.SessionLocal() as db:
            order = db.get(Order, uuid.UUID(order_id))
            self.assertEqual(order.raw_payload["skladbot_request_number"], "WH-R-199186")
            self.assertEqual(order.raw_payload["skladbot_request_id"], "199186")
            self.assertEqual(order.raw_payload["skladbot_status"], "created")

    def test_sync_recalculates_line_total_when_google_blocks_change(self):
        _, item_id = self.seed_order()

        with self.SessionLocal() as db:
            item = db.get(OrderItem, uuid.UUID(item_id))
            item.raw_payload = {
                **item.raw_payload,
                "line_total": 3_600_000,
                "calculated_line_total": 3_600_000,
            }
            db.commit()

        with self.SessionLocal() as db:
            result = sync_google_sheet_to_backend(
                db,
                sheet=self.make_sheet(
                    **{
                        "Товары": "Chapman Brown OP 20",
                        "Кол-во ШТ": 10,
                        "Кол-во блок": 1,
                    }
                ),
            )

        self.assertEqual(result["matched"], 1)
        self.assertEqual(result["items_updated"], 1)
        with self.SessionLocal() as db:
            item = db.get(OrderItem, uuid.UUID(item_id))
            self.assertEqual(item.quantity_blocks, 1)
            self.assertEqual(item.raw_payload["block_price"], 240000)
            self.assertEqual(item.raw_payload["line_total"], 240000)
            self.assertEqual(item.raw_payload["calculated_line_total"], 240000)

    def test_sync_preserves_google_repriced_line_total(self):
        _, item_id = self.seed_order(quantity_blocks=50)

        with self.SessionLocal() as db:
            item = db.get(OrderItem, uuid.UUID(item_id))
            item.raw_payload = {
                **item.raw_payload,
                "imported_line_total": 11_675_000,
                "line_total": 11_675_000,
                "calculated_line_total": 12_000_000,
            }
            db.commit()

        with self.SessionLocal() as db:
            result = sync_google_sheet_to_backend(
                db,
                sheet=self.make_sheet(
                    **{
                        "Товары": "Chapman Brown OP 20",
                        "Кол-во ШТ": 500,
                        "Кол-во блок": 50,
                        "Цена за блок": 240000,
                        "Сумма позиции": 11_675_000,
                    }
                ),
            )

        self.assertEqual(result["matched"], 1)
        self.assertEqual(result["items_updated"], 1)
        with self.SessionLocal() as db:
            item = db.get(OrderItem, uuid.UUID(item_id))
            self.assertEqual(item.raw_payload["block_price"], 240000)
            self.assertEqual(item.raw_payload["calculated_line_total"], 12_000_000)
            self.assertEqual(item.raw_payload["line_total"], 11_675_000)

    def test_sync_keeps_backend_item_removed_from_google_sheet_when_unscanned(self):
        order_id, _ = self.seed_order()
        removed_item_id = self.add_second_item(order_id)

        with self.SessionLocal() as db:
            result = sync_google_sheet_to_backend(db, sheet=self.make_sheet())

        self.assertEqual(result["matched"], 1)
        self.assertEqual(result["removed"], 0)
        self.assertEqual(result["conflicts"], 1)
        with self.SessionLocal() as db:
            removed_item = db.get(OrderItem, uuid.UUID(removed_item_id))
            self.assertEqual(removed_item.status, "not_completed")
            active_orders = list_active_orders(db)
            self.assertEqual(len(active_orders), 1)
            self.assertEqual(len(active_orders[0].items), 2)

    def test_sync_keeps_backend_item_missing_from_google_sheet_when_scanned(self):
        order_id, _ = self.seed_order()
        scanned_item_id = self.add_second_item(order_id, scanned_blocks=1)

        with self.SessionLocal() as db:
            result = sync_google_sheet_to_backend(db, sheet=self.make_sheet())

        self.assertEqual(result["matched"], 1)
        self.assertEqual(result["removed"], 0)
        self.assertEqual(result["conflicts"], 1)
        with self.SessionLocal() as db:
            scanned_item = db.get(OrderItem, uuid.UUID(scanned_item_id))
            self.assertEqual(scanned_item.status, "not_completed")
            audit = db.execute(
                select(AuditLog).where(AuditLog.action == "google_sheets_backend_sync_conflict")
            ).scalars().all()
            self.assertEqual(len(audit), 1)

    def test_sync_imports_scanned_codes_and_completes_item_from_google_sheet(self):
        order_id, item_id = self.seed_order(quantity_blocks=2)

        with self.SessionLocal() as db:
            result = sync_google_sheet_to_backend(
                db,
                sheet=self.make_sheet(
                    **{
                        "Товары": "Chapman Brown OP 20",
                        "Кол-во ШТ": 20,
                        "Кол-во блок": 2,
                        "Отсканированные коды": "0101\n0102",
                    }
                ),
            )

        self.assertEqual(result["matched"], 1)
        self.assertEqual(result["items_updated"], 1)

        with self.SessionLocal() as db:
            order = db.get(Order, uuid.UUID(order_id))
            item = db.get(OrderItem, uuid.UUID(item_id))
            codes = db.execute(select(ScanCode).order_by(ScanCode.code)).scalars().all()
            self.assertEqual([scan.code for scan in codes], ["0101", "0102"])
            self.assertEqual(codes[0].source, "google_sheets")
            self.assertEqual(item.scanned_blocks, 2)
            self.assertEqual(item.status, "completed")
            self.assertEqual(order.status, "completed")

    def test_sync_does_not_lock_unchanged_existing_same_item_kiz(self):
        _order_id, item_id = self.seed_order(quantity_blocks=1, scanned_blocks=1)
        with self.SessionLocal() as db:
            db.add(ScanCode(
                order_item_id=uuid.UUID(item_id),
                code="0101",
                source="google_sheets",
                scanned_at=datetime(2026, 7, 15, tzinfo=timezone.utc),
                raw_payload={},
            ))
            db.commit()

        with mock.patch(
            "backend.app.google_sheets_sync_worker.lock_kiz_codes_for_transaction"
        ) as lock_codes:
            with self.SessionLocal() as db:
                result = sync_google_sheet_to_backend(
                    db,
                    sheet=self.make_sheet(
                        **{
                            "Товары": "Chapman Brown OP 20",
                            "Кол-во ШТ": 10,
                            "Кол-во блок": 1,
                            "Отсканированные коды": "0101",
                        }
                    ),
                )

        self.assertEqual(result["matched"], 1)
        lock_codes.assert_not_called()

    def test_sync_commits_new_kiz_mutations_in_bounded_checkpointed_batches(self):
        codes = [f"01{index:04d}" for index in range(70)]
        self.seed_order(quantity_blocks=len(codes))
        sheet = self.make_sheet(
            **{
                "Товары": "Chapman Brown OP 20",
                "Кол-во ШТ": len(codes) * 10,
                "Кол-во блок": len(codes),
                "Отсканированные коды": "\n".join(reversed(codes)),
            }
        )

        with mock.patch(
            "backend.app.google_sheets_sync_worker.lock_kiz_codes_for_transaction",
            side_effect=lambda _db, codes: len(codes),
        ) as lock_codes:
            with self.SessionLocal() as db:
                result = sync_google_sheet_to_backend(db, sheet=sheet, mutation_batch_size=250)

        self.assertEqual(backend_sync_mutation_batch_size(250), 32)
        self.assertEqual(result["mutation_codes"], 70)
        self.assertEqual(result["mutation_batches"], 3)
        self.assertEqual(lock_codes.call_args_list[0].args[1], tuple(codes[:32]))
        self.assertEqual(lock_codes.call_args_list[1].args[1], tuple(codes[32:64]))
        self.assertEqual(lock_codes.call_args_list[2].args[1], tuple(codes[64:]))
        with self.SessionLocal() as db:
            checkpoints = db.execute(
                select(AuditLog)
                .where(AuditLog.action == "google_sheets_backend_sync_checkpoint")
            ).scalars().all()
            checkpoints.sort(key=lambda row: row.payload["batch_number"])
            self.assertEqual([row.payload["batch_size"] for row in checkpoints], [32, 32, 6])
            self.assertEqual([row.payload["mutation_codes_committed"] for row in checkpoints], [32, 64, 70])

    def test_sync_resumes_after_a_later_kiz_batch_fails(self):
        codes = [f"02{index:04d}" for index in range(40)]
        self.seed_order(quantity_blocks=len(codes))
        sheet = self.make_sheet(
            **{
                "Товары": "Chapman Brown OP 20",
                "Кол-во ШТ": len(codes) * 10,
                "Кол-во блок": len(codes),
                "Отсканированные коды": "\n".join(codes),
            }
        )
        lock_calls = 0

        def fail_second_batch(_db, batch):
            nonlocal lock_calls
            lock_calls += 1
            if lock_calls == 2:
                raise RuntimeError(
                    "out of shared memory; You might need to increase max_locks_per_transaction"
                )
            return len(batch)

        with mock.patch(
            "backend.app.google_sheets_sync_worker.lock_kiz_codes_for_transaction",
            side_effect=fail_second_batch,
        ):
            with self.assertRaisesRegex(RuntimeError, "out of shared memory"):
                with self.SessionLocal() as db:
                    sync_google_sheet_to_backend(db, sheet=sheet)

        with self.SessionLocal() as db:
            persisted_after_failure = db.execute(select(ScanCode)).scalars().all()
            self.assertEqual(len(persisted_after_failure), 32)

        with mock.patch(
            "backend.app.google_sheets_sync_worker.lock_kiz_codes_for_transaction",
            side_effect=lambda _db, batch: len(batch),
        ) as resumed_lock:
            with self.SessionLocal() as db:
                resumed = sync_google_sheet_to_backend(db, sheet=sheet)

        self.assertEqual(resumed["mutation_codes"], 8)
        self.assertEqual(resumed["mutation_batches"], 1)
        self.assertEqual(resumed_lock.call_args.args[1], tuple(codes[32:]))
        with self.SessionLocal() as db:
            all_codes = db.execute(select(ScanCode).order_by(ScanCode.code)).scalars().all()
            item = db.execute(select(OrderItem)).scalar_one()
            order = db.execute(select(Order)).scalar_one()
            self.assertEqual([row.code for row in all_codes], codes)
            self.assertEqual(item.scanned_blocks, 40)
            self.assertEqual(item.status, "completed")
            self.assertEqual(order.status, "completed")

    def test_split_codes_keeps_comma_inside_kiz(self):
        first = "01012345678901234567ABC,DEF"
        second = "01012345678901234567XYZ"

        self.assertEqual(split_codes(f"{first}\n{second}"), [first, second])

    def test_sync_does_not_complete_incomplete_item_with_stale_completed_status(self):
        order_id, item_id = self.seed_order(quantity_blocks=2)

        with self.SessionLocal() as db:
            result = sync_google_sheet_to_backend(
                db,
                sheet=self.make_sheet(
                    **{
                        "Товары": "Chapman Brown OP 20",
                        "Кол-во ШТ": 20,
                        "Кол-во блок": 2,
                        "Отсканированные коды": "01012345678901234567ABC",
                        "Статус": "Выполнено",
                    }
                ),
            )

        self.assertEqual(result["matched"], 1)
        with self.SessionLocal() as db:
            order = db.get(Order, uuid.UUID(order_id))
            item = db.get(OrderItem, uuid.UUID(item_id))
            self.assertEqual(item.scanned_blocks, 1)
            self.assertEqual(item.status, "not_completed")
            self.assertEqual(order.status, "not_completed")

    def test_sync_rejects_quantity_lower_than_already_scanned_blocks(self):
        _, item_id = self.seed_order(scanned_blocks=12)

        with self.SessionLocal() as db:
            result = sync_google_sheet_to_backend(
                db,
                sheet=self.make_sheet(
                    **{
                        "Дата отгрузки": "31.05.2026",
                        "Тип оплаты": "Перечисление",
                        "Клиент": "Old Client",
                        "Адрес": "Old Address",
                        "Торговый представитель": "Old Rep",
                        "Товары": "Chapman Brown OP 20",
                        "Кол-во ШТ": 150,
                        "Кол-во блок": 11,
                    }
                ),
            )

        self.assertEqual(result["matched"], 1)
        self.assertEqual(result["items_updated"], 0)
        self.assertEqual(result["conflicts"], 1)

        with self.SessionLocal() as db:
            item = db.get(OrderItem, uuid.UUID(item_id))
            self.assertEqual(item.quantity_blocks, 15)
            conflicts = db.execute(
                select(AuditLog).where(AuditLog.action == "google_sheets_backend_sync_conflict")
            ).scalars().all()
            self.assertEqual(len(conflicts), 1)
            self.assertEqual(conflicts[0].payload["conflicts"][0]["field"], "quantity_blocks")

    def test_sync_keeps_completed_orders_in_sync_from_google_sheet(self):
        order_id, item_id = self.seed_order(order_status="completed", item_status="completed")

        with self.SessionLocal() as db:
            result = sync_google_sheet_to_backend(db, sheet=self.make_sheet())

        self.assertEqual(result["matched"], 1)
        self.assertEqual(result["missing"], 0)
        self.assertEqual(result["orders_updated"], 1)
        self.assertEqual(result["items_updated"], 1)

        with self.SessionLocal() as db:
            order = db.get(Order, uuid.UUID(order_id))
            item = db.get(OrderItem, uuid.UUID(item_id))
            self.assertEqual(order.client, "New Client")
            self.assertEqual(item.quantity_blocks, 11)

    def test_sync_archives_completed_backend_order_still_present_in_data_sheet(self):
        order_id, _ = self.seed_order(order_status="completed", item_status="completed")
        archived_ids = []

        with mock.patch(
            "backend.app.google_sheets_sync_worker.archive_backend_order_to_google_sheets",
            side_effect=lambda order: archived_ids.append(str(order.id)) or {"status": "completed", "updated": 1},
        ):
            with self.SessionLocal() as db:
                result = sync_google_sheet_to_backend(
                    db,
                    sheet=self.make_sheet(),
                    archive_completed_data_rows=True,
                )

        self.assertEqual(result["matched"], 1)
        self.assertEqual(result["archived"], 1)
        self.assertEqual(archived_ids, [order_id])
        with self.SessionLocal() as db:
            audit = db.execute(
                select(AuditLog).where(AuditLog.action == "google_sheets_sync_archive_export")
            ).scalar_one()
            self.assertEqual(audit.entity_id, order_id)
            self.assertEqual(audit.payload["status"], "completed")

    def test_sync_ignores_manual_google_return_columns_and_keeps_backend_source_of_truth(self):
        order_id, item_id = self.seed_order(order_status="completed", item_status="completed")

        with self.SessionLocal() as db:
            result = sync_google_sheet_to_backend(db, sheet=self.make_return_sheet())

        self.assertEqual(result["matched"], 1)
        self.assertEqual(result["conflicts"], 1)

        with self.SessionLocal() as db:
            order = db.get(Order, uuid.UUID(order_id))
            item = db.get(OrderItem, uuid.UUID(item_id))
            self.assertEqual(order.status, "completed")
            self.assertEqual(item.status, "completed")
            self.assertNotIn("return_status", order.raw_payload)
            self.assertNotIn("returned_at", order.raw_payload)
            self.assertNotIn("return_reference", order.raw_payload)
            self.assertNotIn("returned_by", order.raw_payload)
            audit = db.execute(
                select(AuditLog).where(AuditLog.action == "google_sheets_backend_sync_conflict")
            ).scalar_one()
            self.assertEqual(audit.payload["conflicts"][0]["field"], "return_status")
            self.assertIn("backend return endpoint", audit.payload["conflicts"][0]["reason"])


if __name__ == "__main__":
    unittest.main()
