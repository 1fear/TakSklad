import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
import unittest
import uuid
from unittest import mock

from tools import (
    validate_fresh_principal_backup,
    validate_principal_admin_network,
    validate_principal_provisioner_compose,
    validate_principal_schema_identity,
)


ROOT = Path(__file__).resolve().parents[1]
CANONICAL_TEMP_ROOT = "/private/tmp" if Path("/private/tmp").is_dir() else None


class PrincipalProvisioningGuardTests(unittest.TestCase):
    def test_new_security_tools_have_real_runtime_callers_after_workflow_scope_reset(self):
        callers = {
            "tools/credentialed_returns_canary.py": "deploy/vds/deploy_from_git.sh",
            "tools/package_windows_release_zip.py": ".github/workflows/build-windows-release.yml",
            "tools/validate_auth_canary_token_file.py": "deploy/vds/deploy_from_git.sh",
            "tools/validate_fresh_principal_backup.py": "deploy/vds/provision_service_principal.sh",
            "tools/validate_principal_admin_network.py": "deploy/vds/provision_service_principal.sh",
            "tools/validate_principal_handoff_residue.py": "deploy/vds/provision_service_principal.sh",
            "tools/validate_principal_provisioner_compose.py": "deploy/vds/provision_service_principal.sh",
            "tools/validate_principal_schema_identity.py": "deploy/vds/provision_service_principal.sh",
            "tools/verify_windows_release_zip.py": "tools/verify_release_attestations.sh",
        }
        for tool, caller in callers.items():
            with self.subTest(tool=tool):
                self.assertTrue((ROOT / tool).is_file())
                self.assertIn(
                    Path(tool).name,
                    (ROOT / caller).read_text(encoding="utf-8"),
                )
        workflow = (ROOT / ".github/workflows/deploy-production.yml").read_text(
            encoding="utf-8"
        )
        for stale in (
            "principal_action",
            "principal-status",
            "principal-maintenance",
            "provision_service_principal.sh",
        ):
            self.assertNotIn(stale, workflow)

    def test_manual_bridge_runbooks_require_external_attestation_and_no_workflow_automation(self):
        documents = "\n".join(
            (ROOT / path).read_text(encoding="utf-8")
            for path in (
                "docs/deploy-rollback-runbook.md",
                "docs/vds-release-readiness.md",
            )
        )
        self.assertIn("verify_release_attestations.sh --sha", documents)
        self.assertIn("docker login --password-stdin", documents)
        self.assertIn("--pull never", documents)
        self.assertIn("manual P0 bridge", documents)
        for stale in (
            "TAKSKLAD_PROTECTED_WORKFLOW_PRINCIPAL_JOB",
            "TAKSKLAD_PRINCIPAL_BACKUP_CONFIRMED",
            "principal-maintenance",
            "principal_action",
        ):
            self.assertNotIn(stale, documents)

    def test_manual_p0_gate_is_exact_and_blocks_before_docker_or_file_mutation(self):
        image = "ghcr.io/1fear/taksklad-backend@sha256:" + "a" * 64
        operation_id = "6bb40555-4bb4-4daa-8a44-30d216860a7f"
        source_sha = "b" * 40
        release_tag = "v2.0.43"
        command = "provision"
        kind = "acceptance"
        identifier = "acceptance.release"
        approval = (
            f"MANUAL_P0_BRIDGE:{command}:{kind}:{identifier}:{operation_id}:"
            f"{source_sha}:{release_tag}:{image}:BACKUP:{operation_id}"
        )
        authority = f"VERIFIED_TAGGED_MAIN_RELEASE:{release_tag}:{source_sha}:{image}"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            docker_marker = root / "docker-called"
            fake_docker = root / "docker"
            fake_docker.write_text(
                f"#!/bin/sh\nprintf called > {docker_marker}\nexit 91\n",
                encoding="utf-8",
            )
            fake_docker.chmod(0o700)
            base = {
                **os.environ,
                "PATH": f"{root}:/usr/bin:/bin",
                "TAKSKLAD_PRINCIPAL_BACKUP_ROOT": "/protected/backups/completed",
                "TAKSKLAD_PRINCIPAL_BACKUP_RESULT_FILE": "/protected/results/result.json",
                "TAKSKLAD_PRINCIPAL_BACKUP_ARCHIVE_FILE": "/protected/backups/exact.dump",
                "TAKSKLAD_MANUAL_P0_BRIDGE_APPROVAL": approval,
                "TAKSKLAD_MANUAL_P0_RELEASE_AUTHORITY": authority,
                "TAKSKLAD_PRINCIPAL_WRITE_APPROVAL": "ALLOW_SERVICE_PRINCIPAL_WRITE",
                "TAKSKLAD_PRINCIPAL_COMMAND_APPROVAL": "PROVISION_ACCEPTANCE_PRINCIPAL",
            }
            script = ROOT / "deploy/vds/provision_service_principal.sh"
            argv = [
                str(script), image, command, kind, identifier, operation_id, source_sha, release_tag
            ]
            protected_names = (
                "TAKSKLAD_MANUAL_P0_BRIDGE_APPROVAL",
                "TAKSKLAD_MANUAL_P0_RELEASE_AUTHORITY",
                "TAKSKLAD_PRINCIPAL_WRITE_APPROVAL",
                "TAKSKLAD_PRINCIPAL_COMMAND_APPROVAL",
                "TAKSKLAD_PRINCIPAL_BACKUP_RESULT_FILE",
            )
            for name in protected_names:
                with self.subTest(name=name):
                    environment = dict(base)
                    environment[name] = "WRONG"
                    completed = subprocess.run(
                        argv,
                        cwd=ROOT,
                        env=environment,
                        text=True,
                        capture_output=True,
                        check=False,
                        timeout=5,
                    )
                    self.assertNotEqual(completed.returncode, 0)
                    self.assertFalse(docker_marker.exists())
                    self.assertNotIn(image, completed.stdout + completed.stderr)

            environment = dict(base)
            environment.pop("TAKSKLAD_MANUAL_P0_BRIDGE_APPROVAL")
            environment["TAKSKLAD_PROTECTED_WORKFLOW_PRINCIPAL_JOB"] = "1"
            completed = subprocess.run(
                argv,
                cwd=ROOT,
                env=environment,
                text=True,
                capture_output=True,
                check=False,
                timeout=5,
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertFalse(docker_marker.exists())

    def test_manual_bridge_validates_schema_and_exact_backup_before_topology_mutation(self):
        script_path = ROOT / "deploy/vds/provision_service_principal.sh"
        self.assertTrue(os.access(script_path, os.X_OK))
        script = script_path.read_text(encoding="utf-8")
        schema = script.index("validate_principal_schema_identity.py")
        backup = script.index("validate_operation_backup ||", schema)
        network_create = script.index("docker network create")
        one_shot = script.index("principal-provisioner ", network_create)
        self.assertLess(schema, backup)
        self.assertLess(backup, network_create)
        self.assertLess(backup, one_shot)
        self.assertIn("--pull never", script[:backup])
        self.assertNotIn("TAKSKLAD_PROTECTED_WORKFLOW_PRINCIPAL_JOB", script)
        self.assertNotIn("TAKSKLAD_PRINCIPAL_BACKUP_CONFIRMED", script)

    def test_manual_bridge_stat_probe_is_single_value_on_gnu_and_bsd(self):
        script = (ROOT / "deploy/vds/provision_service_principal.sh").read_text(
            encoding="utf-8"
        )
        functions = script[script.index("stat_uid() {") : script.index("handoff_parent_safe() {")]
        with tempfile.TemporaryDirectory(dir=CANONICAL_TEMP_ROOT) as temporary:
            root = Path(temporary)
            target = root / "protected"
            target.mkdir()
            for implementation in ("gnu", "bsd"):
                if implementation == "gnu":
                    fake_stat = """stat() {
  if [ "$1" = "-c" ]; then
    [ "$2" = "%u" ] && printf '501\\n' || printf '700\\n'
    return 0
  fi
  printf 'filesystem report must not leak\\n'
  return 1
}
"""
                else:
                    fake_stat = """stat() {
  if [ "$1" = "-c" ]; then return 1; fi
  if [ "$1" = "-f" ] && [ "$2" = "%u" ]; then printf '501\\n'; return 0; fi
  if [ "$1" = "-f" ] && [ "$2" = "%Lp" ]; then printf '700\\n'; return 0; fi
  return 1
}
"""
                completed = subprocess.run(
                    ["bash", "-c", fake_stat + functions + '\nprintf "%s|%s\\n" "$(stat_uid "$1")" "$(stat_mode "$1")"', "bash", str(target)],
                    env=os.environ,
                    text=True,
                    capture_output=True,
                    check=False,
                    timeout=5,
                )
                with self.subTest(implementation=implementation):
                    self.assertEqual(completed.returncode, 0, completed.stderr)
                    self.assertEqual(completed.stdout, "501|700\n")
                    self.assertNotIn("filesystem report", completed.stdout + completed.stderr)

    def test_schema_identity_requires_one_exact_candidate_head(self):
        current = "20260716_0019"
        self.assertEqual(
            validate_principal_schema_identity.validate(current, current + " (head)\n"),
            current,
        )
        for rendered in (
            "",
            "20260716_0018 (head)\n",
            "20260716_0020 (head)\n",
            current + " (head)\n20260716_0020 (head)\n",
            "unsafe;revision (head)\n",
        ):
            with self.subTest(rendered=rendered), self.assertRaises(ValueError):
                validate_principal_schema_identity.validate(current, rendered)

    def backup_fixture(self, temporary, *, age_seconds=0):
        now = datetime(2026, 7, 17, 12, 0, tzinfo=timezone.utc)
        root = Path(temporary) / "completed"
        root.mkdir(mode=0o700)
        bundle = root / "taksklad-postgres-exact"
        bundle.mkdir(mode=0o700)
        archive = bundle / "exact.dump"
        archive.write_bytes(b"PGDMPsynthetic")
        archive.chmod(0o600)
        restore_list = bundle / "exact.list"
        restore_list.write_text("synthetic restore list\n", encoding="utf-8")
        restore_list.chmod(0o600)
        archive_sha = hashlib.sha256(archive.read_bytes()).hexdigest()
        list_sha = hashlib.sha256(restore_list.read_bytes()).hexdigest()
        created = now - timedelta(seconds=age_seconds)
        manifest = bundle / "exact.manifest.json"
        manifest.write_text(json.dumps({
            "schema_version": 2,
            "created_at_utc": created.isoformat(),
            "atomic_bundle": True,
            "actual_postgresql": True,
            "source": "postgresql",
            "migration_head": "20260716_0019",
            "archive": {
                "filename": archive.name,
                "format": "postgresql-custom",
                "sha256": archive_sha,
                "bytes": archive.stat().st_size,
                "validated": True,
                "list": {
                    "filename": restore_list.name,
                    "sha256": list_sha,
                    "validated": True,
                },
            },
        }), encoding="utf-8")
        manifest.chmod(0o600)
        operation_id = str(uuid.uuid4())
        result = Path(temporary) / "result.json"
        result.write_text(json.dumps({
            "schema": 1,
            "operation_id": operation_id,
            "manifest_path": str(manifest),
            "archive_path": str(archive),
            "archive_sha256": archive_sha,
            "archive_bytes": archive.stat().st_size,
            "migration_head": "20260716_0019",
            "created_at_utc": created.isoformat(),
        }), encoding="utf-8")
        result.chmod(0o600)
        return root, result, operation_id, archive, now

    def test_fresh_backup_binds_exact_result_not_concurrent_newest(self):
        with tempfile.TemporaryDirectory(dir=CANONICAL_TEMP_ROOT) as temporary:
            root, result, operation_id, _archive, now = self.backup_fixture(temporary)
            foreign = root / "taksklad-postgres-zzzz-newer"
            foreign.mkdir(mode=0o700)
            (foreign / "foreign.manifest.json").write_text("{}", encoding="utf-8")
            validate_fresh_principal_backup.validate(
                root,
                result,
                operation_id,
                "20260716_0019",
                now=now,
                expected_archive=_archive,
            )

    def test_fresh_backup_rejects_tamper_delete_stale_head_and_operation_mismatch(self):
        mutations = (
            "tamper",
            "delete",
            "stale",
            "head",
            "operation",
            "result-bytes",
            "result-created",
            "result-head",
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory(dir=CANONICAL_TEMP_ROOT) as temporary:
                age = 901 if mutation == "stale" else 0
                root, result, operation_id, archive, now = self.backup_fixture(temporary, age_seconds=age)
                head = "wrong_head" if mutation == "head" else "20260716_0019"
                op = str(uuid.uuid4()) if mutation == "operation" else operation_id
                if mutation == "tamper":
                    archive.write_bytes(b"PGDMPtampered")
                elif mutation == "delete":
                    archive.unlink()
                elif mutation in {"result-bytes", "result-created", "result-head"}:
                    payload = json.loads(result.read_text(encoding="utf-8"))
                    if mutation == "result-bytes":
                        payload["archive_bytes"] += 1
                    elif mutation == "result-created":
                        payload["created_at_utc"] = "2026-07-17T11:59:59+00:00"
                    else:
                        payload["migration_head"] = "20260716_0018"
                    result.write_text(json.dumps(payload), encoding="utf-8")
                with self.assertRaises((ValueError, OSError)):
                    validate_fresh_principal_backup.validate(root, result, op, head, now=now)

    def test_backup_rejects_explicit_archive_path_mismatch(self):
        with tempfile.TemporaryDirectory(dir=CANONICAL_TEMP_ROOT) as temporary:
            root, result, operation_id, archive, now = self.backup_fixture(temporary)
            foreign = archive.with_name("foreign.dump")
            foreign.write_bytes(b"PGDMPforeign")
            foreign.chmod(0o600)
            with self.assertRaisesRegex(ValueError, "expected_archive_mismatch"):
                validate_fresh_principal_backup.validate(
                    root,
                    result,
                    operation_id,
                    "20260716_0019",
                    now=now,
                    expected_archive=foreign,
                )

    def test_backup_paths_are_canonical_regular_and_contained(self):
        with tempfile.TemporaryDirectory(dir=CANONICAL_TEMP_ROOT) as temporary:
            root, result, operation_id, archive, now = self.backup_fixture(temporary)
            validated = validate_fresh_principal_backup.validate(
                root,
                result,
                operation_id,
                "20260716_0019",
                now=now,
                expected_archive=archive,
            )
            self.assertEqual(validated["root"], root)
            self.assertEqual(validated["archive"], archive)

            with self.subTest("dotdot-root"), self.assertRaises(ValueError):
                validate_fresh_principal_backup.validate(
                    root / ".." / root.name,
                    result,
                    operation_id,
                    "20260716_0019",
                    now=now,
                    expected_archive=archive,
                )

            root_link = Path(temporary) / "completed-link"
            root_link.symlink_to(root, target_is_directory=True)
            with self.subTest("symlink-root"), self.assertRaises(ValueError):
                validate_fresh_principal_backup.validate(
                    root_link,
                    result,
                    operation_id,
                    "20260716_0019",
                    now=now,
                    expected_archive=archive,
                )

            result_link = Path(temporary) / "result-link.json"
            result_link.symlink_to(result)
            with self.subTest("symlink-result"), self.assertRaises(ValueError):
                validate_fresh_principal_backup.validate(
                    root,
                    result_link,
                    operation_id,
                    "20260716_0019",
                    now=now,
                    expected_archive=archive,
                )

            bundle = archive.parent
            bundle_link = root / "bundle-link"
            bundle_link.symlink_to(bundle, target_is_directory=True)
            payload = json.loads(result.read_text(encoding="utf-8"))
            payload["manifest_path"] = str(bundle_link / "exact.manifest.json")
            payload["archive_path"] = str(bundle_link / "exact.dump")
            result.write_text(json.dumps(payload), encoding="utf-8")
            with self.subTest("symlink-intermediate"), self.assertRaises(ValueError):
                validate_fresh_principal_backup.validate(
                    root,
                    result,
                    operation_id,
                    "20260716_0019",
                    now=now,
                    expected_archive=archive,
                )

    def test_backup_rejects_archive_outside_canonical_root(self):
        with tempfile.TemporaryDirectory(dir=CANONICAL_TEMP_ROOT) as temporary:
            root, result, operation_id, _archive, now = self.backup_fixture(temporary)
            outside = Path(temporary) / "outside.dump"
            outside.write_bytes(b"PGDMPoutside")
            outside.chmod(0o600)
            payload = json.loads(result.read_text(encoding="utf-8"))
            payload["archive_path"] = str(outside)
            result.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "result_path_invalid"):
                validate_fresh_principal_backup.validate(
                    root,
                    result,
                    operation_id,
                    "20260716_0019",
                    now=now,
                    expected_archive=outside,
                )

    def network_payload(self):
        postgres_id = "a" * 64
        operation_id = "12345678-1234-4234-9234-123456789abc"
        return [{
            "Name": "taksklad-principal-12345678123442349234123456789abc",
            "Driver": "bridge",
            "Scope": "local",
            "Internal": True,
            "Ingress": False,
            "Attachable": False,
            "Options": {},
            "Labels": {
                "com.taksklad.principal.owner": "taksklad",
                "com.taksklad.principal.operation": operation_id,
            },
            "Containers": {postgres_id: {"Name": "current-postgres"}},
        }], postgres_id, operation_id

    def run_network_validator(self, payload, postgres_id, operation_id):
        output = io.StringIO()
        error = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO(json.dumps(payload))), mock.patch("sys.stdout", output), mock.patch("sys.stderr", error):
            status = validate_principal_admin_network.main([
                "taksklad-principal-12345678123442349234123456789abc",
                "taksklad",
                postgres_id,
                operation_id,
            ])
        return status, output.getvalue() + error.getvalue()

    def test_network_validator_exact_boundary_and_impostors(self):
        payload, postgres_id, operation_id = self.network_payload()
        self.assertEqual(self.run_network_validator(payload, postgres_id, operation_id)[0], 0)
        cases = {
            "overlay": ("Driver", "overlay"),
            "attachable": ("Attachable", True),
            "options": ("Options", {"com.docker.network.bridge.enable_ip_masquerade": "true"}),
            "external": ("Internal", False),
            "member": ("Containers", {postgres_id: {}, "b" * 64: {}}),
        }
        for label, (field, value) in cases.items():
            with self.subTest(label=label):
                changed = json.loads(json.dumps(payload))
                changed[0][field] = value
                status, rendered = self.run_network_validator(changed, postgres_id, operation_id)
                self.assertNotEqual(status, 0)
                self.assertNotIn("token", rendered.lower())

    def compose_payload(self):
        image = "ghcr.io/1fear/taksklad-backend@sha256:" + "a" * 64
        service = {
            "init": True,
            "read_only": True,
            "cap_drop": ["ALL"],
            "security_opt": ["no-new-privileges:true"],
            "tmpfs": ["/tmp:rw,noexec,nosuid,nodev,size=64m"],
            "pids_limit": 256,
            "mem_limit": 805306368,
            "cpus": 1.0,
            "logging": {"driver": "json-file", "options": {"max-size": "10m", "max-file": "3"}},
            "profiles": ["principal-admin"],
            "image": image,
            "restart": "no",
            "user": "501:20",
            "entrypoint": ["python", "-m", "app.principal_handoff"],
            "environment": {
                "DATABASE_URL": "postgresql+psycopg://synthetic:encoded@postgres:5432/taksklad_test",
                "TAKSKLAD_PRINCIPAL_COMMAND_APPROVAL": "",
                "TAKSKLAD_PRINCIPAL_HANDOFF_ROOT": "/run/taksklad-private",
            },
            "depends_on": {"postgres": {"condition": "service_healthy", "required": True}},
            "networks": {"principal-admin": None},
            "volumes": [{
                "type": "bind",
                "source": "/opt/stacks/taksklad/private",
                "target": "/run/taksklad-private",
            }],
            "labels": {},
        }
        payload = {
            "name": "taksklad",
            "services": {
                "postgres": {"networks": {"taksklad-internal": None}},
                "backend-api": {"networks": {"taksklad-internal": None, "traefik": None}},
                "principal-provisioner": service,
            },
            "networks": {
                "principal-admin": {
                    "name": "taksklad-principal-12345678123442349234123456789abc",
                    "external": True,
                },
                "taksklad-internal": {"name": "taksklad_taksklad-internal"},
            },
        }
        return payload, image

    def test_compose_validator_rejects_any_privilege_secret_or_topology_drift(self):
        payload, image = self.compose_payload()
        self.assertEqual(
            validate_principal_provisioner_compose.validate(
                payload, image, "501", "20", "/opt/stacks/taksklad/private"
            ),
            ("taksklad", "taksklad-principal-12345678123442349234123456789abc"),
        )
        mutations = {
            "labels": lambda p: p["services"]["principal-provisioner"].update(labels={"traefik.enable": "true"}),
            "ports": lambda p: p["services"]["principal-provisioner"].update(ports=[{"published": 8000}]),
            "extra-env": lambda p: p["services"]["principal-provisioner"]["environment"].update(TELEGRAM_TOKEN="synthetic"),
            "extra-network": lambda p: p["services"]["principal-provisioner"]["networks"].update(traefik=None),
            "not-readonly": lambda p: p["services"]["principal-provisioner"].update(read_only=False),
            "capability": lambda p: p["services"]["principal-provisioner"].update(cap_drop=[]),
            "profile": lambda p: p["services"]["principal-provisioner"].update(profiles=[]),
            "other-member": lambda p: p["services"]["backend-api"]["networks"].update(**{"principal-admin": None}),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label):
                changed = json.loads(json.dumps(payload))
                mutate(changed)
                with self.assertRaises(ValueError):
                    validate_principal_provisioner_compose.validate(
                        changed, image, "501", "20", "/opt/stacks/taksklad/private"
                    )

if __name__ == "__main__":
    unittest.main()
