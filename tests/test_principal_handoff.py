import io
import json
import os
from pathlib import Path
import stat
import tempfile
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest
import uuid
from unittest import mock

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app import principal_handoff
from backend.app.auth_identities import ACCEPTANCE_CANARY_SCOPES, SERVICE_PRINCIPAL_SCOPE_MATRIX
from backend.app.models import AuditLog, Base, ServicePrincipal, ServicePrincipalToken


ROOT = Path(__file__).resolve().parents[1]


class PrincipalHandoffTests(unittest.TestCase):
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

    def args(self, command, kind, path, identifier="acceptance.release"):
        return SimpleNamespace(
            command=command,
            kind=kind,
            identifier=identifier,
            handoff_file=str(path),
            ttl_seconds=3600,
            overlap_seconds=0,
            apply=command != "status",
            operation_id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"{command}:{kind}:{identifier}:{path}")),
        )

    def apply(self, args, *, now=None):
        approval = principal_handoff.expected_command_approval(args.command, args.kind)
        with mock.patch.dict(
            "os.environ",
            {"TAKSKLAD_PRINCIPAL_COMMAND_APPROVAL": approval},
            clear=False,
        ):
            return principal_handoff.apply_command(args, session_factory=self.Session, now=now)

    def test_acceptance_and_desktop_use_exact_canonical_scopes_and_atomic_handoff(self):
        with tempfile.TemporaryDirectory() as temporary:
            handoff_root = Path(temporary) / "handoff"
            handoff_root.mkdir(mode=0o700)
            token_path = handoff_root / "acceptance-canary.token"
            with mock.patch.object(principal_handoff, "HANDOFF_ROOT", handoff_root):
                state = self.apply(self.args("provision", "acceptance", token_path))
            self.assertEqual(state, "provisioned")
            self.assertTrue(token_path.is_file())
            self.assertEqual(token_path.stat().st_mode & 0o777, 0o600)
            with self.Session() as db:
                principal = db.execute(select(ServicePrincipal)).scalar_one()
                self.assertEqual(set(principal.scopes), set(ACCEPTANCE_CANARY_SCOPES))
                self.assertEqual(principal.kind, "acceptance")
                audit = db.execute(select(AuditLog)).scalars().all()[0]
                self.assertEqual(audit.action, "service_principal_handoff_provisioned")
                self.assertEqual(audit.payload["kind"], "acceptance")
                self.assertEqual(audit.payload["scopes"], sorted(ACCEPTANCE_CANARY_SCOPES))
                self.assertEqual(audit.payload["ttl_seconds"], 3600)
                self.assertEqual(audit.payload["overlap_seconds"], 0)
                self.assertRegex(audit.payload["token_id"], r"^[0-9a-f-]{36}$")
                rendered_audit = json.dumps(audit.payload, sort_keys=True)
                self.assertNotIn("tks.", rendered_audit)
                self.assertNotIn("DATABASE_URL", rendered_audit)

            token_path.unlink()
            desktop_path = handoff_root / "desktop-token"
            with mock.patch.object(principal_handoff, "HANDOFF_ROOT", handoff_root):
                self.apply(self.args("provision", "desktop", desktop_path, "desktop.pc-01"))
            with self.Session() as db:
                desktop = db.execute(
                    select(ServicePrincipal).where(ServicePrincipal.identifier == "desktop.pc-01")
                ).scalar_one()
                self.assertEqual(set(desktop.scopes), set(SERVICE_PRINCIPAL_SCOPE_MATRIX["desktop"]))

    def test_shell_uses_exact_image_and_container_env_without_dsn_or_token_output(self):
        script = (ROOT / "deploy/vds/provision_service_principal.sh").read_text(encoding="utf-8")
        self.assertIn("TAKSKLAD_BACKEND_IMAGE", script)
        self.assertIn("@sha256:", script)
        self.assertIn("docker compose", script)
        self.assertIn("--rm --no-deps --pull never", script)
        self.assertIn("principal-provisioner", script)
        compose = (ROOT / "deploy/vds/docker-compose.yml").read_text(encoding="utf-8")
        self.assertIn('["python", "-m", "app.principal_handoff"]', compose)
        self.assertIn("validate_principal_provisioner_compose.py", script)
        self.assertIn("validate_principal_admin_network.py", script)
        self.assertIn("acceptance-canary.token", script)
        self.assertNotIn("--database-url", script)
        self.assertNotIn(" backend-api ", script)
        self.assertNotIn("--volume", script)
        self.assertNotIn("token=", script)

    def test_existing_principal_policy_mismatch_never_rotates_or_revokes(self):
        with tempfile.TemporaryDirectory() as temporary:
            handoff_root = Path(temporary) / "handoff"
            handoff_root.mkdir(mode=0o700)
            acceptance_path = handoff_root / "acceptance-canary.token"
            desktop_path = handoff_root / "desktop-token"
            with mock.patch.object(principal_handoff, "HANDOFF_ROOT", handoff_root):
                self.apply(self.args("provision", "acceptance", acceptance_path, "acceptance.release"))
                acceptance_path.unlink()
                status = self.apply(
                    self.args("status", "desktop", desktop_path, "acceptance.release"),
                )
                self.assertIsInstance(status, principal_handoff.StatusResult)
                self.assertFalse(status.policy_exact)
                self.assertFalse(status.ready)
                for command in ("rotate", "revoke"):
                    with self.subTest(command=command), self.assertRaisesRegex(
                        principal_handoff.HandoffError, "principal_policy_mismatch"
                    ):
                        self.apply(self.args(command, "desktop", desktop_path, "acceptance.release"))
            with self.Session() as db:
                principal = db.execute(
                    select(ServicePrincipal).where(ServicePrincipal.identifier == "acceptance.release")
                ).scalar_one()
                self.assertTrue(principal.is_active)
                self.assertEqual(principal.kind, "acceptance")

    def test_cli_output_is_sanitized(self):
        output = io.StringIO()
        error = io.StringIO()
        with (
            mock.patch.object(principal_handoff, "apply_command", return_value="ready"),
            mock.patch("sys.stdout", output),
            mock.patch("sys.stderr", error),
        ):
            status = principal_handoff.main(["status", "--kind", "acceptance", "--identifier", "acceptance.release"])
        self.assertEqual(status, 0)
        self.assertEqual(error.getvalue(), "")
        self.assertIn("secret_output=0", output.getvalue())
        self.assertNotIn("tks.", output.getvalue())

    def test_cli_status_not_ready_is_nonzero_and_sanitized(self):
        output = io.StringIO()
        error = io.StringIO()
        result = principal_handoff.StatusResult(True, True, False, False)
        with (
            mock.patch.object(principal_handoff, "apply_command", return_value=result),
            mock.patch("sys.stdout", output),
            mock.patch("sys.stderr", error),
        ):
            status = principal_handoff.main([
                "status", "--kind", "acceptance", "--identifier", "acceptance.release"
            ])
        self.assertEqual(status, 3)
        self.assertEqual(output.getvalue(), "")
        self.assertIn("ready=0", error.getvalue())
        self.assertNotIn("tks.", error.getvalue())

    def test_mutations_require_exact_command_and_role_approval(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            root.chmod(0o700)
            path = root / "acceptance-canary.token"
            args = self.args("provision", "acceptance", path)
            with mock.patch.object(principal_handoff, "HANDOFF_ROOT", root):
                for approval in (None, "ROTATE_ACCEPTANCE_PRINCIPAL", "PROVISION_DESKTOP_PRINCIPAL"):
                    environment = {} if approval is None else {"TAKSKLAD_PRINCIPAL_COMMAND_APPROVAL": approval}
                    with mock.patch.dict(os.environ, environment, clear=True), self.assertRaisesRegex(
                        principal_handoff.HandoffError, "exact_command_approval_required"
                    ):
                        principal_handoff.apply_command(args, session_factory=self.Session)
            self.assertFalse(path.exists())
            with self.Session() as db:
                self.assertEqual(db.query(ServicePrincipal).count(), 0)

    def test_handoff_retries_short_writes_and_eintr_and_cleans_disk_failure(self):
        for mode in ("short", "eintr"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                target = root / "token"
                real_write = os.write
                calls = 0

                def synthetic_write(fd, payload):
                    nonlocal calls
                    calls += 1
                    if mode == "eintr" and calls == 1:
                        raise InterruptedError()
                    return real_write(fd, payload[:3])

                with mock.patch.object(principal_handoff.os, "write", side_effect=synthetic_write):
                    principal_handoff.write_handoff(target, "tks.synthetic.value")
                self.assertEqual(target.read_text(encoding="utf-8"), "tks.synthetic.value\n")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "token"
            with mock.patch.object(principal_handoff.os, "write", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    principal_handoff.write_handoff(target, "tks.synthetic.value")
            self.assertFalse(target.exists())
            self.assertEqual(list(root.iterdir()), [])

    def test_status_binds_exact_live_token_policy_and_handoff(self):
        issued_at = datetime(2026, 7, 17, 8, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            root.chmod(0o700)
            path = root / "acceptance-canary.token"
            with mock.patch.object(principal_handoff, "HANDOFF_ROOT", root):
                self.apply(self.args("provision", "acceptance", path), now=issued_at)
                ready = self.apply(self.args("status", "acceptance", path), now=issued_at)
                self.assertTrue(ready.db_active)
                self.assertTrue(ready.policy_exact)
                self.assertTrue(ready.token_active)
                self.assertTrue(ready.handoff_valid)
                self.assertTrue(ready.ready)

                expired = self.apply(
                    self.args("status", "acceptance", path),
                    now=issued_at + timedelta(seconds=3600),
                )
                self.assertFalse(expired.token_active)
                self.assertFalse(expired.ready)

                path.unlink()
                missing = self.apply(self.args("status", "acceptance", path), now=issued_at)
                self.assertTrue(missing.db_active)
                self.assertFalse(missing.handoff_valid)
                self.assertFalse(missing.ready)

    def test_swapped_revoked_and_principal_expiry_are_not_ready(self):
        now = datetime(2026, 7, 17, 9, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            root.chmod(0o700)
            path = root / "acceptance-canary.token"
            with mock.patch.object(principal_handoff, "HANDOFF_ROOT", root):
                self.apply(self.args("provision", "acceptance", path), now=now)
                original = path.read_bytes()
                path.write_text("tks." + "0" * 32 + "." + "x" * 32 + "\n", encoding="ascii")
                swapped = self.apply(self.args("status", "acceptance", path), now=now)
                self.assertFalse(swapped.token_active)
                self.assertFalse(swapped.handoff_valid)
                path.write_bytes(original)
                with self.Session() as db:
                    token = db.execute(select(ServicePrincipalToken)).scalar_one()
                    token.revoked_at = now
                    db.commit()
                revoked = self.apply(self.args("status", "acceptance", path), now=now)
                self.assertFalse(revoked.token_active)
                with self.Session() as db:
                    principal = db.execute(select(ServicePrincipal)).scalar_one()
                    principal.expires_at = now
                    db.commit()
                principal_expired = self.apply(self.args("status", "acceptance", path), now=now)
                self.assertFalse(principal_expired.db_active)
                self.assertFalse(principal_expired.ready)

    def test_publish_validation_failure_rolls_back_db_audit_and_owned_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            root.chmod(0o700)
            path = root / "acceptance-canary.token"
            with (
                mock.patch.object(principal_handoff, "HANDOFF_ROOT", root),
                mock.patch.object(
                    principal_handoff,
                    "validate_published_handoff",
                    side_effect=principal_handoff.HandoffError("synthetic_validation_failure"),
                ),
                self.assertRaisesRegex(principal_handoff.HandoffError, "synthetic_validation_failure"),
            ):
                self.apply(self.args("provision", "acceptance", path))
            self.assertFalse(path.exists())
            with self.Session() as db:
                self.assertEqual(db.query(ServicePrincipal).count(), 0)
                self.assertEqual(db.query(AuditLog).count(), 0)

    def test_preexisting_concurrent_winner_survives_and_loser_has_no_db_mutation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            root.chmod(0o700)
            path = root / "acceptance-canary.token"
            winner = b"tks." + b"a" * 32 + b"." + b"b" * 32 + b"\n"
            path.write_bytes(winner)
            path.chmod(0o600)
            with mock.patch.object(principal_handoff, "HANDOFF_ROOT", root), self.assertRaisesRegex(
                principal_handoff.HandoffError, "handoff_file_exists"
            ):
                self.apply(self.args("provision", "acceptance", path))
            self.assertEqual(path.read_bytes(), winner)
            with self.Session() as db:
                self.assertEqual(db.query(ServicePrincipal).count(), 0)
                self.assertEqual(db.query(AuditLog).count(), 0)

    def test_repeated_revoke_is_idempotent_without_duplicate_audit(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            root.chmod(0o700)
            path = root / "acceptance-canary.token"
            with mock.patch.object(principal_handoff, "HANDOFF_ROOT", root):
                self.apply(self.args("provision", "acceptance", path))
                self.apply(self.args("revoke", "acceptance", path))
                self.apply(self.args("revoke", "acceptance", path))
            self.assertFalse(path.exists())
            with self.Session() as db:
                actions = [row.action for row in db.execute(select(AuditLog)).scalars()]
                self.assertEqual(actions.count("service_principal_handoff_revoked"), 1)

    def test_database_target_is_exact_and_timeouts_are_bounded(self):
        allowed = "postgresql+psycopg://svc:p%40ss%3Aword@postgres:5432/taksklad"
        engine = object()
        factory = object()
        with (
            mock.patch.dict(os.environ, {
                "DATABASE_URL": allowed,
                "TAKSKLAD_PRINCIPAL_HANDOFF_ROOT": "/run/taksklad-private",
            }, clear=True),
            mock.patch.object(principal_handoff, "create_engine", return_value=engine) as create_engine,
            mock.patch.object(principal_handoff, "sessionmaker", return_value=factory) as sessionmaker,
        ):
            self.assertIs(principal_handoff.default_session_factory(), factory)
        options = create_engine.call_args.kwargs["connect_args"]
        self.assertEqual(options["connect_timeout"], 5)
        self.assertIn("statement_timeout=15000", options["options"])
        self.assertIn("lock_timeout=5000", options["options"])
        self.assertIn("idle_in_transaction_session_timeout=15000", options["options"])
        sessionmaker.assert_called_once_with(bind=engine, expire_on_commit=False)

        rejected = (
            "postgresql+psycopg://svc:pw@evil.invalid:5432/taksklad",
            "postgresql+psycopg://svc:pw@postgres:5433/taksklad",
            "postgresql+psycopg://svc:pw@postgres:5432/taksklad?host=evil.invalid",
            "postgresql+psycopg://svc:pw@postgres:5432/taksklad?hostaddr=127.0.0.1",
            "postgresql+psycopg://svc:pw@postgres:5432/taksklad?target_session_attrs=any",
            "postgresql://svc:pw@postgres:5432/taksklad",
            "postgresql+psycopg://svc:pw@postgres,evil.invalid:5432/taksklad",
        )
        for value in rejected:
            with self.subTest(value=value), mock.patch.dict(os.environ, {
                "DATABASE_URL": value,
                "TAKSKLAD_PRINCIPAL_HANDOFF_ROOT": "/run/taksklad-private",
            }, clear=True), self.assertRaisesRegex(principal_handoff.HandoffError, "database_environment_invalid"):
                principal_handoff.default_session_factory()

    def test_crash_window_states_fail_closed_without_automatic_deletion(self):
        now = datetime(2026, 7, 17, 10, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            root.chmod(0o700)
            path = root / "acceptance-canary.token"
            orphan = b"tks." + b"a" * 32 + b"." + b"b" * 32 + b"\n"
            path.write_bytes(orphan)
            path.chmod(0o600)
            with mock.patch.object(principal_handoff, "HANDOFF_ROOT", root):
                result = self.apply(self.args("status", "acceptance", path), now=now)
            self.assertFalse(result.db_active)
            self.assertFalse(result.ready)
            self.assertEqual(path.read_bytes(), orphan)

    def test_unexpected_database_failure_is_sanitized(self):
        output = io.StringIO()
        error = io.StringIO()
        with (
            mock.patch.object(principal_handoff, "apply_command", side_effect=RuntimeError("raw dsn and token")),
            mock.patch("sys.stdout", output),
            mock.patch("sys.stderr", error),
        ):
            status = principal_handoff.main([
                "status", "--kind", "acceptance", "--identifier", "acceptance.release"
            ])
        self.assertEqual(status, 1)
        self.assertEqual(output.getvalue(), "")
        self.assertIn("operation_failed", error.getvalue())
        self.assertNotIn("raw dsn", error.getvalue())
        self.assertNotIn("token", error.getvalue().lower())

    def test_rotate_atomically_replaces_bound_handoff_and_failure_restores_old(self):
        now = datetime(2026, 7, 17, 11, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            root.chmod(0o700)
            path = root / "acceptance-canary.token"
            with mock.patch.object(principal_handoff, "HANDOFF_ROOT", root):
                self.apply(self.args("provision", "acceptance", path), now=now)
                old = path.read_bytes()
                self.apply(self.args("rotate", "acceptance", path), now=now + timedelta(seconds=1))
                self.assertNotEqual(path.read_bytes(), old)
                status = self.apply(self.args("status", "acceptance", path), now=now + timedelta(seconds=1))
                self.assertTrue(status.ready)
            with self.Session() as db:
                self.assertEqual(db.query(ServicePrincipalToken).count(), 2)
                actions = [row.action for row in db.execute(select(AuditLog)).scalars()]
                self.assertEqual(actions.count("service_principal_handoff_rotated"), 1)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            root.chmod(0o700)
            path = root / "acceptance-canary.token"
            engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
            Base.metadata.create_all(engine)
            Session = sessionmaker(bind=engine, expire_on_commit=False)
            with mock.patch.object(principal_handoff, "HANDOFF_ROOT", root):
                args = self.args("provision", "acceptance", path)
                with mock.patch.dict(os.environ, {
                    "TAKSKLAD_PRINCIPAL_COMMAND_APPROVAL": "PROVISION_ACCEPTANCE_PRINCIPAL"
                }, clear=False):
                    principal_handoff.apply_command(args, session_factory=Session, now=now)
                old = path.read_bytes()
                with (
                    mock.patch.dict(os.environ, {
                        "TAKSKLAD_PRINCIPAL_COMMAND_APPROVAL": "ROTATE_ACCEPTANCE_PRINCIPAL"
                    }, clear=False),
                    mock.patch.object(
                        principal_handoff,
                        "validate_published_handoff",
                        side_effect=principal_handoff.HandoffError("post_replace_failure"),
                    ),
                    self.assertRaisesRegex(principal_handoff.HandoffError, "post_replace_failure"),
                ):
                    principal_handoff.apply_command(
                        self.args("rotate", "acceptance", path),
                        session_factory=Session,
                        now=now + timedelta(seconds=1),
                    )
            self.assertEqual(path.read_bytes(), old)
            with Session() as db:
                self.assertEqual(db.query(ServicePrincipalToken).count(), 1)
            engine.dispose()

    def test_destroy_desktop_handoff_preserves_active_principal_and_revoke_cleanup_failure_stays_revoked(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            root.chmod(0o700)
            desktop = root / "desktop-token"
            with mock.patch.object(principal_handoff, "HANDOFF_ROOT", root):
                self.apply(self.args("provision", "desktop", desktop, "desktop.pc-01"))
                self.apply(self.args("destroy-handoff", "desktop", desktop, "desktop.pc-01"))
            self.assertFalse(desktop.exists())
            with self.Session() as db:
                principal = db.execute(select(ServicePrincipal)).scalar_one()
                token = db.execute(select(ServicePrincipalToken)).scalar_one()
                self.assertTrue(principal.is_active)
                self.assertIsNone(token.revoked_at)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            root.chmod(0o700)
            path = root / "acceptance-canary.token"
            with mock.patch.object(principal_handoff, "HANDOFF_ROOT", root):
                self.apply(self.args("provision", "acceptance", path))
                with mock.patch.object(principal_handoff, "remove_owned_handoff", return_value=False):
                    result = self.apply(self.args("revoke", "acceptance", path))
                self.assertIsInstance(result, principal_handoff.RevokeResult)
                self.assertEqual(result.cleanup, "unverified")
            self.assertTrue(path.exists())
            with self.Session() as db:
                principal = db.execute(
                    select(ServicePrincipal).where(ServicePrincipal.identifier == "acceptance.release")
                ).scalar_one()
                token = db.execute(
                    select(ServicePrincipalToken).where(ServicePrincipalToken.principal_id == principal.id)
                ).scalar_one()
                self.assertFalse(principal.is_active)
                self.assertIsNotNone(token.revoked_at)

    def test_destroy_fsync_after_unlink_failure_restores_exact_handoff_and_db_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            root.chmod(0o700)
            desktop = root / "desktop-token"
            with mock.patch.object(principal_handoff, "HANDOFF_ROOT", root):
                self.apply(self.args("provision", "desktop", desktop, "desktop.pc-01"))
                original_payload = desktop.read_bytes()
                original_stat = desktop.lstat()
                real_fsync_parent = principal_handoff.fsync_parent
                calls = 0

                def fail_once(path):
                    nonlocal calls
                    calls += 1
                    if calls == 1:
                        raise OSError("synthetic directory fsync failure")
                    return real_fsync_parent(path)

                with (
                    mock.patch.object(principal_handoff, "fsync_parent", side_effect=fail_once),
                    self.assertRaisesRegex(OSError, "synthetic directory fsync failure"),
                ):
                    self.apply(
                        self.args("destroy-handoff", "desktop", desktop, "desktop.pc-01")
                    )

            restored_stat = desktop.lstat()
            self.assertEqual(desktop.read_bytes(), original_payload)
            self.assertEqual(stat.S_IMODE(restored_stat.st_mode), stat.S_IMODE(original_stat.st_mode))
            self.assertEqual(restored_stat.st_uid, original_stat.st_uid)
            with self.Session() as db:
                principal = db.execute(select(ServicePrincipal)).scalar_one()
                token = db.execute(select(ServicePrincipalToken)).scalar_one()
                actions = [row.action for row in db.execute(select(AuditLog)).scalars()]
                self.assertTrue(principal.is_active)
                self.assertIsNone(token.revoked_at)
                self.assertEqual(actions, ["service_principal_handoff_provisioned"])

    def test_link_eexist_competitor_winner_survives_without_db_or_audit(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            root.chmod(0o700)
            path = root / "acceptance-canary.token"
            winner = b"tks." + b"c" * 32 + b"." + b"d" * 32 + b"\n"
            real_link = os.link

            def competitor_link(source, destination):
                Path(destination).write_bytes(winner)
                Path(destination).chmod(0o600)
                raise FileExistsError("synthetic competitor")

            with (
                mock.patch.object(principal_handoff, "HANDOFF_ROOT", root),
                mock.patch.object(principal_handoff.os, "link", side_effect=competitor_link),
                self.assertRaises(FileExistsError),
            ):
                self.apply(self.args("provision", "acceptance", path))
            self.assertEqual(path.read_bytes(), winner)
            with self.Session() as db:
                self.assertEqual(db.query(ServicePrincipal).count(), 0)
                self.assertEqual(db.query(AuditLog).count(), 0)

    def test_stable_operation_replay_precedes_file_preconditions_and_mismatch_blocks(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            root.chmod(0o700)
            path = root / "acceptance-canary.token"
            args = self.args("provision", "acceptance", path)
            with mock.patch.object(principal_handoff, "HANDOFF_ROOT", root):
                self.assertEqual(self.apply(args), "provisioned")
                self.assertEqual(self.apply(args), "already_applied")
                rotate = self.args("rotate", "acceptance", path)
                rotate.operation_id = args.operation_id
                with self.assertRaisesRegex(principal_handoff.HandoffError, "operation_id_reuse_mismatch"):
                    self.apply(rotate)
            with self.Session() as db:
                self.assertEqual(db.query(ServicePrincipalToken).count(), 1)
                self.assertEqual(db.query(AuditLog).count(), 1)

    def test_desktop_rotation_requires_absent_staging_and_reactivation_is_explicit(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            root.chmod(0o700)
            desktop = root / "desktop-token"
            with mock.patch.object(principal_handoff, "HANDOFF_ROOT", root):
                self.apply(self.args("provision", "desktop", desktop, "desktop.pc-02"))
                with self.assertRaisesRegex(
                    principal_handoff.HandoffError, "desktop_rotation_handoff_must_be_absent"
                ):
                    self.apply(self.args("rotate", "desktop", desktop, "desktop.pc-02"))
                self.apply(self.args("destroy-handoff", "desktop", desktop, "desktop.pc-02"))
                self.assertFalse(desktop.exists())
                self.apply(self.args("rotate", "desktop", desktop, "desktop.pc-02"))
                self.assertTrue(desktop.exists())

            acceptance = root / "acceptance-canary.token"
            with mock.patch.object(principal_handoff, "HANDOFF_ROOT", root):
                self.apply(self.args("provision", "acceptance", acceptance))
                self.apply(self.args("revoke", "acceptance", acceptance))
                self.assertFalse(acceptance.exists())
                self.apply(self.args("reactivate", "acceptance", acceptance))
                self.assertTrue(
                    self.apply(self.args("status", "acceptance", acceptance)).ready
                )
                with self.assertRaisesRegex(principal_handoff.HandoffError, "principal_not_inactive"):
                    different = self.args("reactivate", "acceptance", acceptance)
                    different.operation_id = str(uuid.uuid4())
                    self.apply(different)

    def test_emergency_revoke_is_db_first_for_missing_corrupt_and_foreign_file(self):
        scenarios = ("missing", "corrupt", "foreign")
        for scenario in scenarios:
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as temporary:
                engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
                Base.metadata.create_all(engine)
                Session = sessionmaker(bind=engine, expire_on_commit=False)
                root = Path(temporary)
                root.chmod(0o700)
                path = root / "acceptance-canary.token"
                with mock.patch.object(principal_handoff, "HANDOFF_ROOT", root):
                    provision = self.args("provision", "acceptance", path)
                    with mock.patch.dict(os.environ, {
                        "TAKSKLAD_PRINCIPAL_COMMAND_APPROVAL": "PROVISION_ACCEPTANCE_PRINCIPAL"
                    }, clear=False):
                        principal_handoff.apply_command(provision, session_factory=Session)
                    if scenario == "missing":
                        path.unlink()
                    elif scenario == "corrupt":
                        path.write_text("corrupt-synthetic\n", encoding="ascii")
                    else:
                        path.write_text("tks." + "e" * 32 + "." + "f" * 32 + "\n", encoding="ascii")
                    if path.exists():
                        path.chmod(0o600)
                    revoke = self.args("revoke", "acceptance", path)
                    with mock.patch.dict(os.environ, {
                        "TAKSKLAD_PRINCIPAL_COMMAND_APPROVAL": "REVOKE_ACCEPTANCE_PRINCIPAL"
                    }, clear=False):
                        result = principal_handoff.apply_command(revoke, session_factory=Session)
                self.assertIsInstance(result, principal_handoff.RevokeResult)
                self.assertEqual(result.cleanup, "absent" if scenario == "missing" else "unverified")
                if scenario != "missing":
                    self.assertTrue(path.exists())
                with Session() as db:
                    principal = db.execute(select(ServicePrincipal)).scalar_one()
                    self.assertFalse(principal.is_active)
                    self.assertTrue(all(
                        token.revoked_at is not None
                        for token in db.execute(select(ServicePrincipalToken)).scalars()
                    ))
                engine.dispose()

    def test_revoke_reports_foreign_temp_residue_without_deleting_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            root.chmod(0o700)
            path = root / "acceptance-canary.token"
            residue = root / ".token.foreign"
            with mock.patch.object(principal_handoff, "HANDOFF_ROOT", root):
                self.apply(self.args("provision", "acceptance", path))
                residue.write_text("tks." + "e" * 32 + "." + "f" * 32 + "\n", encoding="ascii")
                residue.chmod(0o600)
                result = self.apply(self.args("revoke", "acceptance", path))
            self.assertEqual(result.cleanup, "residue_unverified")
            self.assertTrue(residue.exists())


if __name__ == "__main__":
    unittest.main()
