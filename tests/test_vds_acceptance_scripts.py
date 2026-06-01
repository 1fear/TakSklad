from pathlib import Path
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class VdsAcceptanceScriptsTests(unittest.TestCase):
    def test_acceptance_status_checks_manifest_template_and_rollout_safety(self):
        script = (PROJECT_ROOT / "deploy" / "vds" / "acceptance_status.sh").read_text(encoding="utf-8")

        expected_fragments = [
            "result_template",
            "result_file",
            "Acceptance result template not found",
            "Acceptance result file not found",
            "version.json latest_version must be 2.0.0",
            "version.json min_supported_version must stay 1.1.7 for non-forced rollout",
            "version.json mandatory must be false during staged rollout",
            "version.json onefile download_url and sha256 must be set",
            "version.json onedir download_url_onedir and sha256_onedir must be set",
            '"version_json_staged_rollout", "github_release_published", "push_notifications_allowed", "mandatory_update_disabled"',
            "manifest safety.{key} must be true",
            "manifest safety.contains_secrets must be false",
            "ACCEPTANCE_RESULTS.md",
            "verify_telegram_menu.sh",
            "telegram menu verifier failed",
            '"telegram_menu"',
            "verify_google_backend_sync.sh",
            "google/backend sync verifier failed",
            '"google_backend_sync"',
            "ACCEPTANCE_HEALTH_ATTEMPTS",
            "ACCEPTANCE_HEALTH_RETRY_DELAY_SECONDS",
            "health_attempt",
            "release_go_no_go.py",
            "--require-go",
            '"release_go_no_go"',
            "release GO/NO-GO is not go",
            'ENV_FILE="$(cd "$(dirname "$ENV_FILE")" && pwd)/$(basename "$ENV_FILE")"',
            'if ((${#VERIFY_ARGS[@]})); then',
        ]
        for fragment in expected_fragments:
            self.assertIn(fragment, script)

    def test_acceptance_marker_scripts_keep_safe_marker_guard(self):
        verify_script = (PROJECT_ROOT / "deploy" / "vds" / "verify_acceptance_marker.sh").read_text(
            encoding="utf-8"
        )
        cleanup_script = (PROJECT_ROOT / "deploy" / "vds" / "cleanup_acceptance_marker.sh").read_text(
            encoding="utf-8"
        )
        telegram_menu_script = (PROJECT_ROOT / "deploy" / "vds" / "verify_telegram_menu.sh").read_text(
            encoding="utf-8"
        )
        google_sync_script = (PROJECT_ROOT / "deploy" / "vds" / "verify_google_backend_sync.sh").read_text(
            encoding="utf-8"
        )

        for script in (verify_script, cleanup_script):
            self.assertIn("*ACCEPTANCE*|*WEB_UI_SMOKE*|*SMOKE_MVP*", script)
            self.assertIn("Refusing unsafe marker", script)

        self.assertIn("Выгрузка КИЗов", telegram_menu_script)
        self.assertIn('"status": "failed" if errors else "ok"', telegram_menu_script)
        self.assertIn("getMyCommands", telegram_menu_script)
        self.assertIn("getChatMenuButton", telegram_menu_script)

        self.assertIn("app.google_backend_sync_diagnostic", google_sync_script)
        self.assertIn("--detail-limit", google_sync_script)
        self.assertIn("GOOGLE_BACKEND_SYNC_ATTEMPTS", google_sync_script)
        self.assertIn("GOOGLE_BACKEND_SYNC_RETRY_DELAY_SECONDS", google_sync_script)
        self.assertIn("Quota exceeded", google_sync_script)
        self.assertIn("APIError: [429]", google_sync_script)

    def test_vds_compose_passes_geocoder_and_block_price_to_import_worker(self):
        compose = (PROJECT_ROOT / "deploy" / "vds" / "docker-compose.yml").read_text(encoding="utf-8")
        env_example = (PROJECT_ROOT / "deploy" / "vds" / ".env.example").read_text(encoding="utf-8")

        self.assertIn("${TAKSKLAD_ENV_FILE:-.env}", compose)
        self.assertIn("TAKSKLAD_ENV_FILE=.env.example", env_example)
        self.assertIn("YANDEX_GEOCODER_API_KEY: ${YANDEX_GEOCODER_API_KEY:-}", compose)
        self.assertIn("TAKSKLAD_TIMEZONE: ${TAKSKLAD_TIMEZONE:-Asia/Tashkent}", compose)
        self.assertIn("TAKSKLAD_DEFAULT_BLOCK_PRICE: ${TAKSKLAD_DEFAULT_BLOCK_PRICE:-240000}", compose)
        self.assertIn("SKLADBOT_WORKER_INTERVAL_SECONDS: ${SKLADBOT_WORKER_INTERVAL_SECONDS:-60}", compose)
        self.assertIn("SKLADBOT_SYNC_MAX_LOOKBACK_DAYS: ${SKLADBOT_SYNC_MAX_LOOKBACK_DAYS:-7}", compose)
        self.assertIn("SKLADBOT_ORDER_CREATE_LEAD_DAYS: ${SKLADBOT_ORDER_CREATE_LEAD_DAYS:-3}", compose)
        self.assertIn("SKLADBOT_DETAIL_LIMIT: ${SKLADBOT_DETAIL_LIMIT:-30}", compose)
        self.assertIn("YANDEX_GEOCODER_API_KEY=", env_example)
        self.assertIn("TAKSKLAD_TIMEZONE=Asia/Tashkent", env_example)
        self.assertIn("TAKSKLAD_DEFAULT_BLOCK_PRICE=240000", env_example)
        self.assertIn("SKLADBOT_WORKER_INTERVAL_SECONDS=60", env_example)
        self.assertIn("SKLADBOT_SYNC_MAX_LOOKBACK_DAYS=7", env_example)
        self.assertIn("SKLADBOT_ORDER_CREATE_LEAD_DAYS=3", env_example)
        self.assertIn("SKLADBOT_DETAIL_LIMIT=30", env_example)
        self.assertIn("TELEGRAM_ADMIN_CHAT_IDS=", env_example)


if __name__ == "__main__":
    unittest.main()
