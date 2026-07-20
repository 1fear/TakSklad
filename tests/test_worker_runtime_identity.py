from datetime import datetime, timedelta, timezone
import json
import unittest

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.auth_identities import (
    SERVICE_PRINCIPAL_SCOPE_MATRIX,
    IdentityAuthError,
    authenticate_service_token,
)
from backend.app.models import AuditLog, Base, ServicePrincipal, ServicePrincipalToken
from backend.app.worker_runtime_identity import (
    TELEGRAM_WORKER_IDENTIFIER,
    TELEGRAM_WORKER_ROTATION_OVERLAP_SECONDS,
    WorkerRuntimeIdentityError,
    issue_telegram_worker_runtime_token,
)


class WorkerRuntimeIdentityTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)

    def tearDown(self):
        self.engine.dispose()

    def test_provisions_exact_worker_scope_without_persisting_plaintext(self):
        now = datetime(2026, 7, 20, 6, 0, tzinfo=timezone.utc)
        token = issue_telegram_worker_runtime_token(self.Session, now=now)
        self.assertTrue(token.startswith("tks."))
        with self.Session() as db:
            principal = db.execute(select(ServicePrincipal)).scalar_one()
            self.assertEqual(principal.identifier, TELEGRAM_WORKER_IDENTIFIER)
            self.assertEqual(principal.kind, "worker")
            self.assertEqual(set(principal.scopes), set(SERVICE_PRINCIPAL_SCOPE_MATRIX["worker"]))
            verified = authenticate_service_token(
                db,
                token,
                required_scope="imports:create",
                now=now,
                touch_last_used=False,
            )
            self.assertEqual(verified.principal_identifier, TELEGRAM_WORKER_IDENTIFIER)
            audit = db.execute(select(AuditLog)).scalar_one()
            rendered = json.dumps(audit.payload, sort_keys=True)
            self.assertNotIn(token, rendered)
            self.assertNotIn("tks.", rendered)
            stored = db.execute(select(ServicePrincipalToken)).scalar_one()
            self.assertNotEqual(stored.token_digest, token)

    def test_restart_rotates_with_bounded_overlap(self):
        now = datetime(2026, 7, 20, 6, 0, tzinfo=timezone.utc)
        first = issue_telegram_worker_runtime_token(self.Session, now=now)
        second = issue_telegram_worker_runtime_token(self.Session, now=now + timedelta(seconds=5))
        self.assertNotEqual(first, second)
        with self.Session() as db:
            authenticate_service_token(
                db,
                first,
                now=now + timedelta(seconds=5 + TELEGRAM_WORKER_ROTATION_OVERLAP_SECONDS - 1),
                touch_last_used=False,
            )
            with self.assertRaises(IdentityAuthError):
                authenticate_service_token(
                    db,
                    first,
                    now=now + timedelta(seconds=5 + TELEGRAM_WORKER_ROTATION_OVERLAP_SECONDS),
                    touch_last_used=False,
                )
            authenticate_service_token(
                db,
                second,
                required_scope="imports:create",
                now=now + timedelta(days=1),
                touch_last_used=False,
            )

    def test_inactive_or_policy_mismatched_principal_is_not_reactivated(self):
        for kind, scopes, active in (
            ("worker", list(SERVICE_PRINCIPAL_SCOPE_MATRIX["worker"]), False),
            ("desktop", list(SERVICE_PRINCIPAL_SCOPE_MATRIX["desktop"]), True),
        ):
            with self.subTest(kind=kind, active=active):
                with self.Session() as db:
                    db.query(ServicePrincipalToken).delete()
                    db.query(AuditLog).delete()
                    db.query(ServicePrincipal).delete()
                    db.add(ServicePrincipal(
                        identifier=TELEGRAM_WORKER_IDENTIFIER,
                        kind=kind,
                        scopes=scopes,
                        is_active=active,
                    ))
                    db.commit()
                with self.assertRaises(WorkerRuntimeIdentityError):
                    issue_telegram_worker_runtime_token(self.Session)
