import contextlib
import io
import shutil
import tempfile
import unittest
from pathlib import Path

import yaml

from tools.check_container_policy import main, parse_memory_bytes, validate_repository


ROOT = Path(__file__).resolve().parents[1]


class ContainerPolicyTests(unittest.TestCase):
    def make_root(self):
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        for relative in (
            "backend/Dockerfile",
            "frontend/Dockerfile",
            "deploy/vds/docker-compose.yml",
            "deploy/traefik/docker-compose.yml",
        ):
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / relative, target)
        return temporary, root

    def mutate_compose(self, root, relative, mutator):
        path = root / relative
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        mutator(payload)
        path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")

    def test_current_repository_passes_strict_policy(self):
        errors, rows = validate_repository(ROOT)
        self.assertEqual(errors, [])
        self.assertEqual(len(rows), 6)
        self.assertEqual(next(row for row in rows if row.service == "frontend").sensitive_names, ())

    def test_postgres_wal_init_can_reconcile_existing_directory_mode(self):
        payload = yaml.safe_load(
            (ROOT / "deploy" / "vds" / "docker-compose.yml").read_text(encoding="utf-8")
        )
        service = payload["services"]["postgres-wal-init"]
        self.assertEqual(service["user"], "0:0")
        self.assertIn("CHOWN", service["cap_add"])
        self.assertIn("FOWNER", service["cap_add"])
        self.assertEqual(service["restart"], "no")

    def test_telegram_worker_receives_complete_daily_report_environment(self):
        payload = yaml.safe_load(
            (ROOT / "deploy" / "vds" / "docker-compose.yml").read_text(encoding="utf-8")
        )
        service = payload["services"]["telegram-worker"]
        environment = service["environment"]
        required = {
            "TAKSKLAD_ENV",
            "TAKSKLAD_TIMEZONE",
            "TAKSKLAD_AUTOMATION_ALERT_CHAT_ID",
            "SKLADBOT_API_TOKEN",
            "SKLADBOT_API_TOKENS",
            "SKLADBOT_API_BASE_URL",
            "SKLADBOT_API_TIMEOUT_SECONDS",
            "SKLADBOT_API_MAX_RETRIES",
            "SKLADBOT_REQUEST_DELAY_SECONDS",
            "SKLADBOT_MAX_COOLDOWN_WAIT_SECONDS",
            "SKLADBOT_CUSTOMER_ID",
            "SKLADBOT_REQUESTS_LIMIT",
            "SKLADBOT_DAILY_REPORT_ENABLED",
            "SKLADBOT_DAILY_REPORT_CHAT_IDS",
            "SKLADBOT_DAILY_REPORT_HOUR",
            "SKLADBOT_DAILY_REPORT_MINUTE",
            "SKLADBOT_DAILY_REPORT_RETRY_MINUTES",
            "SKLADBOT_DAILY_REPORT_MAX_ATTEMPTS",
            "SKLADBOT_DAILY_REPORT_GRACE_MINUTES",
            "SKLADBOT_DAILY_REPORT_LOOKBACK_DAYS",
            "SKLADBOT_DAILY_REPORT_STALE_TTL_MINUTES",
            "SKLADBOT_DAILY_REPORT_REQUEST_TYPE_IDS",
            "SKLADBOT_DAILY_REPORT_REQUESTS_LIMIT",
            "SKLADBOT_DAILY_REPORT_MAX_PAGES",
            "SKLADBOT_DAILY_REPORT_DETAIL_LIMIT",
            "SKLADBOT_DAILY_REPORT_OUT_OF_SCOPE_DETAIL_SAMPLE_LIMIT",
            "SKLADBOT_DAILY_REPORT_MAX_RUNTIME_SECONDS",
            "SKLADBOT_DAILY_REPORT_REQUEST_DELAY_SECONDS",
            "SKLADBOT_DAILY_REPORT_429_RETRIES",
            "SKLADBOT_DAILY_REPORT_429_RETRY_SECONDS",
            "SKLADBOT_DAILY_REPORT_READ_POST_RETRIES",
            "SKLADBOT_DAILY_REPORT_READ_POST_RETRY_SECONDS",
            "SKLADBOT_DAILY_REPORT_MOVEMENTS_LIMIT",
            "SKLADBOT_DAILY_REPORT_PRODUCTS_LIMIT",
            "SKLADBOT_DAILY_REPORT_STOCK_LIMIT",
        }
        self.assertEqual(required - set(environment), set())
        self.assertNotIn("env_file", service)
        self.assertEqual(
            environment["SKLADBOT_DAILY_REPORT_ENABLED"],
            "${SKLADBOT_DAILY_REPORT_ENABLED:-false}",
        )

    def test_backend_requires_an_explicit_trusted_proxy_network(self):
        payload = yaml.safe_load(
            (ROOT / "deploy" / "vds" / "docker-compose.yml").read_text(encoding="utf-8")
        )

        self.assertEqual(
            payload["services"]["backend-api"]["environment"]["TAKSKLAD_TRUSTED_PROXY_CIDRS"],
            "${TAKSKLAD_TRUSTED_PROXY_CIDRS:?TAKSKLAD_TRUSTED_PROXY_CIDRS is required}",
        )

    def test_read_only_bind_is_not_classified_as_writable(self):
        temporary, root = self.make_root()
        with temporary:
            def mutate(payload):
                payload["services"]["backend-api"].setdefault("volumes", []).append({
                    "type": "bind",
                    "source": "/run/read-only-signal",
                    "target": "/run/read-only-signal",
                    "read_only": True,
                })

            self.mutate_compose(root, "deploy/vds/docker-compose.yml", mutate)
            errors, _ = validate_repository(root)
            self.assertEqual(errors, [])

    def test_missing_hardening_and_shared_env_file_are_blocked(self):
        temporary, root = self.make_root()
        with temporary:
            def mutate(payload):
                service = payload["services"]["backend-api"]
                service.pop("read_only", None)
                service["env_file"] = ["forbidden-config"]

            self.mutate_compose(root, "deploy/vds/docker-compose.yml", mutate)
            errors, _ = validate_repository(root)
            rendered = "\n".join(errors)
            self.assertIn("read_only=true", rendered)
            self.assertIn("env_file is forbidden", rendered)

    def test_frontend_secret_name_is_blocked_without_printing_value(self):
        temporary, root = self.make_root()
        with temporary:
            def mutate(payload):
                payload["services"]["frontend"]["environment"]["TAKSKLAD_API_TOKEN"] = "do-not-print-value"

            self.mutate_compose(root, "deploy/vds/docker-compose.yml", mutate)
            errors, _ = validate_repository(root)
            rendered = "\n".join(errors)
            self.assertIn("frontend", rendered)
            self.assertIn("TAKSKLAD_API_TOKEN", rendered)
            self.assertNotIn("do-not-print-value", rendered)

    def test_socket_proxy_write_or_images_access_is_blocked(self):
        temporary, root = self.make_root()
        with temporary:
            def mutate(payload):
                environment = payload["services"]["docker-socket-proxy"]["environment"]
                environment["POST"] = "1"
                environment["IMAGES"] = "1"

            self.mutate_compose(root, "deploy/traefik/docker-compose.yml", mutate)
            errors, _ = validate_repository(root)
            self.assertTrue(any("write/sensitive endpoints" in error for error in errors))

    def test_unknown_built_service_is_fail_closed(self):
        temporary, root = self.make_root()
        with temporary:
            def mutate(payload):
                payload["services"]["new-worker"] = {"build": {"context": "../../backend"}}

            self.mutate_compose(root, "deploy/vds/docker-compose.yml", mutate)
            errors, _ = validate_repository(root)
            self.assertIn("unclassified built services: new-worker", errors)

    def test_cli_diagnostics_never_print_environment_values(self):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
            status = main(["--root", str(ROOT), "--strict"])
        self.assertEqual(status, 0)
        self.assertNotIn("${", buffer.getvalue())
        self.assertNotIn("synthetic-only", buffer.getvalue())

    def test_memory_parser_is_bounded_and_explicit(self):
        self.assertEqual(parse_memory_bytes("128m"), 128 * 1024**2)
        self.assertEqual(parse_memory_bytes("1g"), 1024**3)
        self.assertEqual(parse_memory_bytes("unlimited"), 0)


if __name__ == "__main__":
    unittest.main()
