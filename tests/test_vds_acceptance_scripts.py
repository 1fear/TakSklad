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
            "version.json latest_version must be 2.0.14",
            "version.json min_supported_version must be 2.0.14 for forced rollout",
            "version.json mandatory must be true during forced rollout",
            "version.json onefile download_url and sha256 must be set",
            "version.json onedir download_url_onedir and sha256_onedir must be set",
            '"version_json_staged_rollout", "github_release_published", "push_notifications_allowed", "mandatory_update_enabled"',
            "manifest safety.{key} must be true",
            "manifest safety.contains_secrets must be false",
            "ACCEPTANCE_RESULTS.md",
            "verify_telegram_menu.sh",
            "telegram menu verifier failed",
            '"telegram_menu"',
            "verify_google_backend_sync.sh",
            "google/backend sync verifier failed",
            '"google_backend_sync"',
            "verify_skladbot_coverage.sh",
            "skladbot coverage verifier failed",
            '"skladbot_coverage"',
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
        skladbot_coverage_script = (PROJECT_ROOT / "deploy" / "vds" / "verify_skladbot_coverage.sh").read_text(
            encoding="utf-8"
        )

        for script in (verify_script, cleanup_script):
            self.assertIn("*ACCEPTANCE*|*WEB_UI_SMOKE*|*SMOKE_MVP*", script)
            self.assertIn("Refusing unsafe marker", script)

        self.assertIn("expected_commands = []", telegram_menu_script)
        self.assertIn("Telegram public commands must be empty", telegram_menu_script)
        self.assertIn('"status": "failed" if errors else "ok"', telegram_menu_script)
        self.assertIn("getMyCommands", telegram_menu_script)
        self.assertIn("getChatMenuButton", telegram_menu_script)

        self.assertIn("app.google_backend_sync_diagnostic", google_sync_script)
        self.assertIn("--detail-limit", google_sync_script)
        self.assertIn("GOOGLE_BACKEND_SYNC_ATTEMPTS", google_sync_script)
        self.assertIn("GOOGLE_BACKEND_SYNC_RETRY_DELAY_SECONDS", google_sync_script)
        self.assertIn("Quota exceeded", google_sync_script)
        self.assertIn("APIError: [429]", google_sync_script)

        self.assertIn("app.skladbot_coverage_diagnostic", skladbot_coverage_script)
        self.assertIn("--marker", skladbot_coverage_script)
        self.assertIn("--detail-limit", skladbot_coverage_script)

    def test_vds_compose_passes_geocoder_and_block_price_to_import_worker(self):
        compose = (PROJECT_ROOT / "deploy" / "vds" / "docker-compose.yml").read_text(encoding="utf-8")
        env_example = (PROJECT_ROOT / "deploy" / "vds" / ".env.example").read_text(encoding="utf-8")

        self.assertIn("${TAKSKLAD_ENV_FILE:-.env}", compose)
        self.assertIn("TAKSKLAD_ENV_FILE=.env.example", env_example)
        self.assertIn("YANDEX_GEOCODER_API_KEY: ${YANDEX_GEOCODER_API_KEY:-}", compose)
        self.assertIn("TAKSKLAD_TIMEZONE: ${TAKSKLAD_TIMEZONE:-Asia/Tashkent}", compose)
        self.assertIn("TAKSKLAD_DEFAULT_BLOCK_PRICE: ${TAKSKLAD_DEFAULT_BLOCK_PRICE:-240000}", compose)
        self.assertIn("SKLADBOT_WORKER_INTERVAL_SECONDS: ${SKLADBOT_WORKER_INTERVAL_SECONDS:-60}", compose)
        self.assertIn("SKLADBOT_REQUEST_DELAY_SECONDS: ${SKLADBOT_REQUEST_DELAY_SECONDS:-2}", compose)
        self.assertIn("SKLADBOT_SYNC_MAX_LOOKBACK_DAYS: ${SKLADBOT_SYNC_MAX_LOOKBACK_DAYS:-7}", compose)
        self.assertIn("SKLADBOT_ORDER_CREATE_LEAD_DAYS: ${SKLADBOT_ORDER_CREATE_LEAD_DAYS:-3}", compose)
        self.assertIn("SKLADBOT_DETAIL_LIMIT: ${SKLADBOT_DETAIL_LIMIT:-10}", compose)
        self.assertIn("SKLADBOT_COMPLETED_BACKFILL_DAYS: ${SKLADBOT_COMPLETED_BACKFILL_DAYS:-2}", compose)
        self.assertIn(
            "TAKSKLAD_GOOGLE_TO_BACKEND_SYNC_ENABLED: ${TAKSKLAD_GOOGLE_TO_BACKEND_SYNC_ENABLED:-false}",
            compose,
        )
        self.assertIn("YANDEX_GEOCODER_API_KEY=", env_example)
        self.assertIn("TAKSKLAD_TIMEZONE=Asia/Tashkent", env_example)
        self.assertIn("TAKSKLAD_DEFAULT_BLOCK_PRICE=240000", env_example)
        self.assertIn("SKLADBOT_WORKER_INTERVAL_SECONDS=60", env_example)
        self.assertIn("SKLADBOT_REQUEST_DELAY_SECONDS=2", env_example)
        self.assertIn("SKLADBOT_SYNC_MAX_LOOKBACK_DAYS=7", env_example)
        self.assertIn("SKLADBOT_ORDER_CREATE_LEAD_DAYS=3", env_example)
        self.assertIn("SKLADBOT_DETAIL_LIMIT=10", env_example)
        self.assertIn("SKLADBOT_COMPLETED_BACKFILL_DAYS=2", env_example)
        self.assertIn("TAKSKLAD_GOOGLE_TO_BACKEND_SYNC_ENABLED=false", env_example)
        self.assertIn("TELEGRAM_ADMIN_CHAT_IDS=", env_example)

    def test_web_deploy_forces_https_security_headers(self):
        compose = (PROJECT_ROOT / "deploy" / "vds" / "docker-compose.yml").read_text(encoding="utf-8")
        nginx = (PROJECT_ROOT / "frontend" / "nginx.conf.template").read_text(encoding="utf-8")

        self.assertIn("traefik.http.routers.taksklad-backend.middlewares=taksklad-security-headers", compose)
        self.assertIn("traefik.http.routers.taksklad-frontend.middlewares=taksklad-security-headers,taksklad-frontend-csp", compose)
        self.assertIn("traefik.http.routers.taksklad-adminer.middlewares=taksklad-security-headers", compose)
        self.assertIn("headers.stsSeconds=31536000", compose)
        self.assertIn("headers.stsIncludeSubdomains=true", compose)
        self.assertIn("headers.contentTypeNosniff=true", compose)
        self.assertIn("headers.frameDeny=true", compose)
        self.assertIn("upgrade-insecure-requests", compose)
        self.assertIn("block-all-mixed-content", compose)

        self.assertIn('add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;', nginx)
        self.assertIn("Content-Security-Policy", nginx)
        self.assertIn("upgrade-insecure-requests", nginx)
        self.assertIn("block-all-mixed-content", nginx)
        self.assertIn('add_header X-Content-Type-Options "nosniff" always;', nginx)
        self.assertIn('add_header X-Frame-Options "DENY" always;', nginx)
        self.assertIn('add_header Referrer-Policy "same-origin" always;', nginx)
        self.assertIn("resolver 127.0.0.11 valid=10s ipv6=off;", nginx)
        self.assertIn('set $taksklad_backend "${TAKSKLAD_BACKEND_INTERNAL_URL}";', nginx)
        self.assertNotIn("proxy_pass ${TAKSKLAD_BACKEND_INTERNAL_URL}", nginx)
        self.assertEqual(nginx.count("proxy_pass $taksklad_backend;"), 4)
        self.assertIn("proxy_pass $taksklad_backend/api/v1/auth/check;", nginx)
        self.assertNotIn("proxy_set_header X-Forwarded-Proto $scheme;", nginx)
        self.assertEqual(nginx.count("proxy_set_header X-Forwarded-Proto https;"), 4)
        self.assertNotIn("VITE_TAKSKLAD_API_URL", compose)


if __name__ == "__main__":
    unittest.main()
