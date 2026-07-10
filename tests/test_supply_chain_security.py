import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from tools.generate_sbom import (
    SBOM_FILENAMES,
    generate,
    validate_document,
    verify,
)
from tools.security_gate import (
    SEVERITY_ORDER,
    _content_candidates,
    _synthetic_fixture_findings,
    scan_container_and_workflow_config,
    scan_dependency_integrity,
)


ROOT = Path(__file__).resolve().parents[1]


class SupplyChainSecurityTests(unittest.TestCase):
    def test_shell_gates_support_clean_checkout_python_fallback(self):
        env = {**os.environ, "PYTHON_BIN": sys.executable}
        commands = (
            ["bash", "-n", "tools/security_gate.sh"],
            ["bash", "-n", "tools/generate_sbom.sh"],
            ["bash", "tools/generate_sbom.sh", "--verify"],
        )
        for command in commands:
            with self.subTest(command=command):
                completed = subprocess.run(
                    command,
                    cwd=ROOT,
                    env=env,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_exception_schema_requires_owner_approval_reason_and_expiry(self):
        schema = json.loads(
            (ROOT / "security/vulnerability-exceptions.schema.json").read_text(
                encoding="utf-8"
            )
        )
        required = set(schema["properties"]["exceptions"]["items"]["required"])
        self.assertTrue(
            {"owner", "approved_by", "reason", "expires_on"}.issubset(required)
        )
        self.assertEqual(
            schema["properties"]["exceptions"]["items"]["properties"]["severity"]["enum"],
            ["high", "critical"],
        )

    def test_generated_sboms_have_valid_shape_and_complete_component_refs(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir) / "sbom"
            counts = generate(ROOT, output_dir)

            self.assertEqual(set(counts), set(SBOM_FILENAMES))
            self.assertEqual(counts["taksklad-desktop.cdx.json"], 35)
            self.assertEqual(counts["taksklad-backend.cdx.json"], 46)
            self.assertEqual(counts["taksklad-container-images.cdx.json"], 6)

            for filename in SBOM_FILENAMES:
                payload = json.loads((output_dir / filename).read_text(encoding="utf-8"))
                validate_document(payload)

            frontend = json.loads(
                (output_dir / "taksklad-frontend.cdx.json").read_text(encoding="utf-8")
            )
            source_lock = json.loads(
                (ROOT / "frontend/package-lock.json").read_text(encoding="utf-8")
            )
            install_paths = [
                prop["value"]
                for component in frontend["components"]
                for prop in component.get("properties", [])
                if prop.get("name") == "taksklad:install-path"
            ]
            expected_paths = [
                package_path
                for package_path, package in source_lock["packages"].items()
                if package_path and not package.get("link")
            ]
            self.assertCountEqual(install_paths, expected_paths)
            scoped = [
                component
                for component in frontend["components"]
                if component["name"].startswith("@")
            ]
            self.assertTrue(scoped)
            self.assertTrue(
                all(component["purl"].startswith("pkg:npm/%40") for component in scoped)
            )

    def test_sbom_verify_fails_closed_on_missing_or_changed_artifact(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir) / "missing"
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(verify(ROOT, output_dir), 1)

            generate(ROOT, output_dir)
            target = output_dir / "taksklad-backend.cdx.json"
            target.write_text("{}\n", encoding="utf-8")
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(verify(ROOT, output_dir), 1)

    def test_dependency_and_config_policy_have_no_high_or_critical_findings(self):
        dependency_findings, summary = scan_dependency_integrity(ROOT)
        config_findings = scan_container_and_workflow_config(ROOT)
        blocking = [
            finding
            for finding in (*dependency_findings, *config_findings)
            if SEVERITY_ORDER[finding.severity] >= SEVERITY_ORDER["high"]
        ]
        self.assertEqual(blocking, [])
        self.assertGreater(summary["desktop"], 0)
        self.assertGreater(summary["backend"], 0)
        self.assertGreater(summary["frontend"], 0)

    def test_each_synthetic_fixture_is_blocking_without_printing_values(self):
        fixtures = _synthetic_fixture_findings(ROOT)
        self.assertEqual(set(fixtures), {"secret", "forbidden-data", "sast", "dependency"})
        for findings in fixtures.values():
            self.assertTrue(
                any(
                    SEVERITY_ORDER[finding.severity] >= SEVERITY_ORDER["high"]
                    for finding in findings
                )
            )
            self.assertTrue(all("AKIA" not in finding.detail for finding in findings))

    def test_content_scanner_never_opens_nested_forbidden_paths(self):
        paths = [
            "safe/config.txt",
            "nested/backups/customer-export.txt",
            "nested/outputs/operator-report.json",
            "nested/.env.production",
            "deploy/vds/.env.example",
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            safe = root / paths[0]
            safe.parent.mkdir(parents=True)
            safe.write_text("safe", encoding="utf-8")
            candidates = list(_content_candidates(root, paths))

        self.assertEqual([relative for relative, _ in candidates], [paths[0]])


if __name__ == "__main__":
    unittest.main()
