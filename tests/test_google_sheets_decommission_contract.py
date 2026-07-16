import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class GoogleSheetsDecommissionContractTests(unittest.TestCase):
    def test_removed_runtime_modules_do_not_exist(self):
        removed = [
            "src/taksklad/sheets.py",
            "src/taksklad/skladbot_sync.py",
            "backend/app/google_backend_sync_diagnostic.py",
            "backend/app/google_sheets_exporter.py",
            "backend/app/google_sheets_pending.py",
            "backend/app/google_sheets_sync_worker.py",
            "deploy/vds/verify_google_backend_sync.sh",
        ]
        self.assertEqual([path for path in removed if (ROOT / path).exists()], [])

    def test_google_client_dependencies_are_absent(self):
        manifests = [
            "requirements.txt",
            "requirements/desktop.lock",
            "backend/requirements.txt",
            "backend/requirements.lock",
        ]
        forbidden = re.compile(r"(?im)^(?:gspread|oauth2client|google-auth)(?:\[.*\])?(?:==|\s*$)")
        for path in manifests:
            text = (ROOT / path).read_text(encoding="utf-8")
            self.assertIsNone(forbidden.search(text), path)

    def test_runtime_has_no_google_sheets_imports(self):
        roots = [ROOT / "src" / "taksklad", ROOT / "backend" / "app"]
        pattern = re.compile(
            r"(?:from|import)\s+(?:gspread|oauth2client|google_sheets|taksklad\.sheets|\.sheets)"
        )
        violations = []
        for source_root in roots:
            for path in source_root.rglob("*.py"):
                if pattern.search(path.read_text(encoding="utf-8")):
                    violations.append(str(path.relative_to(ROOT)))
        self.assertEqual(violations, [])

    def test_desktop_config_has_no_google_credentials_or_spreadsheet(self):
        config = (ROOT / "src" / "taksklad" / "config.py").read_text(encoding="utf-8")
        for marker in ("SPREADSHEET_ID", "CREDENTIALS_FILE", "credentials.json"):
            self.assertNotIn(marker, config)

    def test_compose_has_no_google_worker_or_credentials_mount(self):
        compose = (ROOT / "deploy" / "vds" / "docker-compose.yml").read_text(encoding="utf-8")
        self.assertNotIn("google-sheets-sync-worker", compose)
        self.assertNotIn("google_sheets_sync", compose)
        self.assertNotIn("credentials.json", compose)
        self.assertNotIn("TAKSKLAD_GOOGLE_", compose)

    def test_public_update_channel_is_not_promoted_by_decommission_branch(self):
        # Promotion belongs to the final, rebased release SHA after all parallel
        # release work is finished; implementation branches must not force it.
        manifest = (ROOT / "version.json").read_text(encoding="utf-8")
        self.assertNotIn("google-sheets-decommission", manifest)


if __name__ == "__main__":
    unittest.main()
