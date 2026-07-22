import json
import unittest
import uuid
from contextlib import redirect_stderr
from datetime import date
from io import StringIO

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.models import Base, PendingEvent
from backend.app.telegram_daily_report_policy import (
    SKLADBOT_DAILY_REPORT_NO_REQUESTS_RESULT,
    SKLADBOT_DAILY_REPORT_SEND_EVENT_TYPE,
)
from backend.app.telegram_manual_daily_catchup import (
    MANUAL_DAILY_CATCHUP_MODE,
    MANUAL_DAILY_CATCHUP_SUCCESS,
    MANUAL_DAILY_CATCHUP_VERSION,
    ManualDailyCatchupConfigurationError,
    configured_daily_chat_id,
    dry_run_manual_daily_catchup,
    main,
    parse_args,
    run_manual_daily_catchup,
)
from backend.app.telegram_scheduled_report_processor import TelegramScheduledReportProcessor


REPORT_DATE = date(2026, 7, 20)
CHAT_ID = "-100900"


def make_session_factory():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(autocommit=False, autoflush=False, bind=engine)


class FakeSender:
    def __init__(self, session_factory):
        self.session_factory = session_factory
        self.skladbot_daily_report_chat_ids = {CHAT_ID}
        self.send_message_count = 0
        self.send_document_count = 0
        self.send_calls = []
        self.prepare_calls = []
        self.reconciliation_calls = []
        self.raise_after_message_started = False
        self.result = True
        self.no_requests_combined_empty = True
        self.prepared = {
            "report": {
                "report_date": REPORT_DATE,
                "requests": [{"id": 11}],
                "request_kiz_rows": [{"code": "do-not-print-order-code"}],
                "daily_kiz_rows": [{"code": "do-not-print-day-code"}],
            },
            "report_date": REPORT_DATE,
            "content": b"synthetic-xlsx",
            "filename": "daily.xlsx",
            "message": "synthetic",
            "blocker": "",
        }

    def _scheduled_session_factory(self):
        return self.session_factory

    def skladbot_daily_report_idempotency_key(
        self,
        chat_id,
        report_date,
        mode="scheduled",
        report_kind="daily_skladbot",
        report_version="v2",
    ):
        return (
            f"skladbot_daily_report:{report_date.isoformat()}:{chat_id}:"
            f"{mode}:{report_kind}:{report_version}"
        )

    def update_scheduled_skladbot_daily_report_progress(self, event_id, stage, **fields):
        with self.session_factory() as db:
            event = db.get(PendingEvent, uuid.UUID(str(event_id)))
            payload = dict(event.payload or {})
            payload.update(fields)
            payload["stage"] = stage
            event.payload = payload
            db.commit()

    def send_skladbot_daily_report(
        self,
        chat_id,
        *,
        report_date,
        scheduled,
        progress,
        delivery_mode,
    ):
        self.send_calls.append({
            "chat_id": chat_id,
            "report_date": report_date,
            "scheduled": scheduled,
            "delivery_mode": delivery_mode,
        })
        progress(
            "report generation finished",
            requests_count=1,
            order_kiz_count=1,
            day_kiz_count=1,
        )
        progress("xlsx created", bytes=14)
        if self.result == SKLADBOT_DAILY_REPORT_NO_REQUESTS_RESULT:
            progress(
                "scheduled job no requests",
                combined_empty=self.no_requests_combined_empty,
                requests_count=0,
                order_kiz_count=0,
                day_kiz_count=0,
            )
            return self.result
        progress("telegram sendMessage started")
        self.send_message_count += 1
        if self.raise_after_message_started:
            raise RuntimeError("Bearer very-secret timeout")
        progress("telegram sendMessage success")
        progress("telegram sendDocument started")
        self.send_document_count += 1
        progress("telegram sendDocument success")
        progress("reported mark success", reported_count=1)
        return self.result

    def prepare_skladbot_daily_report(self, report_date=None, progress=None, **_kwargs):
        self.prepare_calls.append(report_date)
        if progress is not None:
            progress(
                "report generation finished",
                requests_count=len(self.prepared["report"].get("requests") or []),
            )
            progress("xlsx created", bytes=len(self.prepared["content"]))
        return self.prepared

    def run_scheduled_daily_reconciliation(self, chat_id, report_date):
        self.reconciliation_calls.append((chat_id, report_date))


def send_events(session_factory):
    with session_factory() as db:
        return db.execute(
            select(PendingEvent)
            .where(PendingEvent.event_type == SKLADBOT_DAILY_REPORT_SEND_EVENT_TYPE)
            .order_by(PendingEvent.created_at)
        ).scalars().all()


class FakeCombinedReportModule:
    def __init__(self, *, coverage_status="complete", empty=False):
        self.coverage_status = coverage_status
        self.empty = empty
        self.enriched = False
        self.built = False

    def collect_skladbot_daily_report(self, report_date=None):
        requests = [] if self.empty else [{"id": 11, "number": "WH-R-11"}]
        return {
            "report_date": report_date,
            "requests": requests,
            "excluded_requests": [],
            "errors": [],
            "coverage": {
                "coverage_status": self.coverage_status,
                "included_operational_requests": len(requests),
                "excluded_diagnostic_requests": 0,
            },
        }

    @staticmethod
    def enrich_smartup_ids_from_orders(_db, _report):
        return None

    def enrich_daily_kiz_from_orders(self, _db, report):
        self.enriched = True
        report["request_kiz_rows"] = [] if self.empty else [{"code": "hidden-order"}]
        report["daily_kiz_rows"] = [] if self.empty else [{"code": "hidden-day"}]

    def build_skladbot_daily_report_xlsx(self, _report):
        self.built = True
        return b"combined-xlsx", "daily.xlsx"

    @staticmethod
    def build_skladbot_daily_report_message(_report):
        return "synthetic message"


class ManualDailyCatchupTests(unittest.TestCase):
    def setUp(self):
        self.session_factory = make_session_factory()

    def test_success_is_sent_once_and_replay_is_noop(self):
        sender = FakeSender(self.session_factory)

        first = run_manual_daily_catchup(sender, CHAT_ID, REPORT_DATE)
        second = run_manual_daily_catchup(sender, CHAT_ID, REPORT_DATE)

        self.assertEqual(first["status"], MANUAL_DAILY_CATCHUP_SUCCESS)
        self.assertIs(first["sent"], True)
        self.assertEqual(second["status"], "already_completed")
        self.assertIs(second["sent"], False)
        self.assertEqual(sender.send_message_count, 1)
        self.assertEqual(sender.send_document_count, 1)
        self.assertEqual(sender.reconciliation_calls, [])
        self.assertEqual(sender.send_calls, [{
            "chat_id": CHAT_ID,
            "report_date": REPORT_DATE,
            "scheduled": True,
            "delivery_mode": MANUAL_DAILY_CATCHUP_MODE,
        }])
        event = send_events(self.session_factory)[0]
        self.assertEqual(event.status, "completed")
        self.assertEqual(event.payload["result_status"], MANUAL_DAILY_CATCHUP_SUCCESS)
        self.assertEqual(event.payload["sendMessage_count"], 1)
        self.assertEqual(event.payload["sendDocument_count"], 1)
        self.assertIs(event.payload["reconciliation_started"], False)

    def test_reported_registry_precheck_suppresses_send(self):
        with self.session_factory() as db:
            db.add(PendingEvent(
                event_type="skladbot_daily_reported_request",
                idempotency_key=(
                    f"skladbot_daily_reported_request:{REPORT_DATE.isoformat()}:"
                    f"{CHAT_ID}:scheduled:daily_skladbot:abc:11"
                ),
                status="completed",
                attempts=1,
                payload={"report_date": REPORT_DATE.isoformat()},
            ))
            db.commit()
        sender = FakeSender(self.session_factory)

        result = run_manual_daily_catchup(sender, CHAT_ID, REPORT_DATE)

        self.assertEqual(result["status"], "already_reported")
        self.assertIs(result["sent"], False)
        self.assertEqual(sender.send_calls, [])
        self.assertEqual(send_events(self.session_factory), [])

    def test_post_send_ambiguity_blocks_replay(self):
        for status in ("failed", "blocked"):
            with self.subTest(status=status):
                session_factory = make_session_factory()
                with session_factory() as db:
                    db.add(PendingEvent(
                        event_type=SKLADBOT_DAILY_REPORT_SEND_EVENT_TYPE,
                        idempotency_key=(
                            f"skladbot_daily_report:{REPORT_DATE.isoformat()}:{CHAT_ID}:"
                            "scheduled:daily_skladbot:v2"
                        ),
                        status=status,
                        attempts=1,
                        payload={
                            "report_date": REPORT_DATE.isoformat(),
                            "stage": "telegram sendDocument started",
                        },
                    ))
                    db.commit()
                sender = FakeSender(session_factory)

                result = run_manual_daily_catchup(sender, CHAT_ID, REPORT_DATE)

                self.assertEqual(result["status"], "ambiguous_delivery_exists")
                self.assertIs(result["sent"], False)
                self.assertEqual(sender.send_calls, [])

    def test_old_pre_telegram_scheduled_failure_allows_catchup(self):
        with self.session_factory() as db:
            db.add(PendingEvent(
                event_type=SKLADBOT_DAILY_REPORT_SEND_EVENT_TYPE,
                idempotency_key=(
                    f"skladbot_daily_report:{REPORT_DATE.isoformat()}:{CHAT_ID}:"
                    "scheduled:daily_skladbot:v2"
                ),
                status="failed",
                attempts=3,
                payload={
                    "report_date": REPORT_DATE.isoformat(),
                    "stage": "scheduled job failed",
                },
            ))
            db.commit()
        sender = FakeSender(self.session_factory)

        result = run_manual_daily_catchup(sender, CHAT_ID, REPORT_DATE)

        self.assertEqual(result["status"], MANUAL_DAILY_CATCHUP_SUCCESS)
        self.assertEqual(sender.send_message_count, 1)
        self.assertEqual(sender.send_document_count, 1)

    def test_failure_after_send_started_blocks_and_replay_sends_nothing(self):
        sender = FakeSender(self.session_factory)
        sender.raise_after_message_started = True

        first = run_manual_daily_catchup(sender, CHAT_ID, REPORT_DATE)
        sender.raise_after_message_started = False
        second = run_manual_daily_catchup(sender, CHAT_ID, REPORT_DATE)

        self.assertEqual(first["status"], "manual_recovery_required")
        self.assertEqual(second["status"], "ambiguous_delivery_exists")
        self.assertEqual(sender.send_message_count, 1)
        self.assertEqual(sender.send_document_count, 0)
        self.assertEqual(len(sender.send_calls), 1)
        event = send_events(self.session_factory)[0]
        self.assertEqual(event.status, "blocked")
        self.assertEqual(event.payload["origin_stage"], "telegram sendMessage started")
        self.assertNotIn("very-secret", event.last_error or "")
        self.assertIs(event.payload["reconciliation_started"], False)

    def test_no_requests_requires_explicit_combined_empty_proof(self):
        sender = FakeSender(self.session_factory)
        sender.result = SKLADBOT_DAILY_REPORT_NO_REQUESTS_RESULT
        sender.no_requests_combined_empty = False

        result = run_manual_daily_catchup(sender, CHAT_ID, REPORT_DATE)

        self.assertEqual(result["status"], "manual_recovery_required")
        self.assertIs(result["sent"], False)
        self.assertEqual(send_events(self.session_factory)[0].status, "blocked")

    def test_dry_run_builds_full_report_without_db_write_or_send(self):
        sender = FakeSender(self.session_factory)

        result = dry_run_manual_daily_catchup(sender, REPORT_DATE)

        self.assertEqual(result, {
            "status": "ready",
            "report_date": REPORT_DATE.isoformat(),
            "requests_count": 1,
            "order_kiz_count": 1,
            "day_kiz_count": 1,
            "xlsx_bytes": len(b"synthetic-xlsx"),
        })
        self.assertEqual(sender.prepare_calls, [REPORT_DATE])
        self.assertEqual(sender.send_calls, [])
        self.assertEqual(sender.send_message_count, 0)
        self.assertEqual(sender.send_document_count, 0)
        with self.session_factory() as db:
            self.assertEqual(db.execute(select(PendingEvent)).scalars().all(), [])

    def test_dry_run_integrates_with_real_prepare_for_ready_blocked_and_empty(self):
        cases = (
            ("complete", False, "ready", 1, 1, 1),
            ("partial", False, "blocked", 1, 1, 1),
            (
                "complete",
                True,
                SKLADBOT_DAILY_REPORT_NO_REQUESTS_RESULT,
                0,
                0,
                0,
            ),
        )
        for coverage_status, empty, expected_status, requests, order_kiz, day_kiz in cases:
            with self.subTest(coverage_status=coverage_status, empty=empty):
                session_factory = make_session_factory()
                report_module = FakeCombinedReportModule(
                    coverage_status=coverage_status,
                    empty=empty,
                )
                sender = TelegramScheduledReportProcessor(
                    session_factory=session_factory,
                    skladbot_report_module=report_module,
                )

                result = dry_run_manual_daily_catchup(sender, REPORT_DATE)

                self.assertEqual(result["status"], expected_status)
                self.assertEqual(result["requests_count"], requests)
                self.assertEqual(result["order_kiz_count"], order_kiz)
                self.assertEqual(result["day_kiz_count"], day_kiz)
                self.assertEqual(result["xlsx_bytes"], len(b"combined-xlsx"))
                self.assertIs(report_module.enriched, True)
                self.assertIs(report_module.built, True)
                with session_factory() as db:
                    self.assertEqual(db.execute(select(PendingEvent)).scalars().all(), [])

    def test_exactly_one_configured_daily_chat_is_required(self):
        sender = FakeSender(self.session_factory)
        self.assertEqual(configured_daily_chat_id(sender), CHAT_ID)

        sender.skladbot_daily_report_chat_ids = set()
        with self.assertRaises(ManualDailyCatchupConfigurationError):
            configured_daily_chat_id(sender)

        sender.skladbot_daily_report_chat_ids = {CHAT_ID, "-100901"}
        with self.assertRaises(ManualDailyCatchupConfigurationError):
            configured_daily_chat_id(sender)

    def test_cli_modes_are_mutually_exclusive_and_report_date_is_required(self):
        with redirect_stderr(StringIO()):
            with self.assertRaises(SystemExit):
                parse_args(["--dry-run"])
            with self.assertRaises(SystemExit):
                parse_args([
                    "--report-date", REPORT_DATE.isoformat(), "--dry-run", "--execute",
                ])

    def test_cli_output_is_whitelisted_and_contains_no_chat_or_kiz(self):
        sender = FakeSender(self.session_factory)
        output = StringIO()

        exit_code = main(
            ["--report-date", REPORT_DATE.isoformat(), "--dry-run"],
            worker_factory=lambda: sender,
            output=output,
        )

        payload = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(set(payload), {
            "status",
            "report_date",
            "requests_count",
            "order_kiz_count",
            "day_kiz_count",
            "xlsx_bytes",
        })
        rendered = output.getvalue()
        self.assertNotIn(CHAT_ID, rendered)
        self.assertNotIn("do-not-print-order-code", rendered)
        self.assertNotIn("do-not-print-day-code", rendered)

    def test_manual_catchup_contract_version_is_combined_kiz_v4(self):
        self.assertEqual(MANUAL_DAILY_CATCHUP_VERSION, "v4-combined-kiz")


if __name__ == "__main__":
    unittest.main()
