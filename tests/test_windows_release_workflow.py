import json
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class WindowsReleaseWorkflowTests(unittest.TestCase):
    def test_windows_signing_contract_is_fail_closed_and_marks_production_gate(self):
        contract = json.loads(
            (PROJECT_ROOT / "test-artifacts" / "windows-signing-contract.json").read_text(
                encoding="utf-8"
            )
        )

        self.assertEqual(contract["signature_type"], "authenticode")
        self.assertEqual(contract["required_status"], "Valid")
        self.assertEqual(contract["verification_api"], "Get-AuthenticodeSignature")
        self.assertEqual(contract["signer_identity"], "certificate-sha256")
        self.assertEqual(contract["trusted_signer_certificate_sha256"], [])
        self.assertIs(contract["production_signer_pinned"], False)
        self.assertIs(contract["synthetic_test_only"], True)
        self.assertEqual(
            contract["production_gate"],
            "WINDOWS_CODESIGN_CERTIFICATE_NOT_AVAILABLE",
        )

        workflow = (PROJECT_ROOT / ".github/workflows/build-windows-release.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("WINDOWS_CODESIGN_CERTIFICATE_SHA256", workflow)
        self.assertGreaterEqual(workflow.count("WINDOWS_CODESIGN_IDENTITY_MISMATCH"), 2)
        self.assertIn("WINDOWS_CODESIGN_IDENTITY_NOT_PINNED", workflow)
        self.assertIn("taksklad-internal-windows-codesign.pem", workflow)
        self.assertIn("taksklad-internal-windows-root-ca.pem", workflow)
        self.assertIn("python -m tools.verify_windows_signing_chain --require-leaf", workflow)
        self.assertIn("Get-Command certmgr.exe", workflow)
        self.assertIn("Get-Command signtool.exe", workflow)
        self.assertGreaterEqual(workflow.count('"$env:WINDOWS_SIGNTOOL_PATH" sign'), 2)
        self.assertIn('/r localMachine root', workflow)
        self.assertIn('/r localMachine trustedpublisher', workflow)
        self.assertGreaterEqual(workflow.count('/all /s /r localMachine'), 2)
        self.assertIn("Cert:\\LocalMachine\\Root", workflow)
        self.assertIn("Cert:\\LocalMachine\\TrustedPublisher", workflow)
        self.assertIn("timeout-minutes: 2", workflow)
        self.assertIn("WINDOWS_CODESIGN_TEMPORARY_ROOT_THUMBPRINT", workflow)
        self.assertIn("WINDOWS_CODESIGN_TEMPORARY_PUBLISHER_THUMBPRINT", workflow)
        self.assertGreaterEqual(workflow.count("SignerCertificate.RawData"), 2)
        self.assertGreaterEqual(workflow.count("signer_certificate_sha256"), 2)
        self.assertIn("Verify clean-workstation pinned Authenticode status", workflow)
        self.assertIn("WINDOWS_CODESIGN_CLEAN_HOST_IDENTITY_MISMATCH", workflow)
        self.assertIn("SignatureStatus]::NotTrusted", workflow)
        self.assertIn("SignatureStatus]::UnknownError", workflow)
        self.assertIn("X509RevocationMode]::NoCheck", workflow)
        self.assertIn("WINDOWS_CODESIGN_CLEAN_HOST_CHAIN_UNEXPECTED", workflow)
        self.assertIn("@('PartialChain', 'UntrustedRoot')", workflow)
        self.assertIn("$acceptedChainStatuses -notcontains $chainStatuses[0]", workflow)

    def test_windows_release_collects_taksklad_package_and_smoke_tests_exe(self):
        workflow = (PROJECT_ROOT / ".github" / "workflows" / "build-windows-release.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn("--collect-submodules=taksklad", workflow)
        self.assertIn('--add-data "assets\\product_images;assets\\product_images"', workflow)
        self.assertIn("pyinstaller_entry.py", workflow)
        self.assertIn("pyinstaller_auth_entry.py", workflow)
        self.assertIn("--onefile --console", workflow)
        self.assertIn("TakSkladAuth.exe", workflow)
        self.assertIn("$env:PYTHONPATH = $srcPath", workflow)
        self.assertIn("PYTHONPATH=$srcPath", workflow)
        self.assertIn("Rename-Item taksklad taksklad_bridge_disabled", workflow)
        self.assertIn("import taksklad, taksklad.main", workflow)
        self.assertIn("Smoke test transition exe imports from clean directory", workflow)
        self.assertIn("RUNNER_TEMP", workflow)
        self.assertIn(".\\TakSklad.exe --smoke-import", workflow)
        self.assertIn(".\\TakSklad.exe --smoke-gui", workflow)
        self.assertIn(".\\TakSklad\\TakSklad.exe --smoke-import", workflow)
        self.assertIn(".\\TakSklad\\TakSklad.exe --smoke-gui", workflow)

    def test_windows_release_does_not_smoke_test_from_checkout_root(self):
        workflow = (PROJECT_ROOT / ".github" / "workflows" / "build-windows-release.yml").read_text(
            encoding="utf-8"
        )

        self.assertNotIn("& dist\\transition\\TakSklad.exe --smoke-import", workflow)
        self.assertNotIn("& dist\\onedir\\TakSklad\\TakSklad.exe --smoke-import", workflow)

    def test_windows_release_manifest_binds_version_source_lock_and_artifact_hashes(self):
        workflow = (PROJECT_ROOT / ".github" / "workflows" / "build-windows-release.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn("latest_version = $env:TAKSKLAD_APP_VERSION", workflow)
        self.assertIn("source_sha = $env:SOURCE_SHA", workflow)
        self.assertIn("dependency_lock_sha256 = $env:TAKSKLAD_DESKTOP_LOCK_SHA256", workflow)
        self.assertIn("artifact_sha256 = $env:TAKSKLAD_EXE_SHA256", workflow)
        self.assertIn("artifact_sha256_onedir = $env:TAKSKLAD_ZIP_SHA256", workflow)
        self.assertIn("WINDOWS_SOURCE_SHA_MISMATCH", workflow)
        self.assertIn(
            '"auth_helper_sha256_onedir", "app_sha256_onedir",',
            workflow,
        )
        self.assertIn('"acceptance_wrapper_sha256", "dependency_lock_sha256",', workflow)
        self.assertIn('"acceptance_wrapper": windows["acceptance_wrapper"]', workflow)
        self.assertIn('"app_sha256_onedir": windows["app_sha256_onedir"]', workflow)
        self.assertIn('"version": windows_version', workflow)
        self.assertIn('"artifact_sha256": windows_exe_sha256', workflow)
        self.assertIn('"artifact_sha256_onedir": windows_onedir_sha256', workflow)
        self.assertIn('"dependency_lock_sha256": windows["dependency_lock_sha256"]', workflow)
        self.assertIn('"app_sha256_onedir": windows["app_sha256_onedir"]', workflow)
        self.assertIn('"acceptance_wrapper": windows["acceptance_wrapper"]', workflow)
        self.assertIn(
            '"acceptance_wrapper_sha256": windows["acceptance_wrapper_sha256"]',
            workflow,
        )
        self.assertIn("tools\\package_windows_release_zip.py", workflow)
        self.assertNotIn("Compress-Archive", workflow)

    def test_release_build_requires_exact_tag_draft_and_successful_ci_gate(self):
        workflow = (PROJECT_ROOT / ".github/workflows/build-windows-release.yml").read_text(
            encoding="utf-8"
        )

        self.assertNotIn("types: [published]", workflow)
        self.assertIn("ci_run_id:", workflow)
        self.assertIn("refs/tags/$env:RELEASE_TAG", workflow)
        self.assertIn("IMMUTABLE_WORKFLOW_SHA_MISMATCH", workflow)
        self.assertIn("Require exact successful CI Release gate", workflow)
        self.assertIn('metadata.get("workflowName") != "CI"', workflow)
        self.assertIn('job.get("name") == "Release gate"', workflow)
        self.assertIn("CI_RELEASE_GATE_NOT_SUCCESSFUL", workflow)
        self.assertIn("Require existing empty draft release", workflow)
        self.assertIn('release.get("isDraft") is not True', workflow)
        self.assertIn("IMMUTABLE_RELEASE_ASSET_ALREADY_EXISTS", workflow)
        self.assertIn("taksklad-release-${{ inputs.tag }}", workflow)
        self.assertIn("Refuse to overwrite existing image tags", workflow)
        self.assertIn("IMMUTABLE_IMAGE_TAG_ALREADY_EXISTS", workflow)
        self.assertIn("IMMUTABLE_IMAGE_TAG_ABSENCE_UNVERIFIED", workflow)
        self.assertNotIn("--clobber", workflow)
        self.assertIn('gh release upload "$RELEASE_TAG" --repo "$GITHUB_REPOSITORY"', workflow)
        self.assertIn('gh release edit "$RELEASE_TAG" --repo "$GITHUB_REPOSITORY" --draft=false --verify-tag', workflow)

    def test_unified_manifest_binds_ci_and_all_immutable_subjects(self):
        workflow = (PROJECT_ROOT / ".github/workflows/build-windows-release.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn('"workflow": "CI"', workflow)
        self.assertIn('"run_id": int(os.environ["CI_RUN_ID"])', workflow)
        self.assertIn('"head_sha": source_sha', workflow)
        self.assertIn('"required_check": "Release gate"', workflow)
        self.assertIn('"artifact": windows_exe', workflow)
        self.assertIn('"auth_helper": windows_auth_helper', workflow)
        self.assertIn('"artifact_onedir": windows_onedir', workflow)
        self.assertIn('"manifest": windows_manifest', workflow)
        self.assertIn('{"kind": "windows", "name": windows_exe, "sha256": windows_exe_sha256}', workflow)
        self.assertIn('{"kind": "windows", "name": windows_auth_helper, "sha256": windows_auth_helper_sha256}', workflow)
        self.assertIn('{"kind": "windows", "name": windows_onedir, "sha256": windows_onedir_sha256}', workflow)
        self.assertIn('{"kind": "windows", "name": windows_manifest', workflow)
        self.assertEqual(workflow.count('{"kind": "oci", "name":'), 2)

    def test_production_deploy_reverifies_ci_tag_windows_and_immutable_images(self):
        workflow = (PROJECT_ROOT / ".github/workflows/deploy-production.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn("IMMUTABLE_RELEASE_TAG_VERSION_MISMATCH", workflow)
        self.assertIn("CI_RELEASE_GATE_NOT_SUCCESSFUL", workflow)
        self.assertIn("WINDOWS_ATTESTATION_SUBJECTS_MISMATCH", workflow)
        self.assertIn("OCI_ATTESTATION_SUBJECTS_MISMATCH", workflow)
        self.assertIn('expected_name = f"ghcr.io/{sys.argv[4].split(\'/\', 1)[0]}/taksklad-{service}"', workflow)
        self.assertIn("WINDOWS_SUBJECT_SHA256_MISMATCH", workflow)
        self.assertIn('for subject in TakSklad.exe TakSkladAuth.exe TakSklad-windows-x64.zip version.json', workflow)
        self.assertIn("RELEASE_TAG_SOURCE_SHA_MISMATCH", workflow)


if __name__ == "__main__":
    unittest.main()
