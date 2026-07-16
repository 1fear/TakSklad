import subprocess
import tempfile
import unittest
from pathlib import Path

from tools.container_runtime_harness import (
    HarnessError,
    docker_bind_source,
    last_integer_line,
    memory_allocation_was_denied,
    parse_memory_bytes,
)


ROOT = Path(__file__).resolve().parents[1]


class ContainerRuntimeHarnessContractTests(unittest.TestCase):
    def test_shell_wrappers_require_fail_closed_flags(self):
        smoke = (ROOT / "tools/run_container_smoke.sh").read_text(encoding="utf-8")
        load = (ROOT / "tools/run_container_load.sh").read_text(encoding="utf-8")
        self.assertIn("--dummy-config", smoke)
        self.assertIn("--permission-tests", smoke)
        self.assertIn("--assert-resource-limits", load)
        self.assertIn("COMPOSE_DISABLE_ENV_FILE=1", smoke)
        self.assertIn("COMPOSE_DISABLE_ENV_FILE=1", load)

    def test_runtime_harness_is_uniquely_scoped_and_never_uses_compose_down_v(self):
        source = (ROOT / "tools/container_runtime_harness.py").read_text(encoding="utf-8")
        self.assertIn("uuid.uuid4().hex", source)
        self.assertIn("production_volumes_touched=0", source)
        self.assertNotIn("down -v", source)
        self.assertNotIn("../../outputs", source)
        self.assertIn("type=bind,src=", source)

    def test_output_permission_reconciliation_requires_exact_confirmation(self):
        source = (ROOT / "tools/reconcile_output_permissions.sh").read_text(encoding="utf-8")
        self.assertIn("PHASE22_CHANGE_OUTPUT_OWNER", source)
        self.assertIn("--expected-parent", source)
        self.assertIn("$PARENT_CANONICAL/outputs", source)
        self.assertIn("TAKSKLAD_PHASE22_SYNTHETIC_OUTPUT_ROOT", source)
        self.assertIn("--cap-add CHOWN", source)
        self.assertIn("TAKSKLAD_OUTPUT_PERMISSIONS_IMAGE", source)
        self.assertIn("path_value_redacted=1", source)
        self.assertNotIn("docker volume rm", source)

    def test_output_reconciliation_rejects_arbitrary_absolute_directory_before_docker(self):
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            arbitrary = parent / "not-outputs"
            arbitrary.mkdir()
            completed = subprocess.run(
                [
                    str(ROOT / "tools/reconcile_output_permissions.sh"),
                    "--path",
                    str(arbitrary),
                    "--expected-parent",
                    str(parent),
                    "--apply",
                    "--confirm",
                    "PHASE22_CHANGE_OUTPUT_OWNER",
                ],
                text=True,
                capture_output=True,
                check=False,
            )
        self.assertEqual(completed.returncode, 2)
        self.assertIn("outputs child", completed.stderr)

    def test_real_worker_entrypoints_are_exercised_on_an_internal_network(self):
        source = (ROOT / "tools/container_runtime_harness.py").read_text(encoding="utf-8")
        self.assertIn('"network", "create", "--internal"', source)
        for module in (
            "app.skladbot_worker_runner",
            "app.smartup_auto_import_worker",
            "app.google_sheets_sync_worker",
            "app.telegram_worker",
        ):
            self.assertIn(module, source)
        self.assertNotIn("Path('/tmp/heartbeat')", source)

    def test_runtime_harness_contains_all_required_negative_probes(self):
        source = (ROOT / "tools/container_runtime_harness.py").read_text(encoding="utf-8")
        for marker in (
            "rootfs write unexpectedly succeeded",
            "Docker POST unexpectedly allowed",
            "Docker images endpoint unexpectedly allowed",
            "PID limit did not deny",
            "memory limit did not deny",
            "log rotation did not retain",
        ):
            self.assertIn(marker, source)

    def test_stats_memory_parser(self):
        self.assertEqual(parse_memory_bytes("52.5MiB"), int(52.5 * 1024**2))
        self.assertEqual(parse_memory_bytes("1.25GiB"), int(1.25 * 1024**3))
        self.assertEqual(parse_memory_bytes("unknown"), 0)

    def test_memory_limit_accepts_kernel_kill_or_allocator_denial(self):
        self.assertTrue(memory_allocation_was_denied({"OOMKilled": True, "ExitCode": 137}))
        self.assertTrue(memory_allocation_was_denied({"OOMKilled": False, "ExitCode": 42}))
        self.assertFalse(memory_allocation_was_denied({"OOMKilled": False, "ExitCode": 0}))
        self.assertFalse(memory_allocation_was_denied({"OOMKilled": False, "ExitCode": 1}))

    def test_integer_parser_ignores_fresh_image_pull_diagnostics(self):
        output = "Unable to find image locally\nPull complete\n70\n"
        self.assertEqual(last_integer_line(output, "postgres uid"), 70)
        with self.assertRaises(HarnessError):
            last_integer_line("Pull complete\n", "postgres uid")

    def test_darwin_private_var_bind_path_has_explicit_normalizer(self):
        source = (ROOT / "tools/container_runtime_harness.py").read_text(encoding="utf-8")
        self.assertIn("/private/var/", source)
        self.assertIn('removeprefix("/private")', source)


if __name__ == "__main__":
    unittest.main()
