import copy
import unittest
from datetime import date

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.models import Base, PendingEvent
from backend.app.telegram_daily_report_policy import (
    SKLADBOT_DAILY_REPORT_NO_REQUESTS_RESULT,
)
from backend.app.telegram_scheduled_report_processor import (
    SKLADBOT_DAILY_REPORTED_REQUEST_EVENT_TYPE,
    TelegramScheduledReportProcessor,
)


def sqlite_session_factory():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(autocommit=False, autoflush=False, bind=engine)


def complete_report(*, requests=None, excluded=0):
    requests = list(requests or [])
    return {
        "report_date": date(2026, 7, 21),
        "requests": requests,
        "excluded_requests": [{"request_id": index + 1} for index in range(excluded)],
        "errors": [],
        "coverage": {
            "coverage_status": "complete",
            "included_operational_requests": len(requests),
            "excluded_diagnostic_requests": excluded,
        },
        "summary": {
            "requests_total": len(requests),
            "category_counts": {},
            "request_blocks_by_category": {},
            "stock_total": 0,
        },
    }


class FakeReportModule:
    def __init__(self, report, timeline, hydrate):
        self.report = report
        self.timeline = timeline
        self.hydrate = hydrate

    def collect_skladbot_daily_report(self, report_date=None):
        self.timeline.append("collect")
        result = copy.deepcopy(self.report)
        result["report_date"] = report_date
        return result

    def enrich_smartup_ids_from_orders(self, db, report):
        self.timeline.append("smartup")

    def enrich_daily_kiz_from_orders(self, db, report):
        self.timeline.append("hydrate_kiz")
        self.hydrate(report)

    def build_skladbot_daily_report_xlsx(self, report):
        self.timeline.append("build_xlsx")
        if "request_kiz_rows" not in report or "daily_kiz_rows" not in report:
            raise AssertionError("combined KIZ rows must be hydrated before XLSX build")
        return b"combined-xlsx", "TakSklad_SkladBot_daily_21.07.2026.xlsx"

    def build_skladbot_daily_report_message(self, report):
        self.timeline.append("build_message")
        return "combined daily"


def scheduled_processor(report_module, session_factory):
    processor = TelegramScheduledReportProcessor.__new__(TelegramScheduledReportProcessor)
    processor.skladbot_report_module = report_module
    processor.session_factory = session_factory
    return processor


class TelegramScheduledDailyCombinedTests(unittest.TestCase):
    def test_sends_one_message_and_one_combined_document_then_marks_delivery_mode(self):
        timeline = []
        report = complete_report(requests=[{
            "id": 404,
            "number": "WH-R-404",
            "category": "Приемка",
            "include_reasons": ["Дата выгрузки"],
        }])

        def hydrate(combined_report):
            combined_report["requests"][0]["kiz_count"] = 2
            combined_report["request_kiz_rows"] = [{"kiz": "synthetic-1"}, {"kiz": "synthetic-2"}]
            combined_report["daily_kiz_rows"] = [{"kiz": "synthetic-2"}]

        factory = sqlite_session_factory()
        processor = scheduled_processor(FakeReportModule(report, timeline, hydrate), factory)
        messages = []
        documents = []
        processor.send_message = lambda chat_id, text, reply_markup=None: (
            timeline.append("send_message"),
            messages.append((chat_id, text)),
            {"message_id": 1},
        )[-1]
        processor.send_document = lambda chat_id, content, filename, caption="": (
            timeline.append("send_document"),
            documents.append((chat_id, content, filename, caption)),
            {"message_id": 2},
        )[-1]

        result = processor.send_skladbot_daily_report(
            "synthetic-client",
            report_date=date(2026, 7, 21),
            scheduled=True,
            delivery_mode="manual_catchup",
        )

        self.assertTrue(result)
        self.assertEqual(len(messages), 1)
        self.assertEqual(len(documents), 1)
        self.assertEqual(documents[0][2], "TakSklad_SkladBot_daily_21.07.2026.xlsx")
        self.assertLess(timeline.index("hydrate_kiz"), timeline.index("build_xlsx"))
        self.assertLess(timeline.index("build_xlsx"), timeline.index("send_message"))
        self.assertLess(timeline.index("send_message"), timeline.index("send_document"))

        with factory() as db:
            events = db.execute(
                select(PendingEvent).where(
                    PendingEvent.event_type == SKLADBOT_DAILY_REPORTED_REQUEST_EVENT_TYPE
                )
            ).scalars().all()
        self.assertEqual(len(events), 1)
        self.assertEqual((events[0].payload or {}).get("mode"), "manual_catchup")
        self.assertIn("manual_catchup", events[0].idempotency_key)

    def test_kiz_hydration_failure_happens_before_any_telegram_call(self):
        timeline = []
        report = complete_report(requests=[{"id": 404, "number": "WH-R-404"}])

        def fail_hydration(_report):
            raise RuntimeError("synthetic hydration failure")

        processor = scheduled_processor(
            FakeReportModule(report, timeline, fail_hydration),
            sqlite_session_factory(),
        )
        telegram_calls = []
        processor.send_message = lambda *args, **kwargs: telegram_calls.append((args, kwargs))
        processor.send_document = lambda *args, **kwargs: telegram_calls.append((args, kwargs))

        with self.assertRaisesRegex(RuntimeError, "synthetic hydration failure"):
            processor.send_skladbot_daily_report(
                "synthetic-client",
                report_date=date(2026, 7, 21),
                scheduled=True,
            )

        self.assertEqual(telegram_calls, [])
        self.assertNotIn("build_xlsx", timeline)

    def test_day_kiz_only_report_completes_without_client_send(self):
        timeline = []
        progress = []
        report = complete_report(requests=[], excluded=1)

        def hydrate(combined_report):
            combined_report["request_kiz_rows"] = []
            combined_report["daily_kiz_rows"] = [{"kiz": "synthetic-day-only"}]

        processor = scheduled_processor(
            FakeReportModule(report, timeline, hydrate),
            sqlite_session_factory(),
        )
        telegram_calls = []
        processor.send_message = lambda *args, **kwargs: telegram_calls.append((args, kwargs))
        processor.send_document = lambda *args, **kwargs: telegram_calls.append((args, kwargs))

        result = processor.send_skladbot_daily_report(
            "synthetic-client",
            report_date=date(2026, 7, 21),
            scheduled=True,
            progress=lambda stage, **fields: progress.append((stage, fields)),
        )

        self.assertEqual(result, SKLADBOT_DAILY_REPORT_NO_REQUESTS_RESULT)
        self.assertEqual(telegram_calls, [])
        self.assertNotIn("build_xlsx", timeline)
        no_requests = next(fields for stage, fields in progress if stage == "scheduled job no requests")
        self.assertTrue(no_requests["combined_empty"])
        self.assertEqual(no_requests["requests_count"], 0)
        self.assertEqual(no_requests["order_kiz_count"], 0)
        self.assertEqual(no_requests["day_kiz_count"], 1)

    def test_no_requests_and_no_day_kiz_completes_without_send_or_build(self):
        timeline = []
        progress = []
        report = complete_report(requests=[])

        def hydrate(combined_report):
            combined_report["request_kiz_rows"] = []
            combined_report["daily_kiz_rows"] = []

        processor = scheduled_processor(
            FakeReportModule(report, timeline, hydrate),
            sqlite_session_factory(),
        )
        telegram_calls = []
        processor.send_message = lambda *args, **kwargs: telegram_calls.append((args, kwargs))
        processor.send_document = lambda *args, **kwargs: telegram_calls.append((args, kwargs))

        result = processor.send_skladbot_daily_report(
            "synthetic-client",
            report_date=date(2026, 7, 21),
            scheduled=True,
            progress=lambda stage, **fields: progress.append((stage, fields)),
        )

        self.assertEqual(result, SKLADBOT_DAILY_REPORT_NO_REQUESTS_RESULT)
        self.assertEqual(telegram_calls, [])
        self.assertNotIn("build_xlsx", timeline)
        no_requests = next(fields for stage, fields in progress if stage == "scheduled job no requests")
        self.assertTrue(no_requests["combined_empty"])
        self.assertEqual(no_requests["requests_count"], 0)
        self.assertEqual(no_requests["order_kiz_count"], 0)
        self.assertEqual(no_requests["day_kiz_count"], 0)

    def test_true_empty_dry_run_still_builds_combined_workbook(self):
        timeline = []
        report = complete_report(requests=[])

        def hydrate(combined_report):
            combined_report["request_kiz_rows"] = []
            combined_report["daily_kiz_rows"] = []

        processor = scheduled_processor(
            FakeReportModule(report, timeline, hydrate),
            sqlite_session_factory(),
        )
        processor.send_message = lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("dry-run must not call Telegram")
        )
        processor.send_document = lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("dry-run must not call Telegram")
        )

        prepared = processor.prepare_skladbot_daily_report(
            date(2026, 7, 21),
            scheduled=True,
            build_for_dry_run=True,
        )

        self.assertEqual(prepared["result_status"], SKLADBOT_DAILY_REPORT_NO_REQUESTS_RESULT)
        self.assertTrue(prepared["combined_empty"])
        self.assertEqual(prepared["content"], b"combined-xlsx")
        self.assertEqual(prepared["filename"], "TakSklad_SkladBot_daily_21.07.2026.xlsx")
        self.assertIn("build_xlsx", timeline)

    def test_registry_is_not_written_when_document_delivery_is_not_confirmed(self):
        timeline = []
        report = complete_report(requests=[{
            "id": 404,
            "number": "WH-R-404",
            "category": "Приемка",
        }])

        def hydrate(combined_report):
            combined_report["request_kiz_rows"] = []
            combined_report["daily_kiz_rows"] = []

        factory = sqlite_session_factory()
        processor = scheduled_processor(FakeReportModule(report, timeline, hydrate), factory)
        processor.send_message = lambda *args, **kwargs: {"message_id": 1}
        processor.send_document = lambda *args, **kwargs: None

        result = processor.send_skladbot_daily_report(
            "synthetic-client",
            report_date=date(2026, 7, 21),
            scheduled=True,
        )

        self.assertFalse(result)
        with factory() as db:
            events = db.execute(
                select(PendingEvent).where(
                    PendingEvent.event_type == SKLADBOT_DAILY_REPORTED_REQUEST_EVENT_TYPE
                )
            ).scalars().all()
        self.assertEqual(events, [])


if __name__ == "__main__":
    unittest.main()
