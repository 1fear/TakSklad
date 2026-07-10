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
        self.assertGreaterEqual(workflow.count("SignerCertificate.RawData"), 2)
        self.assertGreaterEqual(workflow.count("signer_certificate_sha256"), 2)

    def test_windows_release_collects_taksklad_package_and_smoke_tests_exe(self):
        workflow = (PROJECT_ROOT / ".github" / "workflows" / "build-windows-release.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn("--collect-submodules=taksklad", workflow)
        self.assertIn('--add-data "assets\\product_images;assets\\product_images"', workflow)
        self.assertIn("pyinstaller_entry.py", workflow)
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


if __name__ == "__main__":
    unittest.main()
