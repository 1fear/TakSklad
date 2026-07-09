import unittest
from datetime import datetime, timedelta, timezone

from sqlalchemy.dialects import postgresql

from backend.app.event_leases import build_postgres_claim_statement, event_leases_enabled


class EventLeaseContractTests(unittest.TestCase):
    def test_canary_flag_is_fail_closed_by_default(self):
        self.assertFalse(event_leases_enabled({}))
        self.assertTrue(event_leases_enabled({"TAKSKLAD_EVENT_LEASES_ENABLED": "1"}))
        self.assertFalse(event_leases_enabled({"TAKSKLAD_EVENT_LEASES_ENABLED": "0"}))

    def test_postgres_claim_is_one_update_returning_over_locked_candidates(self):
        now = datetime(2026, 7, 10, 0, 0, tzinfo=timezone.utc)
        statement = build_postgres_claim_statement(
            event_types=("google_sheets_export",),
            owner="worker-1",
            limit=50,
            now=now,
            expires_at=now + timedelta(minutes=30),
        )

        sql = str(statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )).upper()

        self.assertIn("FOR UPDATE SKIP LOCKED", sql)
        self.assertIn("UPDATE PENDING_EVENTS", sql)
        self.assertIn("RETURNING", sql)
        self.assertIn("AVAILABLE_AT", sql)
        self.assertIn("LEASE_EXPIRES_AT", sql)
        self.assertNotIn("PAYLOAD ->", sql)


if __name__ == "__main__":
    unittest.main()
