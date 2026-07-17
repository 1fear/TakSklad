import contextlib
import hashlib
import io
import os
import stat
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from backend.app.auth_identities import (
    IdentityAuthError,
    SERVICE_PRINCIPAL_SCOPE_MATRIX,
    authenticate_service_token,
)
from backend.app.models import AuditLog, Base, ServicePrincipal, ServicePrincipalToken
from tools import manage_service_principals as tool


class ManageServicePrincipalsTests(unittest.TestCase):
    def run_main(self, arguments):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            exit_code = tool.main(arguments)
        return exit_code, stdout.getvalue(), stderr.getvalue()

    def create_database(self, directory):
        path = Path(directory, "identities.sqlite3")
        url = f"sqlite+pysqlite:///{path}"
        engine = create_engine(url)
        Base.metadata.create_all(engine)
        engine.dispose()
        return path, url

    def test_dummy_plan_is_deterministic_and_never_connects_or_generates(self):
        with (
            mock.patch.object(tool, "create_engine", side_effect=AssertionError("must not connect")),
            mock.patch(
                "backend.app.auth_identities.secrets.token_urlsafe",
                side_effect=AssertionError("must not generate"),
            ),
        ):
            first = self.run_main(["plan", "--dummy-only"])
            second = self.run_main(["plan", "--dummy-only"])

        self.assertEqual(first, second)
        exit_code, stdout, stderr = first
        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr, "")
        lines = stdout.splitlines()
        self.assertEqual(len(lines), len(SERVICE_PRINCIPAL_SCOPE_MATRIX) + 1)
        for kind in sorted(SERVICE_PRINCIPAL_SCOPE_MATRIX):
            expected_scopes = ",".join(sorted(SERVICE_PRINCIPAL_SCOPE_MATRIX[kind]))
            self.assertTrue(
                any(
                    f"identifier={kind} kind={kind} scopes={expected_scopes}" in line
                    for line in lines
                ),
                kind,
            )
        self.assertEqual(
            lines[-1],
            "service_principal_plan_summary principals=3 "
            "rotation_max_overlap_seconds=900 secret_values=0",
        )

    def test_remote_database_is_rejected_before_engine_creation(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            secret_file = str(Path(tmp_dir, "handoff.txt"))
            with mock.patch.object(tool, "create_engine", side_effect=AssertionError("must not connect")):
                exit_code, stdout, stderr = self.run_main([
                    "provision",
                    "--apply",
                    "--database-url",
                    "postgresql+psycopg://synthetic@db.example.test/taksklad",
                    "--secret-file",
                    secret_file,
                    "--identifier",
                    "desktop",
                    "--kind",
                    "desktop",
                ])

        self.assertEqual(exit_code, 2)
        self.assertEqual(stdout, "")
        self.assertEqual(stderr, "service_principal_error class=database_url_not_local\n")
        self.assertNotIn("db.example.test", stderr)

    def test_local_database_validator_accepts_files_loopback_and_unix_socket_only(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            sqlite_url = f"sqlite+pysqlite:///{Path(tmp_dir, 'local.sqlite3')}"
            accepted = (
                sqlite_url,
                "postgresql+psycopg://synthetic@localhost/taksklad",
                "postgresql+psycopg://synthetic@127.0.0.1:5432/taksklad",
                "postgresql+psycopg://synthetic@[::1]:5432/taksklad",
                "postgresql+psycopg://synthetic@/taksklad?host=/var/run/postgresql",
                "postgresql+psycopg://synthetic@/taksklad",
            )
            for database_url in accepted:
                with self.subTest(database_url=database_url):
                    self.assertTrue(tool.validate_local_database_url(database_url))

            rejected = (
                "sqlite+pysqlite:///:memory:",
                "sqlite+pysqlite:///relative.sqlite3",
                "postgresql+psycopg://synthetic@db.example.test/taksklad",
                "postgresql+psycopg://synthetic@localhost/taksklad?hostaddr=203.0.113.10",
                "postgresql+psycopg://synthetic@/taksklad?service=remote-profile",
                "postgresql+psycopg://synthetic@/taksklad?host=/var/run/postgresql,db.example.test",
            )
            for database_url in rejected:
                with self.subTest(database_url=database_url):
                    with self.assertRaises(tool.ServicePrincipalToolError):
                        tool.validate_local_database_url(database_url)

    def test_provision_requires_apply_without_mutating_database_or_file(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            database_path, database_url = self.create_database(tmp_dir)
            secret_path = Path(tmp_dir, "initial.txt")
            before = database_path.read_bytes()

            exit_code, stdout, stderr = self.run_main([
                "provision",
                "--database-url",
                database_url,
                "--secret-file",
                str(secret_path),
                "--identifier",
                "desktop",
                "--kind",
                "desktop",
            ])

            self.assertEqual(exit_code, 2)
            self.assertEqual(stdout, "")
            self.assertEqual(stderr, "service_principal_error class=apply_required\n")
            self.assertFalse(secret_path.exists())
            self.assertEqual(database_path.read_bytes(), before)

    def test_local_sqlite_provision_hands_off_once_and_persists_digest_only(self):
        sentinel = "PHASE13-SYNTHETIC-SERVICE-SECRET"
        with tempfile.TemporaryDirectory() as tmp_dir:
            _, database_url = self.create_database(tmp_dir)
            secret_path = Path(tmp_dir, "initial-handoff.txt")
            with mock.patch(
                "backend.app.auth_identities.secrets.token_urlsafe",
                return_value=sentinel,
            ):
                exit_code, stdout, stderr = self.run_main([
                    "provision",
                    "--apply",
                    "--database-url",
                    database_url,
                    "--secret-file",
                    str(secret_path),
                    "--identifier",
                    "desktop",
                    "--kind",
                    "desktop",
                ])

            self.assertEqual(exit_code, 0, stderr)
            self.assertEqual(stderr, "")
            token = secret_path.read_text(encoding="utf-8").strip()
            self.assertTrue(token.endswith(f".{sentinel}"))
            self.assertEqual(stat.S_IMODE(secret_path.stat().st_mode), 0o600)
            self.assertNotIn(token, stdout)
            self.assertNotIn(sentinel, stdout)
            self.assertNotIn(str(secret_path), stdout)
            self.assertRegex(
                stdout,
                r"^service_principal_apply principal_id=[0-9a-f-]+ "
                r"token_id=[0-9a-f-]+ state=provisioned\n$",
            )

            engine = create_engine(database_url)
            Session = sessionmaker(bind=engine)
            with Session() as db:
                principal = db.execute(select(ServicePrincipal)).scalar_one()
                stored = db.execute(select(ServicePrincipalToken)).scalar_one()
                audits = db.execute(select(AuditLog)).scalars().all()
                self.assertEqual(principal.identifier, "desktop")
                self.assertEqual(principal.kind, "desktop")
                self.assertEqual(principal.scopes, sorted(SERVICE_PRINCIPAL_SCOPE_MATRIX["desktop"]))
                self.assertEqual(stored.token_digest, hashlib.sha256(token.encode("utf-8")).hexdigest())
                self.assertNotIn(sentinel, stored.token_digest)
                self.assertEqual(len(stored.token_digest), 64)
                self.assertEqual([audit.action for audit in audits], ["service_principal_provisioned"])
                self.assertNotIn(sentinel, repr([audit.payload for audit in audits]))
            engine.dispose()

            second_exit, second_stdout, second_stderr = self.run_main([
                "provision",
                "--apply",
                "--database-url",
                database_url,
                "--secret-file",
                str(secret_path),
                "--identifier",
                "desktop-second",
                "--kind",
                "desktop",
            ])
            self.assertEqual(second_exit, 2)
            self.assertEqual(second_stdout, "")
            self.assertEqual(second_stderr, "service_principal_error class=secret_file_exists\n")
            self.assertEqual(secret_path.read_text(encoding="utf-8").strip(), token)

    def test_acceptance_principal_can_be_narrowed_to_returns_read_only(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            _, database_url = self.create_database(tmp_dir)
            secret_path = Path(tmp_dir, "returns-canary-handoff.txt")
            exit_code, stdout, stderr = self.run_main([
                "provision",
                "--apply",
                "--database-url",
                database_url,
                "--secret-file",
                str(secret_path),
                "--identifier",
                "release-returns-canary",
                "--kind",
                "acceptance",
                "--scope",
                "returns:read",
            ])

            self.assertEqual(exit_code, 0, stderr)
            self.assertNotIn(secret_path.read_text(encoding="utf-8").strip(), stdout)
            engine = create_engine(database_url)
            Session = sessionmaker(bind=engine)
            with Session() as db:
                principal = db.execute(select(ServicePrincipal)).scalar_one()
                self.assertEqual(principal.kind, "acceptance")
                self.assertEqual(principal.scopes, ["returns:read"])
            engine.dispose()

    def test_provision_rejects_scope_outside_principal_kind(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            _, database_url = self.create_database(tmp_dir)
            secret_path = Path(tmp_dir, "blocked-handoff.txt")
            exit_code, stdout, stderr = self.run_main([
                "provision",
                "--apply",
                "--database-url",
                database_url,
                "--secret-file",
                str(secret_path),
                "--identifier",
                "release-returns-canary",
                "--kind",
                "acceptance",
                "--scope",
                "returns:write",
            ])

            self.assertEqual(exit_code, 2)
            self.assertEqual(stdout, "")
            self.assertEqual(stderr, "service_principal_error class=principal_scope_invalid\n")
            self.assertFalse(secret_path.exists())

    def test_local_sqlite_rotation_bounds_old_token_and_keeps_plaintext_out_of_output(self):
        old_secret = "PHASE13-SYNTHETIC-OLD-SERVICE-SECRET"
        new_secret = "PHASE13-SYNTHETIC-NEW-SERVICE-SECRET"
        with tempfile.TemporaryDirectory() as tmp_dir:
            _, database_url = self.create_database(tmp_dir)
            old_path = Path(tmp_dir, "old-handoff.txt")
            new_path = Path(tmp_dir, "new-handoff.txt")
            with mock.patch(
                "backend.app.auth_identities.secrets.token_urlsafe",
                return_value=old_secret,
            ):
                provisioned = self.run_main([
                    "provision",
                    "--apply",
                    "--database-url",
                    database_url,
                    "--secret-file",
                    str(old_path),
                    "--identifier",
                    "worker",
                    "--kind",
                    "worker",
                ])
            self.assertEqual(provisioned[0], 0, provisioned[2])
            old_token = old_path.read_text(encoding="utf-8").strip()

            rotate_started = datetime.now(timezone.utc)
            with mock.patch(
                "backend.app.auth_identities.secrets.token_urlsafe",
                return_value=new_secret,
            ):
                exit_code, stdout, stderr = self.run_main([
                    "rotate",
                    "--apply",
                    "--database-url",
                    database_url,
                    "--secret-file",
                    str(new_path),
                    "--identifier",
                    "worker",
                    "--overlap-seconds",
                    "30",
                ])

            self.assertEqual(exit_code, 0, stderr)
            self.assertEqual(stderr, "")
            new_token = new_path.read_text(encoding="utf-8").strip()
            self.assertEqual(stat.S_IMODE(new_path.stat().st_mode), 0o600)
            for hidden in (old_token, new_token, old_secret, new_secret, str(new_path)):
                self.assertNotIn(hidden, stdout)
            self.assertRegex(
                stdout,
                r"^service_principal_apply principal_id=[0-9a-f-]+ "
                r"token_id=[0-9a-f-]+ state=rotated\n$",
            )

            engine = create_engine(database_url)
            Session = sessionmaker(bind=engine)
            with Session() as db:
                tokens = db.execute(
                    select(ServicePrincipalToken).order_by(ServicePrincipalToken.issued_at)
                ).scalars().all()
                self.assertEqual(len(tokens), 2)
                old_stored, new_stored = tokens
                self.assertEqual(old_stored.replaced_by_token_id, new_stored.id)
                old_expiry = self.as_utc(old_stored.expires_at)
                self.assertGreaterEqual(old_expiry, rotate_started + timedelta(seconds=28))
                self.assertLessEqual(old_expiry, datetime.now(timezone.utc) + timedelta(seconds=31))
                self.assertEqual(new_stored.token_digest, hashlib.sha256(new_token.encode("utf-8")).hexdigest())

                verified = authenticate_service_token(
                    db,
                    old_token,
                    now=old_expiry - timedelta(microseconds=1),
                    touch_interval_seconds=0,
                )
                self.assertEqual(verified.token_id, old_stored.id)
                with self.assertRaises(IdentityAuthError):
                    authenticate_service_token(db, old_token, now=old_expiry)
                self.assertEqual(
                    authenticate_service_token(db, new_token, now=old_expiry).token_id,
                    new_stored.id,
                )
                audits = db.execute(select(AuditLog).order_by(AuditLog.created_at)).scalars().all()
                self.assertEqual(
                    [audit.action for audit in audits],
                    ["service_principal_provisioned", "service_principal_token_rotated"],
                )
                serialized = repr([
                    (token.token_digest, token.id, token.replaced_by_token_id) for token in tokens
                ]) + repr([audit.payload for audit in audits])
                self.assertNotIn(old_secret, serialized)
                self.assertNotIn(new_secret, serialized)
            engine.dispose()

    @staticmethod
    def as_utc(value):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


if __name__ == "__main__":
    unittest.main()
