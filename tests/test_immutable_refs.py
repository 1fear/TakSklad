import json
from pathlib import Path
import subprocess
import sys
import unittest

from tools.verify_immutable_refs import validate_action_line, validate_image_reference


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class ImmutableReferenceTests(unittest.TestCase):
    def test_attestation_command_defect_is_recorded_without_false_github_claim(self):
        evidence = json.loads(
            (PROJECT_ROOT / "test-artifacts/attestation-verification.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(evidence["roadmap_command_exit"], 1)
        self.assertEqual(evidence["roadmap_command_error"], "MISSING_POSITIONAL_ARTIFACT")
        self.assertIs(evidence["github_issued_bundle_present"], False)
        self.assertIs(evidence["github_identity_verified"], False)
        self.assertIs(evidence["autonomous_replacement"]["subject_bound"], True)
        self.assertIs(evidence["autonomous_replacement"]["github_identity_claimed"], False)

    def test_repository_uses_only_manifested_full_shas_and_image_digests(self):
        completed = subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "tools" / "verify_immutable_refs.py")],
            cwd=PROJECT_ROOT,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("IMMUTABLE_REFS_OK", completed.stdout)

    def test_mutable_action_and_image_examples_are_rejected(self):
        _, action_error = validate_action_line(
            "uses: actions/checkout@v4", "synthetic-workflow.yml:1"
        )
        _, image_error = validate_image_reference(
            "postgres:16-alpine", "synthetic-compose.yml:1"
        )

        self.assertIn("full lowercase 40-character commit SHA", action_error or "")
        self.assertIn("immutable sha256 digest", image_error or "")

    def test_malformed_or_uppercase_action_pin_cannot_bypass_validator(self):
        _, malformed_error = validate_action_line(
            "uses: actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683 # v4 extra",
            "synthetic-workflow.yml:1",
        )
        _, uppercase_error = validate_action_line(
            "uses: actions/checkout@11BD71901BBE5B1630CEEA73D27597364C9AF683 # v4.2.2",
            "synthetic-workflow.yml:2",
        )

        self.assertIn("malformed action reference", malformed_error or "")
        self.assertIn("full lowercase 40-character commit SHA", uppercase_error or "")

    def test_update_policy_requires_owner_approved_non_mutating_changes(self):
        manifest = json.loads(
            (PROJECT_ROOT / "supply-chain" / "immutable-refs.json").read_text(encoding="utf-8")
        )
        policy = manifest["update_policy"]
        workflow = (
            PROJECT_ROOT / ".github" / "workflows" / "immutable-reference-policy.yml"
        ).read_text(encoding="utf-8")

        self.assertEqual(policy["change_channel"], "owner-approved-pull-request")
        self.assertEqual(policy["network_mode"], "read-only-resolution")
        self.assertIs(policy["automatic_commits"], False)
        self.assertIs(policy["automatic_pushes"], False)
        self.assertIn("pull_request:", workflow)
        self.assertNotIn("schedule:", workflow)
        self.assertNotIn("workflow_dispatch:", workflow)
        self.assertNotIn("contents: write", workflow)

    def test_ci_wires_locked_install_security_sbom_and_immutable_ref_gates(self):
        workflow = (PROJECT_ROOT / ".github" / "workflows" / "ci.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn("./tools/verify_locked_installs.sh --cold --require-hashes", workflow)
        self.assertIn("./tools/security_gate.sh --synthetic-fixtures --fail-on high", workflow)
        self.assertIn("./tools/generate_sbom.sh --verify", workflow)
        self.assertIn("python3 tools/verify_immutable_refs.py", workflow)

    def test_release_workflow_publishes_and_attests_immutable_release_subjects(self):
        workflow = (
            PROJECT_ROOT / ".github" / "workflows" / "build-windows-release.yml"
        ).read_text(encoding="utf-8")

        self.assertIn("permissions: {}", workflow)
        self.assertGreaterEqual(workflow.count("attestations: write"), 2)
        self.assertGreaterEqual(workflow.count("id-token: write"), 2)
        self.assertIn("dist/TakSklad-windows-x64.zip", workflow)
        self.assertIn("dist/TakSklad.exe", workflow)
        self.assertIn("test-artifacts/sbom/", workflow)
        self.assertIn("build-container-subjects:\n    needs: build-windows", workflow)
        self.assertEqual(workflow.count("uses: docker/build-push-action@"), 2)
        self.assertEqual(workflow.count("push: true"), 2)
        self.assertIn("subject-digest: ${{ steps.backend.outputs.digest }}", workflow)
        self.assertIn("subject-digest: ${{ steps.frontend.outputs.digest }}", workflow)
        self.assertIn("push-to-registry: true", workflow)
        self.assertNotIn("dist/provenance/taksklad-backend.oci.tar", workflow)
        self.assertNotIn("dist/provenance/taksklad-frontend.oci.tar", workflow)
        self.assertGreaterEqual(workflow.count("IMMUTABLE_RELEASE_TAG_REQUIRED"), 4)
        self.assertEqual(workflow.count("ref: ${{ steps.release.outputs.tag }}"), 1)
        self.assertIn("ref: ${{ needs.build-windows.outputs.source_sha }}", workflow)
        self.assertGreaterEqual(workflow.count("fetch-tags: true"), 2)
        self.assertIn("steps.source.outputs.sha", workflow)
        self.assertNotIn("/TakSklad/main/version.json", workflow)
        self.assertIn("signer_certificate_sha256", workflow)
        self.assertGreaterEqual(workflow.count("WINDOWS_CODESIGN_IDENTITY_MISMATCH"), 2)
        self.assertIn("WINDOWS_CODESIGN_IDENTITY_NOT_PINNED", workflow)
        self.assertIn('throw "IMMUTABLE_RELEASE_TAG_VERSION_MISMATCH"', workflow)
        self.assertIn('download_url = "$releaseBaseUrl/TakSklad.exe"', workflow)
        self.assertIn(
            'download_url_onedir = "$releaseBaseUrl/TakSklad-windows-x64.zip"', workflow
        )
        self.assertIn("sha256 = $env:TAKSKLAD_EXE_SHA256", workflow)
        self.assertIn("sha256_onedir = $env:TAKSKLAD_ZIP_SHA256", workflow)
        self.assertIn('signature_type = "authenticode"', workflow)
        self.assertIn("signature_required = $true", workflow)
        self.assertIn("dist/version.json", workflow)
        self.assertGreaterEqual(workflow.count("GH_TOKEN: ${{ github.token }}"), 3)
        self.assertIn(
            "actions/attest-build-provenance@e8998f949152b193b063cb0ec769d69d929409be # v2.4.0",
            workflow,
        )

    def test_windows_attestation_is_fail_closed_behind_valid_authenticode(self):
        workflow = (
            PROJECT_ROOT / ".github" / "workflows" / "build-windows-release.yml"
        ).read_text(encoding="utf-8")
        missing_certificate = workflow.index("WINDOWS_CODESIGN_CERTIFICATE_NOT_AVAILABLE")
        transition_valid = workflow.index(
            "$signature.Status -ne [System.Management.Automation.SignatureStatus]::Valid"
        )
        onedir_valid = workflow.index(
            "$signature.Status -ne [System.Management.Automation.SignatureStatus]::Valid",
            transition_valid + 1,
        )
        attestation = workflow.index("- name: Attest Windows release subjects")

        self.assertLess(missing_certificate, transition_valid)
        self.assertLess(transition_valid, onedir_valid)
        self.assertLess(onedir_valid, attestation)
        self.assertIn("secrets.WINDOWS_CODESIGN_PFX_BASE64", workflow)
        self.assertIn("secrets.WINDOWS_CODESIGN_PFX_PASSWORD", workflow)
        self.assertNotIn("Write-Output $env:WINDOWS_CODESIGN", workflow)


if __name__ == "__main__":
    unittest.main()
