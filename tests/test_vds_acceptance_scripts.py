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
            "version.json latest_version is not pinned to 1.1.7",
            "version.json min_supported_version is not pinned to 1.1.7",
            "version.json mandatory must be false before rollout",
            "version.json download URLs must stay empty before rollout",
            '"no_version_json_change", "no_github_release", "no_push_notifications"',
            "manifest safety.{key} must be true",
            "manifest safety.contains_secrets must be false",
            "ACCEPTANCE_RESULTS.md",
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

        for script in (verify_script, cleanup_script):
            self.assertIn("*ACCEPTANCE*|*WEB_UI_SMOKE*|*SMOKE_MVP*", script)
            self.assertIn("Refusing unsafe marker", script)

    def test_vds_compose_passes_geocoder_and_block_price_to_import_worker(self):
        compose = (PROJECT_ROOT / "deploy" / "vds" / "docker-compose.yml").read_text(encoding="utf-8")
        env_example = (PROJECT_ROOT / "deploy" / "vds" / ".env.example").read_text(encoding="utf-8")

        self.assertIn("${TAKSKLAD_ENV_FILE:-.env}", compose)
        self.assertIn("TAKSKLAD_ENV_FILE=.env.example", env_example)
        self.assertIn("YANDEX_GEOCODER_API_KEY: ${YANDEX_GEOCODER_API_KEY:-}", compose)
        self.assertIn("TAKSKLAD_DEFAULT_BLOCK_PRICE: ${TAKSKLAD_DEFAULT_BLOCK_PRICE:-240000}", compose)
        self.assertIn("YANDEX_GEOCODER_API_KEY=", env_example)
        self.assertIn("TAKSKLAD_DEFAULT_BLOCK_PRICE=240000", env_example)
        self.assertIn("SKLADBOT_WORKER_INTERVAL_SECONDS=60", env_example)
        self.assertIn("TELEGRAM_ADMIN_CHAT_IDS=", env_example)


if __name__ == "__main__":
    unittest.main()
