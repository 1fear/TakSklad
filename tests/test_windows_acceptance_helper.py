from pathlib import Path
import unittest


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "tools" / "windows_backend_acceptance.ps1"


class WindowsAcceptanceHelperTest(unittest.TestCase):
    def test_operator_docs_require_attested_safe_extract_and_packaged_wrapper(self):
        root = SCRIPT_PATH.parents[1]
        documents = "\n".join(
            (root / path).read_text(encoding="utf-8")
            for path in (
                "docs/windows-backend-acceptance.md",
                "docs/manual-acceptance-runbook.md",
            )
        )
        self.assertIn("verify_release_attestations.sh", documents)
        self.assertIn("--extract-windows-to", documents)
        self.assertIn("TakSklad\\windows_backend_acceptance.ps1", documents)
        self.assertNotIn(".\\tools\\windows_backend_acceptance.ps1", documents)
        self.assertNotIn("Expand-Archive", documents)

    def test_helper_contains_expected_backend_flags(self):
        script = SCRIPT_PATH.read_text(encoding="utf-8")

        self.assertIn("https://api.taksklad.uz", script)
        self.assertIn("--auth-canary", script)
        self.assertIn("current-user DPAPI", script)
        self.assertIn('$MinAppVersion = "2.0.0"', script)
        self.assertIn('$ExpectedAppVersion = ""', script)
        self.assertIn('$ExpectedBuildLabel = "MVP 2.0"', script)
        self.assertIn("Compare-TakSkladVersion", script)
        self.assertIn("SkipAppVersionCheck", script)
        self.assertIn("SkipBuildLabelCheck", script)
        self.assertIn("APP_VERSION", script)
        self.assertIn("APP_BUILD_LABEL", script)
        self.assertIn("app_build_label", script)
        self.assertIn("build_manifest.json", script)
        self.assertIn("Cannot verify TakSklad.exe because", script)
        self.assertNotIn(".venv\\Scripts\\python.exe", script)
        self.assertIn("TAKSKLAD_BACKEND_ENABLED", script)
        self.assertIn("TAKSKLAD_BACKEND_READ_ORDERS_ENABLED", script)
        self.assertIn("TAKSKLAD_BACKEND_BASE_URL", script)
        self.assertIn("TAKSKLAD_BACKEND_TIMEOUT_SECONDS", script)
        self.assertIn("Remove-Item", script)
        self.assertNotIn("[string]$Token", script)
        self.assertEqual(script.count('"TAKSKLAD_BACKEND_API_TOKEN"'), 1)
        self.assertNotIn("$env:TAKSKLAD_BACKEND_API_TOKEN", script)
        self.assertIn("$LegacyCleanupOnlyEnvNames", script)
        self.assertNotIn("Authorization", script)
        self.assertNotIn("-Token", script)
        self.assertNotIn("[string]$AuthHelperPath", script)
        self.assertIn('"TakSkladAuth.exe"', script)
        self.assertIn("Assert-TakSkladAuthHelperIntegrity", script)
        self.assertIn("Production DPAPI acceptance is packaged-only", script)
        self.assertNotIn("UsePython", script)
        self.assertIn("$PinnedProductionSignerCertificateSha256", script)
        self.assertIn("SignatureStatus]::Valid", script)
        self.assertIn("SignatureStatus]::NotTrusted", script)
        self.assertIn("SignatureStatus]::UnknownError", script)
        self.assertIn("@('PartialChain', 'UntrustedRoot')", script)
        self.assertIn("$ChainStatuses.Count -ne 1", script)
        self.assertIn("$AcceptedChainStatuses -notcontains $ChainStatuses[0]", script)
        self.assertIn("$AcceptedSignatureStatuses -notcontains $Signature.Status", script)
        self.assertEqual(script.count("Assert-TakSkladPinnedAuthenticodeSignature -ArtifactPath"), 2)
        self.assertNotIn(".\\tools\\windows_backend_acceptance.ps1", script)
        self.assertIn("TAKSKLAD_DESKTOP_PRINCIPAL_IDENTIFIER", script)
        self.assertIn("[string]$PrincipalIdentifier", script)
        self.assertIn("TakSklad.exe must be adjacent to the verified packaged acceptance wrapper", script)
        self.assertIn("[StringComparer]::OrdinalIgnoreCase.Equals($WrapperDirectory, $AppDirectory)", script)

    def test_integrity_preflight_precedes_token_materialization_and_helper_start(self):
        script = SCRIPT_PATH.read_text(encoding="utf-8")

        integrity_call = script.rindex(
            "Assert-TakSkladAuthHelperIntegrity -ResolvedAppPath $CandidatePath"
        )
        read_host = script.index('Read-Host "Scoped desktop token"')
        helper_install = script.index("--install-backend-token-stdin")
        self.assertLess(integrity_call, read_host)
        self.assertLess(integrity_call, helper_install)
        self.assertIn("Package manifest signer is not the pinned production signer", script)
        self.assertIn("is not signed by the pinned production signer", script)

    def test_helper_does_not_store_token_literal(self):
        script = SCRIPT_PATH.read_text(encoding="utf-8")

        forbidden_fragments = [
            "<service-token-from-local-secret-storage>",
            "api.135.181.245.84.sslip.io",
            "TELEGRAM_BOT_TOKEN=",
            "SKLADBOT_API_TOKEN=",
            "Authorization =",
        ]
        for fragment in forbidden_fragments:
            self.assertNotIn(fragment, script)


if __name__ == "__main__":
    unittest.main()
