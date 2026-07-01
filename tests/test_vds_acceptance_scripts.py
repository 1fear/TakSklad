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
            "version.json must be paused 1.1.7 rollout or forced 2.0.25 rollout",
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
            "verify_smartup_automation.sh",
            "SMARTUP_AUTOMATION_RUNTIME_REQUIRED=1",
            "smartup automation verifier failed",
            '"smartup_automation"',
            "ACCEPTANCE_HEALTH_ATTEMPTS",
            "ACCEPTANCE_HEALTH_RETRY_DELAY_SECONDS",
            "health_attempt",
            "http://127.0.0.1:8000/ready",
            "READINESS_OUTPUT",
            'readiness.get("status") != "ok"',
            "backend readiness status is not ok",
            '"backend_readiness"',
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
        smartup_automation_script = (PROJECT_ROOT / "deploy" / "vds" / "verify_smartup_automation.sh").read_text(
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

        self.assertIn("app.smartup_auto_import_worker status", smartup_automation_script)
        self.assertIn("SMARTUP_AUTO_IMPORT_BACKEND_IMPORT_ENABLED", smartup_automation_script)
        self.assertIn('skladbot_create_mode="dry_run"', smartup_automation_script)
        self.assertIn("client.change_status", smartup_automation_script)
        self.assertIn("successful_deal_ids", smartup_automation_script)
        self.assertIn("smartup_status_not_confirmed", smartup_automation_script)
        self.assertIn("failed_preview", smartup_automation_script)
        self.assertIn("target_delivery_date", smartup_automation_script)
        self.assertIn("reverse_geocode_yandex", smartup_automation_script)
        self.assertIn("imported_line_total > 0", smartup_automation_script)
        self.assertIn("explicit * quantity_blocks == line_total", smartup_automation_script)
        self.assertIn("Smartup runtime status is required but skipped", smartup_automation_script)
        self.assertIn('"status": "failed" if errors else "ok"', smartup_automation_script)

    def test_vds_compose_passes_geocoder_and_block_price_to_import_worker(self):
        compose = (PROJECT_ROOT / "deploy" / "vds" / "docker-compose.yml").read_text(encoding="utf-8")
        env_example = (PROJECT_ROOT / "deploy" / "vds" / ".env.example").read_text(encoding="utf-8")
        smartup_worker = compose.split("  smartup-auto-import-worker:", 1)[1].split(
            "\n  google-sheets-sync-worker:",
            1,
        )[0]

        self.assertIn("${TAKSKLAD_ENV_FILE:-.env}", compose)
        self.assertIn("TAKSKLAD_ENV_FILE=.env.example", env_example)
        self.assertIn("YANDEX_GEOCODER_API_KEY: ${YANDEX_GEOCODER_API_KEY:-}", smartup_worker)
        self.assertIn("TAKSKLAD_TIMEZONE: ${TAKSKLAD_TIMEZONE:-Asia/Tashkent}", smartup_worker)
        self.assertIn("TAKSKLAD_DEFAULT_BLOCK_PRICE: ${TAKSKLAD_DEFAULT_BLOCK_PRICE:-240000}", smartup_worker)
        self.assertIn("SKLADBOT_WORKER_INTERVAL_SECONDS: ${SKLADBOT_WORKER_INTERVAL_SECONDS:-60}", compose)
        self.assertIn("SKLADBOT_REQUEST_DELAY_SECONDS: ${SKLADBOT_REQUEST_DELAY_SECONDS:-2}", compose)
        self.assertIn("SKLADBOT_SKU_MAPPING_JSON: ${SKLADBOT_SKU_MAPPING_JSON:-}", smartup_worker)
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
        self.assertIn("SKLADBOT_SKU_MAPPING_JSON=", env_example)
        self.assertIn("SKLADBOT_SYNC_MAX_LOOKBACK_DAYS=7", env_example)
        self.assertIn("SKLADBOT_ORDER_CREATE_LEAD_DAYS=3", env_example)
        self.assertIn("SKLADBOT_DETAIL_LIMIT=10", env_example)
        self.assertIn("SKLADBOT_COMPLETED_BACKFILL_DAYS=2", env_example)
        self.assertIn("TAKSKLAD_GOOGLE_TO_BACKEND_SYNC_ENABLED=false", env_example)
        self.assertIn("TAKSKLAD_ENV=production", env_example)
        self.assertNotIn("TAKSKLAD_ADMINER_HOST", env_example)
        self.assertIn("TELEGRAM_ADMIN_CHAT_IDS=", env_example)

    def test_web_deploy_forces_https_security_headers(self):
        compose = (PROJECT_ROOT / "deploy" / "vds" / "docker-compose.yml").read_text(encoding="utf-8")
        nginx = (PROJECT_ROOT / "frontend" / "nginx.conf.template").read_text(encoding="utf-8")

        self.assertIn("traefik.http.routers.taksklad-backend.middlewares=taksklad-security-headers", compose)
        self.assertIn("traefik.http.routers.taksklad-frontend.middlewares=taksklad-security-headers,taksklad-frontend-csp", compose)
        self.assertIn('profiles: ["adminer"]', compose)
        self.assertIn("traefik.enable=false", compose)
        self.assertNotIn("traefik.http.routers.taksklad-adminer", compose)
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

    def test_vds_compose_declares_runtime_healthchecks(self):
        compose = (PROJECT_ROOT / "deploy" / "vds" / "docker-compose.yml").read_text(encoding="utf-8")

        self.assertIn("http://127.0.0.1:8000/ready", compose)
        self.assertIn("wget -qO- http://127.0.0.1/", compose)
        self.assertGreaterEqual(compose.count("db.execute(text('SELECT 1')).scalar()"), 4)

    def test_frontend_uses_same_origin_api_proxy_contract(self):
        compose = (PROJECT_ROOT / "deploy" / "vds" / "docker-compose.yml").read_text(encoding="utf-8")
        nginx = (PROJECT_ROOT / "frontend" / "nginx.conf.template").read_text(encoding="utf-8")
        api_source = (PROJECT_ROOT / "frontend" / "src" / "api.ts").read_text(encoding="utf-8")
        vite_config = (PROJECT_ROOT / "frontend" / "vite.config.ts").read_text(encoding="utf-8")

        self.assertIn("export function defaultApiUrl()", api_source)
        self.assertIn('return "";', api_source)
        self.assertNotIn("VITE_TAKSKLAD_API_URL", api_source)
        self.assertIn("const response = await fetch(`${apiUrl}${path}`", api_source)
        self.assertIn('downloadDiagnosticsLog(config: ApiConfig)', api_source)
        self.assertIn('fetch(`${apiUrl}/api/v1/diagnostics/logs`', api_source)

        self.assertIn("location /api/ {", nginx)
        self.assertIn("auth_request /_taksklad_auth_check;", nginx)
        self.assertNotIn('proxy_set_header Authorization "Bearer ${TAKSKLAD_API_TOKEN}";', nginx)
        self.assertIn('proxy_set_header Authorization "";', nginx)
        self.assertIn("proxy_pass $taksklad_backend;", nginx)
        self.assertIn("connect-src 'self'", nginx)

        self.assertIn("TAKSKLAD_BACKEND_INTERNAL_URL: http://backend-api:8000", compose)
        self.assertIn("taksklad-internal", compose)
        self.assertNotIn("VITE_TAKSKLAD_API_URL", compose)

        self.assertIn("VITE_TAKSKLAD_DEV_API_URL", vite_config)
        self.assertIn('"/api"', vite_config)


if __name__ == "__main__":
    unittest.main()
