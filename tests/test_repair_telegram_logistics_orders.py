import copy
import hashlib
import unittest
import uuid
from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest import mock

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from backend.app.imports_service import source_import_lookup_key
from backend.app.models import (
    AuditLog,
    Base,
    ImportFile,
    ImportJob,
    KizCode,
    KizMovement,
    Order,
    OrderItem,
    PendingEvent,
    ScanCode,
)
from tools.repair_telegram_logistics_orders import (
    APPLY_APPROVAL,
    EXPECTED_SCAN_COUNT,
    EXPECTED_SOURCE_FILE_COUNT,
    EXPECTED_TARGET_COUNT,
    EXPECTED_TARGET_ITEM_COUNT,
    ROLLBACK_APPROVAL,
    TARGET_DATE,
    TARGET_REFS,
    ParsedSource,
    RepairBlocked,
    apply_plan,
    create_plan,
    ensure_coordinates_header_alias,
    rollback_applied,
    run,
    validate_preimage_file,
    verify_applied,
    write_preimage_file,
)


SOURCE_SHAS = ("a" * 64, "b" * 64)
EVENT_IDS = tuple(
    uuid.uuid5(uuid.NAMESPACE_URL, f"telegram-logistics-repair-event-{index}")
    for index in range(EXPECTED_SOURCE_FILE_COUNT)
)
IMPORT_IDS = tuple(
    uuid.uuid5(uuid.NAMESPACE_URL, f"telegram-logistics-repair-import-{index}")
    for index in range(EXPECTED_SOURCE_FILE_COUNT)
)
EVENT_ID = EVENT_IDS[0]


class RepairTelegramLogisticsOrdersTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)

    def seed_scope(self, db):
        source_item_counts = (90, 140)
        source_boundaries = (source_item_counts[0], sum(source_item_counts))
        parsed_rows = {event_id: {} for event_id in EVENT_IDS}
        for source_index, (event_id, import_id, source_sha, row_count) in enumerate(
            zip(EVENT_IDS, IMPORT_IDS, SOURCE_SHAS, source_item_counts, strict=True)
        ):
            filename = f"synthetic-{source_index + 1}.xlsx"
            db.add_all([
                PendingEvent(
                    id=event_id,
                    event_type="telegram_excel_import",
                    status="completed",
                    attempts=1,
                    payload={
                        "document": {
                            "file_id": f"synthetic-file-id-{source_index + 1}",
                            "file_name": filename,
                        },
                        "file_name": filename,
                        "shipment_date": "22.07.2026",
                    },
                ),
                ImportJob(
                    id=import_id,
                    source="telegram",
                    status="completed",
                    rows_total=row_count,
                    rows_imported=row_count,
                    raw_payload={
                        "filename": filename,
                        "sha256": source_sha,
                        "telegram_event_id": str(event_id),
                    },
                ),
                ImportFile(
                    id=uuid.uuid5(
                        uuid.NAMESPACE_URL,
                        f"telegram-logistics-repair-file-{source_index}",
                    ),
                    import_id=import_id,
                    filename=filename,
                    sha256=source_sha,
                    size_bytes=1,
                ),
            ])

        item_index = 0
        scan_index = 0
        for index, ref in enumerate(TARGET_REFS):
            order_id = uuid.uuid5(uuid.NAMESPACE_URL, f"repair-order-{ref}")
            client = f"Synthetic Client {index:02d}"
            address = f"Synthetic address {index:02d}"
            coordinates = f"41.{index + 1:02d}, 69.{index + 1:02d}"
            order_item_count = 4 if index < 44 else 3
            order = Order(
                id=order_id,
                source="telegram",
                external_id=f"synthetic-external-{index:02d}",
                import_order_key=f"synthetic-order-key-{index:02d}",
                import_source_order_key=f"synthetic-source-key-{index:02d}",
                order_date=date(2026, 7, 22),
                payment_type="Terminal",
                client=client,
                address="Самовывоз со склада",
                representative="Synthetic Representative",
                status="completed",
                raw_payload={
                    "skladbot_request_number": ref,
                    "skladbot_request_id": f"synthetic-request-{index:02d}",
                    "skladbot_status": "created",
                    "coordinates": "",
                    "source_import_id": f"synthetic-source-{item_index:03d}",
                },
                created_at=datetime(2026, 7, 21, tzinfo=timezone.utc),
                updated_at=datetime(2026, 7, 22, tzinfo=timezone.utc),
            )
            db.add(order)
            for item_offset in range(order_item_count):
                source_index = 0 if item_index < source_boundaries[0] else 1
                import_id = IMPORT_IDS[source_index]
                event_id = EVENT_IDS[source_index]
                filename = f"synthetic-{source_index + 1}.xlsx"
                source_import_id = f"synthetic-source-{item_index:03d}"
                product = f"Synthetic Product {item_offset:02d}"
                block_count = 3 if item_index < 100 else 2
                item_id = uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"repair-item-{ref}-{item_offset}",
                )
                item = OrderItem(
                    id=item_id,
                    order=order,
                    product=product,
                    import_item_key=f"synthetic-item-key-{item_index:03d}",
                    source_import_key=source_import_lookup_key(source_import_id),
                    source_import_id=source_import_id,
                    source_batch_key=f"synthetic-batch-{source_index + 1}",
                    quantity_pieces=10 * block_count,
                    quantity_blocks=block_count,
                    pieces_per_block=10,
                    scanned_blocks=block_count,
                    requires_kiz=True,
                    status="completed",
                    raw_payload={
                        "source_import_id": source_import_id,
                        "backend_import_id": str(import_id),
                        "source_order_id": f"synthetic-source-order-{index:02d}",
                    },
                    created_at=datetime(2026, 7, 21, tzinfo=timezone.utc),
                    updated_at=datetime(2026, 7, 22, tzinfo=timezone.utc),
                )
                db.add(item)
                for block_index in range(block_count):
                    code = f"synthetic-kiz-{scan_index:03d}"
                    scan = ScanCode(
                        id=uuid.uuid5(uuid.NAMESPACE_URL, f"repair-scan-{scan_index}"),
                        order_item=item,
                        code=code,
                        source="synthetic",
                        scanned_by="synthetic",
                        scanned_at=datetime(2026, 7, 22, tzinfo=timezone.utc),
                        raw_payload={"synthetic": True},
                    )
                    kiz = KizCode(
                        id=uuid.uuid5(uuid.NAMESPACE_URL, f"repair-kiz-{scan_index}"),
                        code=code,
                        first_seen_at=datetime(2026, 7, 22, tzinfo=timezone.utc),
                        updated_at=datetime(2026, 7, 22, tzinfo=timezone.utc),
                    )
                    movement = KizMovement(
                        id=uuid.uuid5(uuid.NAMESPACE_URL, f"repair-movement-{scan_index}"),
                        kiz_code=kiz,
                        movement_type="outbound",
                        order_id=order_id,
                        order_item_id=item_id,
                        scan_code_id=scan.id,
                        source="synthetic",
                        actor="synthetic",
                        occurred_at=datetime(2026, 7, 22, tzinfo=timezone.utc),
                        raw_payload={"synthetic": True},
                    )
                    db.add_all([scan, kiz, movement])
                    scan_index += 1
                parsed_rows[event_id][source_import_id] = {
                    "Дата отгрузки": "22.07.2026",
                    "Тип оплаты": "Terminal",
                    "Клиент": client,
                    "Адрес": address,
                    "Координаты": coordinates,
                    "Торговый представитель": "Synthetic Representative",
                    "Товары": product,
                    "Кол-во ШТ": 10 * block_count,
                    "Кол-во блок": block_count,
                    "Статус": "Выполнено",
                    "ID импорта": source_import_id,
                    "ID заказа": f"synthetic-source-order-{index:02d}",
                    "Источник файла": filename,
                    "Строка файла": str(item_index + 2),
                    "Номер заявки SkladBot": ref,
                    "ID заявки SkladBot": f"synthetic-request-{index:02d}",
                }
                item_index += 1
        self.assertEqual(item_index, EXPECTED_TARGET_ITEM_COUNT)
        self.assertEqual(scan_index, EXPECTED_SCAN_COUNT)
        self.assertEqual(tuple(len(parsed_rows[event_id]) for event_id in EVENT_IDS), source_item_counts)
        db.commit()
        return {
            event_id: ParsedSource(
                sha256=SOURCE_SHAS[source_index],
                rows=tuple(parsed_rows[event_id].values()),
            )
            for source_index, event_id in enumerate(EVENT_IDS)
        }

    def test_scope_is_exact_and_immutable(self):
        self.assertEqual(EXPECTED_TARGET_COUNT, 62)
        self.assertEqual(EXPECTED_TARGET_ITEM_COUNT, 230)
        self.assertEqual(EXPECTED_SOURCE_FILE_COUNT, 2)
        self.assertEqual(EXPECTED_SCAN_COUNT, 560)
        self.assertEqual(len(TARGET_REFS), 62)
        self.assertEqual(TARGET_REFS[0], "WH-R-208826")
        self.assertEqual(TARGET_REFS[23], "WH-R-208849")
        self.assertEqual(TARGET_REFS[24], "WH-R-209244")
        self.assertEqual(TARGET_REFS[-1], "WH-R-209281")
        self.assertEqual(TARGET_DATE, date(2026, 7, 23))
        self.assertEqual(APPLY_APPROVAL, "REPAIR-62-LOGISTICS-2026-07-23")
        self.assertEqual(ROLLBACK_APPROVAL, APPLY_APPROVAL)

    def test_clean_plan_is_counts_only_and_hash_stable(self):
        with self.Session() as db:
            sources = self.seed_scope(db)
            first = create_plan(db, parsed_sources=sources, enforce_runtime_guards=False)
            second = create_plan(db, parsed_sources=sources, enforce_runtime_guards=False)

            self.assertTrue(first.summary["safe_to_repair"])
            self.assertEqual(first.summary["target_count"], 62)
            self.assertEqual(first.summary["target_items"], 230)
            self.assertEqual(first.summary["source_files"], 2)
            self.assertEqual(first.summary["scan_count"], 560)
            self.assertEqual(first.summary["unique_kiz_count"], 560)
            self.assertEqual(first.summary["mutations_expected"], 62)
            self.assertEqual(first.summary["plan_sha256"], second.summary["plan_sha256"])
            rendered = str(first.summary)
            self.assertNotIn("Synthetic Client", rendered)
            self.assertNotIn("Synthetic address", rendered)
            self.assertNotIn("synthetic-file-id", rendered)

    def test_missing_target_and_invalid_source_row_block_whole_plan(self):
        with self.Session() as db:
            sources = self.seed_scope(db)
            first_order = db.scalars(select(Order).order_by(Order.id)).first()
            db.delete(first_order)
            db.commit()
            with self.assertRaisesRegex(RepairBlocked, "TARGET_SCOPE_MISMATCH"):
                create_plan(db, parsed_sources=sources, enforce_runtime_guards=False)

        Base.metadata.drop_all(self.engine)
        Base.metadata.create_all(self.engine)
        with self.Session() as db:
            sources = self.seed_scope(db)
            broken = copy.deepcopy(sources)
            rows = list(broken[EVENT_ID].rows)
            rows[0]["Адрес"] = "Самовывоз со склада"
            broken[EVENT_ID] = ParsedSource(sha256=SOURCE_SHAS[0], rows=tuple(rows))
            with self.assertRaisesRegex(RepairBlocked, "SOURCE_ADDRESS_INVALID"):
                create_plan(db, parsed_sources=broken, enforce_runtime_guards=False)

    def test_source_sha_and_materialized_identity_are_required(self):
        with self.Session() as db:
            sources = self.seed_scope(db)
            wrong_sha = copy.deepcopy(sources)
            wrong_sha[EVENT_ID] = ParsedSource(sha256="c" * 64, rows=sources[EVENT_ID].rows)
            with self.assertRaisesRegex(RepairBlocked, "SOURCE_SHA_MISMATCH"):
                create_plan(db, parsed_sources=wrong_sha, enforce_runtime_guards=False)

            item = db.scalars(select(OrderItem).order_by(OrderItem.id)).first()
            item.source_import_key = "broken"
            db.commit()
            with self.assertRaisesRegex(RepairBlocked, "SOURCE_IDENTITY_MISMATCH"):
                create_plan(db, parsed_sources=sources, enforce_runtime_guards=False)

    def test_source_document_and_scan_scope_are_exact(self):
        with self.Session() as db:
            sources = self.seed_scope(db)
            missing_source = dict(sources)
            missing_source.pop(EVENT_IDS[-1])
            with self.assertRaisesRegex(RepairBlocked, "SOURCE_DOCUMENT_SCOPE_MISMATCH"):
                create_plan(db, parsed_sources=missing_source, enforce_runtime_guards=False)

            item = db.scalars(select(OrderItem).order_by(OrderItem.id)).first()
            item.scanned_blocks -= 1
            db.commit()
            with self.assertRaisesRegex(RepairBlocked, "TARGET_SCAN_SCOPE_MISMATCH"):
                create_plan(db, parsed_sources=sources, enforce_runtime_guards=False)

            item.scanned_blocks += 1
            scans = db.scalars(select(ScanCode).order_by(ScanCode.id)).all()
            original_code = scans[1].code
            scans[1].code = scans[0].code
            db.commit()
            with self.assertRaisesRegex(RepairBlocked, "TARGET_SCAN_SCOPE_MISMATCH"):
                create_plan(db, parsed_sources=sources, enforce_runtime_guards=False)

            scans[1].code = original_code
            db.commit()
            db.delete(scans[1])
            db.commit()
            with self.assertRaisesRegex(RepairBlocked, "TARGET_SCAN_SCOPE_MISMATCH"):
                create_plan(db, parsed_sources=sources, enforce_runtime_guards=False)

    def test_coordinates_alias_compatibility_shim_is_strict_and_idempotent(self):
        aliases = {"coordinates": ["Координаты", "GPS"]}
        parser = SimpleNamespace(HEADER_ALIASES=aliases)

        ensure_coordinates_header_alias(parser)
        ensure_coordinates_header_alias(parser)

        self.assertEqual(aliases["coordinates"].count("GPS-координаты клиента"), 1)
        with self.assertRaisesRegex(RepairBlocked, "SOURCE_PARSER_ALIAS_STRUCTURE_INVALID"):
            ensure_coordinates_header_alias(SimpleNamespace(HEADER_ALIASES={"coordinates": ()}))

    def test_kiz_registry_must_match_scans_without_limiting_movement_history(self):
        with self.Session() as db:
            sources = self.seed_scope(db)
            movement = db.scalars(select(KizMovement).order_by(KizMovement.id)).first()
            db.add(KizMovement(
                id=uuid.uuid5(uuid.NAMESPACE_URL, "repair-extra-kiz-history"),
                kiz_id=movement.kiz_id,
                movement_type="returned",
                order_id=movement.order_id,
                order_item_id=movement.order_item_id,
                scan_code_id=movement.scan_code_id,
                source="synthetic-history",
                actor="synthetic",
                occurred_at=datetime(2026, 7, 22, 1, tzinfo=timezone.utc),
                raw_payload={"synthetic": True},
            ))
            db.commit()
            plan = create_plan(db, parsed_sources=sources, enforce_runtime_guards=False)
            self.assertEqual(plan.summary["unique_kiz_count"], EXPECTED_SCAN_COUNT)
            self.assertGreater(
                db.scalar(select(func.count()).select_from(KizMovement)),
                EXPECTED_SCAN_COUNT,
            )

            kiz = db.get(KizCode, movement.kiz_id)
            original_code = kiz.code
            kiz.code = "synthetic-kiz-registry-mismatch"
            db.commit()
            with self.assertRaisesRegex(RepairBlocked, "TARGET_SCAN_SCOPE_MISMATCH"):
                create_plan(db, parsed_sources=sources, enforce_runtime_guards=False)

            kiz.code = original_code
            missing = db.scalars(
                select(KizMovement)
                .where(KizMovement.kiz_id != movement.kiz_id)
                .order_by(KizMovement.id)
            ).first()
            db.delete(missing)
            db.commit()
            with self.assertRaisesRegex(RepairBlocked, "TARGET_SCAN_SCOPE_MISMATCH"):
                create_plan(db, parsed_sources=sources, enforce_runtime_guards=False)

    def test_download_parser_shim_preserves_source_bytes_sha_and_row_identity(self):
        from pathlib import Path

        import tools.repair_telegram_logistics_orders as repair_tool

        source_bytes = b"synthetic-workbook-bytes"
        source_sha = hashlib.sha256(source_bytes).hexdigest()
        source_import_id = "synthetic-source-identity"
        aliases = {"coordinates": ["Координаты"]}
        observed_bytes = []

        class FakeTelegramApiClient:
            def __init__(self, *_args, **_kwargs):
                pass

            def download_file(self, _file_id, path, _max_size):
                Path(path).write_bytes(source_bytes)

        def parse_workbook(path, **_kwargs):
            self.assertIn("GPS-координаты клиента", aliases["coordinates"])
            observed_bytes.append(Path(path).read_bytes())
            return {
                "sha256": source_sha,
                "rows": [{"ID импорта": source_import_id}],
            }

        modules = {
            "telegram_clients": SimpleNamespace(TelegramApiClient=FakeTelegramApiClient),
            "excel_importer": SimpleNamespace(
                HEADER_ALIASES=aliases,
                excel_file_to_import_payload=parse_workbook,
            ),
            "telegram_common": SimpleNamespace(parse_date_from_text=lambda _value: TARGET_DATE),
        }
        contexts = {
            EVENT_ID: {
                "file_id": "synthetic-file-id",
                "file_name": "synthetic.xlsx",
                "shipment_date": "22.07.2026",
                "expected_sha256": source_sha,
            },
        }
        with (
            mock.patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "synthetic-token"}),
            mock.patch.object(repair_tool, "backend_module", side_effect=modules.__getitem__),
        ):
            parsed = repair_tool.download_and_parse_sources(contexts)

        self.assertEqual(observed_bytes, [source_bytes])
        self.assertEqual(parsed[EVENT_ID].sha256, source_sha)
        self.assertEqual(parsed[EVENT_ID].rows[0]["ID импорта"], source_import_id)

    def test_active_skladbot_create_event_blocks_plan(self):
        with self.Session() as db:
            sources = self.seed_scope(db)
            order = db.scalars(select(Order).order_by(Order.id)).first()
            db.add(PendingEvent(
                event_type="skladbot_request_create",
                action="skladbot_request_create",
                aggregate_type="order",
                aggregate_id=str(order.id),
                status="processing",
                attempts=1,
                payload={"synthetic": True},
            ))
            db.commit()
            with self.assertRaisesRegex(RepairBlocked, "SKLADBOT_CREATE_EVENT_ACTIVE"):
                create_plan(db, parsed_sources=sources, enforce_runtime_guards=False)

    def test_preimage_file_is_exclusive_mode_0600_and_hash_bound(self):
        import tempfile
        from pathlib import Path

        with self.Session() as db, tempfile.TemporaryDirectory() as temp_dir:
            sources = self.seed_scope(db)
            plan = create_plan(db, parsed_sources=sources, enforce_runtime_guards=False)
            path = Path(temp_dir) / "preimage.json"
            digest = write_preimage_file(plan, path)
            self.assertEqual(digest, plan.summary["preimage_sha256"])
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(len(path.read_bytes()), path.stat().st_size)
            validated = validate_preimage_file(
                path,
                plan.summary["preimage_sha256"],
                plan.summary["plan_sha256"],
            )
            self.assertEqual(validated, plan.preimage)
            with self.assertRaises(FileExistsError):
                write_preimage_file(plan, path)
            path.chmod(0o640)
            with self.assertRaisesRegex(RepairBlocked, "PREIMAGE_MODE_INVALID"):
                validate_preimage_file(
                    path,
                    plan.summary["preimage_sha256"],
                    plan.summary["plan_sha256"],
                )

    def test_stale_plan_does_not_materialize_preimage(self):
        import tempfile
        from pathlib import Path

        with self.Session() as db, tempfile.TemporaryDirectory() as temp_dir:
            sources = self.seed_scope(db)
            plan = create_plan(db, parsed_sources=sources, enforce_runtime_guards=False)
            order = db.scalars(select(Order).order_by(Order.id)).first()
            order.address = "Synthetic drift"
            db.commit()
            path = Path(temp_dir) / "must-not-exist.json"
            with self.assertRaisesRegex(RepairBlocked, "PLAN_CHANGED_UNDER_LOCK"):
                apply_plan(db, plan, preimage_out=path)
            self.assertFalse(path.exists())

    def test_cli_apply_drift_blocks_before_preimage_materialization(self):
        import tempfile
        from pathlib import Path

        import tools.repair_telegram_logistics_orders as repair_tool

        class SessionProxy:
            def __init__(self, session):
                self.session = session

            def __enter__(self):
                return self

            def __exit__(self, _exc_type, _exc, _traceback):
                return False

            def __getattr__(self, name):
                return getattr(self.session, name)

            def execute(self, statement, *args, **kwargs):
                sql = str(statement)
                if sql.startswith("SET LOCAL") or "pg_advisory_xact_lock" in sql:
                    return SimpleNamespace()
                return self.session.execute(statement, *args, **kwargs)

        with self.Session() as db, tempfile.TemporaryDirectory() as temp_dir:
            sources = self.seed_scope(db)
            expected = create_plan(db, parsed_sources=sources, enforce_runtime_guards=False)
            destination = Path(temp_dir) / "must-not-exist.json"
            proxy = SessionProxy(db)
            original_backend_module = repair_tool.backend_module
            original_create_plan = repair_tool.create_plan
            first_call = True

            def fake_backend_module(name):
                if name == "db":
                    return SimpleNamespace(SessionLocal=lambda: proxy)
                return original_backend_module(name)

            def fake_create_plan(session, *, parsed_sources=None, enforce_runtime_guards=True):
                nonlocal first_call
                if first_call:
                    first_call = False
                    plan = original_create_plan(
                        session,
                        parsed_sources=sources,
                        enforce_runtime_guards=False,
                    )
                    order = session.scalars(select(Order).order_by(Order.id)).first()
                    order.address = "Synthetic concurrent drift"
                    session.flush()
                    return plan
                return original_create_plan(
                    session,
                    parsed_sources=parsed_sources,
                    enforce_runtime_guards=False,
                )

            with (
                mock.patch.object(repair_tool, "backend_module", side_effect=fake_backend_module),
                mock.patch.object(repair_tool, "create_plan", side_effect=fake_create_plan),
            ):
                with self.assertRaisesRegex(RepairBlocked, "PLAN_CHANGED_UNDER_LOCK"):
                    run([
                        "--apply",
                        "--approval", APPLY_APPROVAL,
                        "--expected-plan-sha", expected.summary["plan_sha256"],
                        "--preimage-out", str(destination),
                    ])
            self.assertFalse(destination.exists())

    def test_apply_verify_and_compensating_rollback_preserve_warehouse_rows(self):
        with self.Session() as db:
            sources = self.seed_scope(db)
            before = {
                "items": db.scalar(select(func.count()).select_from(OrderItem)),
                "scans": db.scalar(select(func.count()).select_from(ScanCode)),
                "kiz": db.scalar(select(func.count()).select_from(KizCode)),
                "movements": db.scalar(select(func.count()).select_from(KizMovement)),
            }
            plan = create_plan(db, parsed_sources=sources, enforce_runtime_guards=False)
            result = apply_plan(db, plan)

            self.assertEqual(result["mutations_applied"], 62)
            self.assertEqual(result["target_audits"], 62)
            verified = verify_applied(db, plan.summary["plan_sha256"], no_send=True)
            self.assertTrue(verified["safe_to_repair"], verified)
            self.assertEqual(verified["verified_count"], 62)
            self.assertEqual(verified["report_rows"], 230)
            self.assertEqual(verified["problem_rows"], 0)
            self.assertTrue(verified["no_send_unchanged"])
            self.assertEqual(
                db.scalar(select(func.count()).select_from(Order).where(Order.order_date == TARGET_DATE)),
                62,
            )
            self.assertEqual(before["items"], db.scalar(select(func.count()).select_from(OrderItem)))
            self.assertEqual(before["scans"], db.scalar(select(func.count()).select_from(ScanCode)))
            self.assertEqual(before["kiz"], db.scalar(select(func.count()).select_from(KizCode)))
            self.assertEqual(before["movements"], db.scalar(select(func.count()).select_from(KizMovement)))

            with self.assertRaisesRegex(RepairBlocked, "REPAIR_ALREADY_APPLIED"):
                apply_plan(db, plan)
            db.rollback()

            rollback = rollback_applied(
                db,
                plan.summary["plan_sha256"],
                approved_preimage=copy.deepcopy(plan.preimage),
                expected_preimage_sha=plan.summary["preimage_sha256"],
            )
            self.assertEqual(rollback["rollback_count"], 62)
            self.assertEqual(rollback["rollback_audits"], 62)
            self.assertEqual(
                db.scalar(select(func.count()).select_from(Order).where(Order.order_date == date(2026, 7, 22))),
                62,
            )
            self.assertEqual(before["items"], db.scalar(select(func.count()).select_from(OrderItem)))
            self.assertEqual(before["scans"], db.scalar(select(func.count()).select_from(ScanCode)))
            self.assertEqual(before["kiz"], db.scalar(select(func.count()).select_from(KizCode)))
            self.assertEqual(before["movements"], db.scalar(select(func.count()).select_from(KizMovement)))

    def test_verify_detects_no_send_or_immutable_drift(self):
        with self.Session() as db:
            sources = self.seed_scope(db)
            plan = create_plan(db, parsed_sources=sources, enforce_runtime_guards=False)
            apply_plan(db, plan)
            db.add(PendingEvent(
                event_type="smartup_logistics_report",
                status="processing",
                attempts=1,
                payload={"delivery_date": TARGET_DATE.isoformat()},
            ))
            db.commit()
            verified = verify_applied(db, plan.summary["plan_sha256"], no_send=True)
            self.assertFalse(verified["safe_to_repair"])
            self.assertFalse(verified["no_send_unchanged"])
            with self.assertRaisesRegex(RepairBlocked, "ROLLBACK_GUARD_FAILED"):
                rollback_applied(
                    db,
                    plan.summary["plan_sha256"],
                    approved_preimage=copy.deepcopy(plan.preimage),
                    expected_preimage_sha=plan.summary["preimage_sha256"],
                )

    def test_tampered_repair_audit_cannot_drive_rollback(self):
        with self.Session() as db:
            sources = self.seed_scope(db)
            plan = create_plan(db, parsed_sources=sources, enforce_runtime_guards=False)
            apply_plan(db, plan)
            audit = db.scalars(
                select(AuditLog)
                .where(AuditLog.action == "telegram_logistics_order_repaired")
                .order_by(AuditLog.id)
            ).first()
            payload = copy.deepcopy(audit.payload)
            payload["preimage"]["address"] = "Synthetic tampered rollback address"
            audit.payload = payload
            db.commit()

            with self.assertRaisesRegex(RepairBlocked, "ROLLBACK_AUDIT_MISMATCH"):
                rollback_applied(
                    db,
                    plan.summary["plan_sha256"],
                    approved_preimage=copy.deepcopy(plan.preimage),
                    expected_preimage_sha=plan.summary["preimage_sha256"],
                )
            self.assertEqual(
                db.scalar(select(func.count()).select_from(Order).where(Order.order_date == TARGET_DATE)),
                EXPECTED_TARGET_COUNT,
            )
            self.assertEqual(
                db.scalar(
                    select(func.count())
                    .select_from(AuditLog)
                    .where(AuditLog.action == "telegram_logistics_order_repair_rolled_back")
                ),
                0,
            )

    def test_audit_payload_keeps_exact_preimage_but_result_is_counts_only(self):
        with self.Session() as db:
            sources = self.seed_scope(db)
            plan = create_plan(db, parsed_sources=sources, enforce_runtime_guards=False)
            result = apply_plan(db, plan)
            audit = db.scalars(
                select(AuditLog)
                .where(AuditLog.action == "telegram_logistics_order_repaired")
                .order_by(AuditLog.id)
            ).first()
            self.assertIn("preimage", audit.payload)
            self.assertIn("applied", audit.payload)
            self.assertEqual(audit.payload["applied"]["order_date"], "2026-07-23")
            rendered_result = str(result)
            self.assertNotIn("Synthetic Client", rendered_result)
            self.assertNotIn("Synthetic address", rendered_result)


if __name__ == "__main__":
    unittest.main()
