import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEPLOY_SCRIPT = ROOT / "deploy" / "vds" / "deploy_from_git.sh"

CANDIDATE = {
    "RELEASE_SOURCE_SHA": "b" * 40,
    "RELEASE_SERVER_RELEASE_ID": "server-" + "b" * 40,
    "RELEASE_BACKEND_IMAGE": "ghcr.io/example/backend@sha256:" + "b" * 64,
    "RELEASE_FRONTEND_IMAGE": "ghcr.io/example/frontend@sha256:" + "b" * 64,
    "RELEASE_BACKEND_DIGEST": "sha256:" + "b" * 64,
    "RELEASE_DESKTOP_API_CONTRACT": "2",
}
PREVIOUS = {
    "RELEASE_SOURCE_SHA": "a" * 40,
    "RELEASE_SERVER_RELEASE_ID": "server-" + "a" * 40,
    "RELEASE_BACKEND_IMAGE": "ghcr.io/example/backend@sha256:" + "a" * 64,
    "RELEASE_FRONTEND_IMAGE": "ghcr.io/example/frontend@sha256:" + "a" * 64,
    "RELEASE_BACKEND_DIGEST": "sha256:" + "a" * 64,
    "RELEASE_DESKTOP_API_CONTRACT": "1",
}
# Ровно те переменные, которые export_release_runtime_env переносит в контейнеры.
EXPORTED = (
    ("TAKSKLAD_BACKEND_IMAGE", "RELEASE_BACKEND_IMAGE"),
    ("TAKSKLAD_FRONTEND_IMAGE", "RELEASE_FRONTEND_IMAGE"),
    ("TAKSKLAD_COMMIT_SHA", "RELEASE_SOURCE_SHA"),
    ("TAKSKLAD_IMAGE_DIGEST", "RELEASE_BACKEND_DIGEST"),
    ("TAKSKLAD_SERVER_RELEASE_ID", "RELEASE_SERVER_RELEASE_ID"),
    ("TAKSKLAD_DESKTOP_API_CONTRACT", "RELEASE_DESKTOP_API_CONTRACT"),
)


class DeployReleaseEnvRestoreTests(unittest.TestCase):
    """Проверка отката rollback-preflight к окружению кандидата.

    verify_previous_runtime_preflight временно подменяет RELEASE_* на прошлый
    релиз, чтобы доказать возможность отката. После него активируются контейнеры,
    поэтому в окружении не должно остаться ни одного значения прошлого релиза:
    иначе backend уезжает в прод с чужим release id и пересоздаётся на каждом
    следующем деплое из-за расхождения compose config hash.
    """

    @classmethod
    def setUpClass(cls):
        cls.script = DEPLOY_SCRIPT.read_text(encoding="utf-8")

    def function_body(self, name):
        return self.script.split(f"{name}() {{", 1)[1].split("\n}\n", 1)[0]

    def run_preflight(self):
        preflight = self.function_body("verify_previous_runtime_preflight")
        export_env = self.function_body("export_release_runtime_env")
        candidate_assignments = "\n".join(
            f"{key}={value!r}" for key, value in CANDIDATE.items()
        )
        previous_emits = "\n".join(
            f"  echo {key}={value!r}" for key, value in PREVIOUS.items()
        )
        candidate_emits = "\n".join(
            f"  echo {key}={value!r}" for key, value in CANDIDATE.items()
        )
        harness = f"""#!/usr/bin/env bash
set -u
PREVIOUS_MANIFEST=/tmp/previous-release.json
ARTIFACT_MANIFEST=/tmp/candidate-release.json
DAILY_REPORT_RECOVERY_ENABLED=0
HEALTH_URL=https://example.invalid/health
READY_URL=https://example.invalid/ready
{candidate_assignments}

export_release_runtime_env() {{{export_env}
}}
verify_release_manifest() {{ return 0; }}
emit_release_shell() {{
if [[ "${{1:-}}" == "$PREVIOUS_MANIFEST" ]]; then
{previous_emits}
else
{candidate_emits}
fi
}}
compose() {{ echo "20260902_0023"; return 0; }}
verify_selected_runtime_identity() {{ return 0; }}
check_public_url() {{ return 0; }}
verify_telegram_import_auth_recovery_candidate() {{ return 1; }}
verify_telegram_worker_repair_candidate() {{ return 1; }}
verify_daily_report_recovery_candidate() {{ return 0; }}
run_previous_auth_canary() {{ return 0; }}

verify_previous_runtime_preflight() {{{preflight}
}}

set +e
verify_previous_runtime_preflight
status=$?
set -e
echo "STATUS=$status"
for name in TAKSKLAD_BACKEND_IMAGE TAKSKLAD_FRONTEND_IMAGE TAKSKLAD_COMMIT_SHA \\
            TAKSKLAD_IMAGE_DIGEST TAKSKLAD_SERVER_RELEASE_ID TAKSKLAD_DESKTOP_API_CONTRACT; do
  echo "$name=${{!name-<unset>}}"
done
"""
        with tempfile.TemporaryDirectory() as temp_dir:
            completed = subprocess.run(
                ["bash", "-c", harness],
                cwd=temp_dir,
                text=True,
                capture_output=True,
                check=False,
            )
        values = {}
        for line in completed.stdout.splitlines():
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
        return completed, values

    def test_preflight_restores_every_exported_release_variable(self):
        completed, values = self.run_preflight()

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(values.get("STATUS"), "0", completed.stderr)
        for exported_name, release_name in EXPORTED:
            self.assertEqual(
                values.get(exported_name),
                CANDIDATE[release_name],
                f"{exported_name} обязана вернуться к значению кандидата после rollback-preflight",
            )

    def test_no_exported_variable_keeps_the_previous_release_value(self):
        _completed, values = self.run_preflight()

        leaked = [
            exported_name
            for exported_name, release_name in EXPORTED
            if values.get(exported_name) == PREVIOUS[release_name]
            and PREVIOUS[release_name] != CANDIDATE[release_name]
        ]
        self.assertEqual(leaked, [], "значения прошлого релиза не должны переживать preflight")

    def test_restore_is_not_a_hand_written_variable_list(self):
        preflight = self.function_body("verify_previous_runtime_preflight")

        self.assertIn(
            "candidate_shell",
            preflight,
            "окружение кандидата восстанавливается целиком, а не перечислением переменных",
        )


if __name__ == "__main__":
    unittest.main()
