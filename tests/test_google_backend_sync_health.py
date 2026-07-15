import unittest
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.health_service import build_google_backend_sync_readiness
from backend.app.models import AuditLog, Base


class GoogleBackendSyncHealthTests(unittest.TestCase):
    def setUp(self):
        engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(engine)
        self.SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

    def test_lock_capacity_circuit_remains_degraded_until_success_closes_it(self):
        now = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
        with self.SessionLocal() as db:
            db.add(AuditLog(
                action="google_sheets_backend_sync_circuit_open",
                entity_type="google_sheets",
                entity_id="data",
                payload={
                    "reason": "postgres_lock_capacity",
                    "cooldown_seconds": 900,
                    "opened_at": now.isoformat(),
                },
                created_at=now,
            ))
            db.commit()

            active = build_google_backend_sync_readiness(db, now=now + timedelta(minutes=5))
            half_open = build_google_backend_sync_readiness(db, now=now + timedelta(minutes=16))
            db.add(AuditLog(
                action="google_sheets_backend_sync_circuit_closed",
                entity_type="google_sheets",
                entity_id="data",
                payload={"closed_at": (now + timedelta(minutes=17)).isoformat()},
                created_at=now + timedelta(minutes=17),
            ))
            db.commit()
            closed = build_google_backend_sync_readiness(db, now=now + timedelta(minutes=18))

        self.assertEqual(active["status"], "degraded")
        self.assertTrue(active["circuit_open"])
        self.assertEqual(active["reason"], "postgres_lock_capacity")
        self.assertEqual(half_open["status"], "degraded")
        self.assertTrue(half_open["circuit_open"])
        self.assertEqual(half_open["circuit_state"], "half_open")
        self.assertEqual(closed["status"], "ok")
        self.assertFalse(closed["circuit_open"])


if __name__ == "__main__":
    unittest.main()
