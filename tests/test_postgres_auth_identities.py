import hashlib
import os
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

from backend.app.auth_identities import issue_service_token, rotate_service_token
from backend.app.models import ServicePrincipal, ServicePrincipalToken
from tests.postgres_support import create_database, drop_database, run_alembic, scalar


POSTGRES_AVAILABLE = bool(os.environ.get("TAKSKLAD_TEST_DATABASE_URL"))
PREVIOUS_HEAD = "20260710_0011"
CURRENT_HEAD = "20260719_0020"


@unittest.skipUnless(POSTGRES_AVAILABLE, "disposable PostgreSQL URL not provided")
class PostgresAuthIdentityTests(unittest.TestCase):
    databases = (
        "taksklad_auth_identities_empty",
        "taksklad_auth_identities_populated",
        "taksklad_auth_identities_digest",
        "taksklad_auth_identities_rotation",
    )

    @classmethod
    def tearDownClass(cls):
        if not POSTGRES_AVAILABLE:
            return
        for name in cls.databases:
            drop_database(name)

    def test_empty_database_has_additive_identity_schema(self):
        url = create_database(self.databases[0])

        run_alembic(url, "upgrade", "head")

        self.assertEqual(scalar(url, "SELECT version_num FROM alembic_version"), CURRENT_HEAD)
        engine = create_engine(url)
        try:
            inspector = inspect(engine)
            tables = set(inspector.get_table_names())
            user_columns = {column["name"] for column in inspector.get_columns("users")}
            session_columns = {column["name"]: column for column in inspector.get_columns("auth_sessions")}
            principal_columns = {column["name"] for column in inspector.get_columns("service_principals")}
            token_columns = {column["name"] for column in inspector.get_columns("service_principal_tokens")}
            audit_columns = {column["name"] for column in inspector.get_columns("audit_log")}
            session_indexes = {index["name"] for index in inspector.get_indexes("auth_sessions")}
            principal_indexes = {index["name"] for index in inspector.get_indexes("service_principals")}
            token_indexes = {index["name"] for index in inspector.get_indexes("service_principal_tokens")}
        finally:
            engine.dispose()

        self.assertTrue({"auth_sessions", "service_principals", "service_principal_tokens"}.issubset(tables))
        self.assertTrue({"auth_version", "updated_at"}.issubset(user_columns))
        self.assertEqual(
            set(session_columns),
            {
                "id",
                "user_id",
                "subject",
                "role",
                "auth_version",
                "auth_state_digest",
                "session_digest",
                "expires_at",
                "revoked_at",
                "last_used_at",
                "created_at",
            },
        )
        self.assertTrue(session_columns["user_id"]["nullable"])
        self.assertEqual(
            principal_columns,
            {
                "id",
                "identifier",
                "kind",
                "scopes",
                "is_active",
                "expires_at",
                "last_used_at",
                "created_at",
                "updated_at",
            },
        )
        self.assertEqual(
            token_columns,
            {
                "id",
                "principal_id",
                "token_digest",
                "issued_at",
                "expires_at",
                "revoked_at",
                "last_used_at",
                "replaced_by_token_id",
            },
        )
        self.assertIn("actor_service_principal_id", audit_columns)
        self.assertIn("actor_subject", audit_columns)
        self.assertTrue({"idx_auth_sessions_user_active", "idx_auth_sessions_expires_at"}.issubset(session_indexes))
        self.assertTrue(
            {"idx_service_principals_kind_active", "idx_service_principals_expires_at"}.issubset(
                principal_indexes
            )
        )
        self.assertTrue(
            {
                "idx_service_principal_tokens_principal_active",
                "idx_service_principal_tokens_expires_at",
            }.issubset(token_indexes)
        )

    def test_populated_previous_head_upgrades_and_previous_application_remains_compatible(self):
        url = create_database(self.databases[1])
        legacy_user_id = "00000000-0000-0000-0000-000000001301"

        run_alembic(url, "upgrade", PREVIOUS_HEAD)
        engine = create_engine(url)
        try:
            with engine.begin() as connection:
                connection.execute(text("""
                    INSERT INTO users (id, username, password_hash, role, is_active)
                    VALUES (
                        CAST(:user_id AS uuid), '998000001301', 'synthetic-hash-before-upgrade',
                        'operator', true
                    )
                """), {"user_id": legacy_user_id})
                connection.execute(text("""
                    INSERT INTO audit_log (actor_user_id, action, entity_type, entity_id, payload)
                    VALUES (
                        CAST(:user_id AS uuid), 'synthetic.before_upgrade', 'user', :user_id,
                        '{"source":"synthetic"}'::jsonb
                    )
                """), {"user_id": legacy_user_id})
        finally:
            engine.dispose()

        run_alembic(url, "upgrade", "head")
        run_alembic(url, "upgrade", "head")

        self.assertEqual(scalar(url, "SELECT version_num FROM alembic_version"), CURRENT_HEAD)
        self.assertEqual(
            scalar(url, "SELECT auth_version FROM users WHERE id='00000000-0000-0000-0000-000000001301'"),
            1,
        )
        self.assertEqual(
            scalar(url, "SELECT count(*) FROM audit_log WHERE action='synthetic.before_upgrade'"),
            1,
        )

        engine = create_engine(url)
        try:
            with engine.begin() as connection:
                # The previous application names only its original columns. Extra columns use
                # server defaults, so an application rollback stays operational on the expanded schema.
                connection.execute(text("""
                    INSERT INTO users (username, password_hash, role, is_active)
                    VALUES ('998000001302', 'synthetic-old-app-hash', 'operator', true)
                """))
                connection.execute(text("""
                    INSERT INTO audit_log (action, entity_type, entity_id, payload)
                    VALUES ('synthetic.old_app_write', 'user', '998000001302', '{}'::jsonb)
                """))
                connection.execute(text("""
                    UPDATE users
                    SET password_hash='synthetic-old-app-hash-rotated', role='logistics_slots'
                    WHERE username='998000001302'
                """))
                legacy_row = connection.execute(text("""
                    SELECT username, password_hash, role, is_active, created_at, auth_version
                    FROM users WHERE username='998000001302'
                """)).mappings().one()
        finally:
            engine.dispose()

        self.assertEqual(legacy_row["username"], "998000001302")
        self.assertEqual(legacy_row["role"], "logistics_slots")
        self.assertTrue(legacy_row["is_active"])
        self.assertGreaterEqual(legacy_row["auth_version"], 2)
        self.assertEqual(scalar(url, "SELECT count(*) FROM alembic_version"), 1)

    def test_digest_only_storage_rotation_cap_and_audit_sentinel_absence(self):
        url = create_database(self.databases[2])
        run_alembic(url, "upgrade", "head")

        sentinel = "SYNTHETIC-PLAINTEXT-SERVICE-TOKEN-PHASE13"
        session_secret = "SYNTHETIC-PLAINTEXT-SESSION-PHASE13"
        token_digest = hashlib.sha256(sentinel.encode("utf-8")).hexdigest()
        session_digest = hashlib.sha256(session_secret.encode("utf-8")).hexdigest()
        auth_state_digest = hashlib.sha256(b"synthetic-auth-state").hexdigest()
        user_id = "00000000-0000-0000-0000-000000001311"
        principal_id = "00000000-0000-0000-0000-000000001312"
        old_token_id = "00000000-0000-0000-0000-000000001313"
        new_token_id = "00000000-0000-0000-0000-000000001314"

        engine = create_engine(url)
        try:
            with engine.begin() as connection:
                connection.execute(text("""
                    INSERT INTO users (id, username, password_hash, role, is_active)
                    VALUES (CAST(:user_id AS uuid), '998000001311', 'synthetic-password-hash', 'operator', true)
                """), {"user_id": user_id})
                connection.execute(text("""
                    INSERT INTO auth_sessions (
                        user_id, subject, role, auth_version, auth_state_digest, session_digest, expires_at
                    ) VALUES (
                        CAST(:user_id AS uuid), '998000001311', 'operator', 1,
                        :auth_state_digest, :session_digest, now() + interval '1 hour'
                    )
                """), {
                    "user_id": user_id,
                    "auth_state_digest": auth_state_digest,
                    "session_digest": session_digest,
                })
                connection.execute(text("""
                    INSERT INTO service_principals (id, identifier, kind, scopes, expires_at)
                    VALUES (
                        CAST(:principal_id AS uuid), 'desktop-synthetic', 'desktop',
                        '["orders:read","scans:write"]'::jsonb, now() + interval '30 days'
                    )
                """), {"principal_id": principal_id})
                connection.execute(text("""
                    INSERT INTO service_principal_tokens (
                        id, principal_id, token_digest, expires_at
                    ) VALUES (
                        CAST(:old_token_id AS uuid), CAST(:principal_id AS uuid), :token_digest,
                        now() + interval '7 days'
                    ), (
                        CAST(:new_token_id AS uuid), CAST(:principal_id AS uuid), repeat('a', 64),
                        now() + interval '7 days'
                    )
                """), {
                    "old_token_id": old_token_id,
                    "new_token_id": new_token_id,
                    "principal_id": principal_id,
                    "token_digest": token_digest,
                })
                connection.execute(text("""
                    UPDATE service_principal_tokens
                    SET replaced_by_token_id=CAST(:new_token_id AS uuid),
                        expires_at=LEAST(expires_at, now() + interval '5 minutes')
                    WHERE id=CAST(:old_token_id AS uuid)
                """), {"new_token_id": new_token_id, "old_token_id": old_token_id})
                connection.execute(text("""
                    INSERT INTO audit_log (
                        actor_service_principal_id, action, entity_type, entity_id, payload
                    ) VALUES (
                        CAST(:principal_id AS uuid), 'service_token.rotate', 'service_principal',
                        CAST(:principal_id AS text),
                        jsonb_build_object(
                            'old_token_id', CAST(:old_token_id AS text),
                            'new_token_id', CAST(:new_token_id AS text)
                        )
                    )
                """), {
                    "principal_id": principal_id,
                    "old_token_id": old_token_id,
                    "new_token_id": new_token_id,
                })
                old_token = connection.execute(text("""
                    SELECT replaced_by_token_id::text AS replaced_by, expires_at < now() + interval '6 minutes' AS capped
                    FROM service_principal_tokens WHERE id=CAST(:old_token_id AS uuid)
                """), {"old_token_id": old_token_id}).mappings().one()
                sentinel_count = connection.execute(text("""
                    SELECT count(*) FROM (
                        SELECT session_digest AS value FROM auth_sessions
                        UNION ALL SELECT auth_state_digest FROM auth_sessions
                        UNION ALL SELECT token_digest FROM service_principal_tokens
                        UNION ALL SELECT identifier FROM service_principals
                        UNION ALL SELECT payload::text FROM audit_log
                    ) values_to_scan
                    WHERE value LIKE '%' || :sentinel || '%' OR value LIKE '%' || :session_secret || '%'
                """), {"sentinel": sentinel, "session_secret": session_secret}).scalar_one()
        finally:
            engine.dispose()

        self.assertEqual(old_token["replaced_by"], new_token_id)
        self.assertTrue(old_token["capped"])
        self.assertEqual(sentinel_count, 0)
        self.assertEqual(
            scalar(url, "SELECT count(*) FROM service_principal_tokens WHERE length(token_digest)=64"),
            2,
        )

    def test_concurrent_rotations_leave_only_one_long_lived_replacement(self):
        url = create_database(self.databases[3])
        run_alembic(url, "upgrade", "head")
        engine = create_engine(url, pool_pre_ping=True)
        Session = sessionmaker(bind=engine, expire_on_commit=False)
        now = datetime.now(timezone.utc)
        try:
            with Session() as db:
                principal = ServicePrincipal(
                    identifier="worker-concurrent-rotation",
                    kind="worker",
                    scopes=["orders:read"],
                    is_active=True,
                    expires_at=now + timedelta(days=1),
                )
                db.add(principal)
                db.flush()
                issue_service_token(
                    db,
                    principal,
                    expires_at=now + timedelta(hours=4),
                    now=now,
                    secret_factory=lambda _count: "synthetic-concurrent-original-" + "x" * 32,
                )
                principal_id = principal.id
                db.commit()

            barrier = threading.Barrier(2)

            def rotate(label):
                with Session() as db:
                    principal = db.get(ServicePrincipal, principal_id)
                    barrier.wait(timeout=5)
                    issued = rotate_service_token(
                        db,
                        principal,
                        expires_at=now + timedelta(hours=4),
                        overlap_seconds=30,
                        max_overlap_seconds=300,
                        now=now,
                        secret_factory=lambda _count: f"synthetic-concurrent-{label}-" + "x" * 32,
                    )
                    db.commit()
                    return issued.identifier

            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(rotate, label) for label in ("one", "two")]
                issued_ids = {future.result(timeout=15) for future in futures}

            with Session() as db:
                tokens = db.query(ServicePrincipalToken).filter(
                    ServicePrincipalToken.principal_id == principal_id
                ).all()
                long_lived = [
                    token for token in tokens
                    if token.revoked_at is None
                    and token.expires_at.astimezone(timezone.utc) > now + timedelta(seconds=30)
                ]
                self.assertEqual(len(tokens), 3)
                self.assertEqual(len(issued_ids), 2)
                self.assertEqual(len(long_lived), 1)
                self.assertIn(long_lived[0].id, issued_ids)
        finally:
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
