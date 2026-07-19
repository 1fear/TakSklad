import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from tools import validate_auth_canary_token_file as validator


TOKEN = b"tks." + b"a" * 32 + b"." + b"b" * 43
ROOT = Path(__file__).resolve().parents[1]


class DeployAuthCanaryContractTests(unittest.TestCase):
    def run_previous_canary(self, capabilities, status, *, flag=False, approval=""):
        script = (ROOT / "deploy/vds/deploy_from_git.sh").read_text(encoding="utf-8")
        function = script.split("run_previous_auth_canary() {", 1)[1].split("\n}\n", 1)[0]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = root / "previous.json"
            manifest.write_text(json.dumps({"capabilities": capabilities}), encoding="utf-8")
            calls = root / "calls"
            harness = f'''set -Eeuo pipefail
PREVIOUS_MANIFEST={str(manifest)!r}
PYTHON_BIN={sys.executable!r}
ALLOW_LEGACY_CANARY_BOOTSTRAP={1 if flag else 0}
TAKSKLAD_LEGACY_CANARY_BOOTSTRAP_APPROVAL={approval!r}
FAKE_HTTP_STATUS={status}
CALLS_FILE={str(calls)!r}
run_server_auth_canary() {{
  printf '%s\n' "$*" >> "$CALLS_FILE"
  case "$FAKE_HTTP_STATUS" in
    204)
      [[ " $* " != *" --require-missing-endpoint "* ]]
      return $?
      ;;
    404)
      [[ " $* " == *" --allow-missing-endpoint "* && \
         " $* " == *" --require-missing-endpoint "* ]]
      return $?
      ;;
    401|403|500) return 1 ;;
    *) return 1 ;;
  esac
}}
run_previous_auth_canary() {{{function}
}}
run_previous_auth_canary
'''
            completed = subprocess.run(
                ["bash", "-c", harness],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
                timeout=5,
            )
            invocations = calls.read_text(encoding="utf-8").splitlines() if calls.exists() else []
            return completed, invocations

    def test_candidate_capability_gate_precedes_candidate_shell_activation(self):
        script = (ROOT / "deploy" / "vds" / "deploy_from_git.sh").read_text(encoding="utf-8")

        candidate_gate = script.index('verify_candidate_release_manifest "$ARTIFACT_MANIFEST"')
        emit_shell = script.index('eval "$(emit_release_shell "$ARTIFACT_MANIFEST")"')
        self.assertLess(candidate_gate, emit_shell)

    def make_file(self, root: Path, payload=TOKEN + b"\n", mode=0o600):
        root.chmod(0o700)
        path = root / "acceptance.token"
        path.write_bytes(payload)
        path.chmod(mode)
        return path

    def test_token_file_validator_accepts_only_current_user_protected_scoped_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            valid = self.make_file(root)
            validator.validate(str(valid))

            bad_payloads = (b"", b"legacy", TOKEN + b"\nextra", b"x" * 4098)
            for index, payload in enumerate(bad_payloads):
                bad = root / f"bad-{index}"
                bad.write_bytes(payload)
                bad.chmod(0o600)
                with self.subTest(index=index), self.assertRaises(ValueError):
                    validator.validate(str(bad))

            for mode in (0o644, 0o660, 0o666):
                valid.chmod(mode)
                with self.subTest(mode=oct(mode)), self.assertRaises(ValueError):
                    validator.validate(str(valid))
            valid.chmod(0o600)

            with self.assertRaises(ValueError):
                validator.validate("relative.token")
            link = root / "link.token"
            link.symlink_to(valid)
            with self.assertRaises(ValueError):
                validator.validate(str(link))
            with mock.patch.object(validator.os, "geteuid", return_value=os.geteuid() + 1):
                with self.assertRaises(ValueError):
                    validator.validate(str(valid))
            root.chmod(0o722)
            with self.assertRaises(ValueError):
                validator.validate(str(valid))

    def test_validator_output_is_redacted(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with tempfile.TemporaryDirectory() as temporary:
            path = self.make_file(Path(temporary), payload=TOKEN + b"\nextra")
            with mock.patch("sys.stdout", stdout), mock.patch("sys.stderr", stderr):
                status = validator.main([str(path)])
        rendered = stdout.getvalue() + stderr.getvalue()
        self.assertEqual(status, 1)
        self.assertNotIn(TOKEN.decode(), rendered)

    def test_deploy_shell_orders_canary_before_record_and_has_fail_closed_rollback(self):
        script = (ROOT / "deploy/vds/deploy_from_git.sh").read_text(encoding="utf-8")
        self.assertLess(script.index("validate_auth_canary_token_file"), script.index('echo "Pulling verified'))
        self.assertLess(script.rindex("run_server_auth_canary ||"), script.index('mv -f "$temporary_record"'))
        rollback = script.split("rollback_runtime() {", 1)[1].split("\n}", 1)[0]
        for fragment in (
            'verify_release_manifest "$PREVIOUS_MANIFEST"',
            'emit_release_shell "$PREVIOUS_MANIFEST"',
            'docker pull "$TAKSKLAD_BACKEND_IMAGE" || return 1',
            'docker pull "$TAKSKLAD_FRONTEND_IMAGE" || return 1',
            "compose up -d",
            "verify_selected_runtime_identity || return 1",
            'check_public_url health "$HEALTH_URL" || return 1',
            'check_public_url readiness "$READY_URL" || return 1',
            "run_previous_auth_canary || return 1",
            "return 0",
        ):
            self.assertIn(fragment, rollback)
        self.assertNotIn("rollback_runtime || true", script)
        self.assertIn("rollback_unverified=1", script)
        self.assertIn("verified previous deployment record is required before production mutation", script)

    def run_rollback_harness(self, failure_stage: str) -> subprocess.CompletedProcess[str]:
        script = (ROOT / "deploy/vds/deploy_from_git.sh").read_text(encoding="utf-8")
        rollback = script.split("rollback_runtime() {", 1)[1].split("\n}\n", 1)[0]
        after = script.split("rollback_after_candidate_failure() {", 1)[1].split("\n}\n", 1)[0]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake_python = root / "python"
            fake_python.write_text(
                """#!/bin/sh
case " $* " in
  *"release_artifacts.py verify"*)
    [ "$FAIL_STAGE" = verify ] && exit 1
    exit 0
    ;;
  *"release_artifacts.py emit-shell"*)
    [ "$FAIL_STAGE" = emit ] && exit 1
    if [ "$FAIL_STAGE" = eval ]; then printf 'false\\n'; exit 0; fi
    cat <<'EOF'
RELEASE_BACKEND_IMAGE=backend@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
RELEASE_FRONTEND_IMAGE=frontend@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
RELEASE_SOURCE_SHA=cccccccccccccccccccccccccccccccccccccccc
RELEASE_BACKEND_DIGEST=sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
EOF
    exit 0
    ;;
esac
exit 1
""",
                encoding="utf-8",
            )
            fake_python.chmod(0o700)
            harness = f'''set -Euo pipefail
FAIL_STAGE={failure_stage!r}
export FAIL_STAGE
PREVIOUS_MANIFEST={str(root / "previous.json")!r}
PYTHON_BIN={str(fake_python)!r}
COMPOSE_WAIT_TIMEOUT_SECONDS=1
HEALTH_URL=health
READY_URL=ready
WRITER_SERVICES=(legacy worker)
docker() {{
  if [[ "$1" == pull ]]; then
    [[ "$FAIL_STAGE" != backend_pull || "$2" != backend@* ]] || return 1
    [[ "$FAIL_STAGE" != frontend_pull || "$2" != frontend@* ]] || return 1
  fi
  return 0
}}
compose() {{
  if [[ "$1" == exec ]]; then
    [[ "$FAIL_STAGE" != database_head ]] || return 1
    printf '20260716_0019\\n'
  elif [[ "$1" == run ]]; then
    [[ "$FAIL_STAGE" != runtime_head ]] || return 1
    printf '20260716_0019 (head)\\n'
  elif [[ "$1" == up ]]; then
    [[ "$FAIL_STAGE" != compose_up ]]
  fi
}}
legacy_google_worker_ids() {{
  [[ "$FAIL_STAGE" != legacy_ids ]] || return 1
  printf ''
}}
verify_selected_runtime_identity() {{ [[ "$FAIL_STAGE" != identity ]]; }}
check_public_url() {{
  [[ "$FAIL_STAGE" != "$1" ]]
}}
run_previous_auth_canary() {{ [[ "$FAIL_STAGE" != auth_canary ]]; }}
verify_release_manifest() {{
  "$PYTHON_BIN" tools/release_artifacts.py verify --manifest "$1"
}}
emit_release_shell() {{
  "$PYTHON_BIN" tools/release_artifacts.py emit-shell --manifest "$1"
}}
export_release_runtime_env() {{
  export TAKSKLAD_BACKEND_IMAGE="$RELEASE_BACKEND_IMAGE"
  export TAKSKLAD_FRONTEND_IMAGE="$RELEASE_FRONTEND_IMAGE"
  export TAKSKLAD_COMMIT_SHA="$RELEASE_SOURCE_SHA"
  export TAKSKLAD_IMAGE_DIGEST="$RELEASE_BACKEND_DIGEST"
}}
fail() {{ printf '%s\\n' "$*" >&2; exit 1; }}
rollback_runtime() {{{rollback}
}}
rollback_after_candidate_failure() {{{after}
}}
set +e
rollback_after_candidate_failure synthetic_candidate_failure
status=$?
set -e
exit "$status"
'''
            return subprocess.run(
                ["bash", "-c", harness],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
                timeout=5,
            )

    def test_each_rollback_stage_failure_is_unverified_never_false_restored(self):
        stages = (
            "verify",
            "emit",
            "eval",
            "backend_pull",
            "frontend_pull",
            "database_head",
            "runtime_head",
            "compose_up",
            "legacy_ids",
            "identity",
            "health",
            "readiness",
            "auth_canary",
        )
        for stage in stages:
            with self.subTest(stage=stage):
                completed = self.run_rollback_harness(stage)
                self.assertNotEqual(completed.returncode, 0)
                self.assertIn("rollback_unverified=1", completed.stderr)
                self.assertNotIn("rollback_restored=1", completed.stderr)
                self.assertNotIn("Runtime rolled back", completed.stdout)

        completed = self.run_rollback_harness("")
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("rollback_restored=1", completed.stderr)
        self.assertNotIn("rollback_unverified=1", completed.stderr)

    def test_previous_canary_capability_downgrade_matrix_executes_fail_closed(self):
        completed, calls = self.run_previous_canary([], 204)
        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(calls, [])
        self.assertIn("exact_identifier_capability_missing", completed.stderr)

        completed, calls = self.run_previous_canary(
            ["returns_auth_canary_v2_exact_identifier"], 204
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(calls, [""])

        completed, calls = self.run_previous_canary(
            [], 404, flag=True, approval="ALLOW_ONE_LEGACY_CANARY_BOOTSTRAP"
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(
            calls, ["--allow-missing-endpoint --require-missing-endpoint"]
        )

        completed, calls = self.run_previous_canary(
            [], 204, flag=True, approval="ALLOW_ONE_LEGACY_CANARY_BOOTSTRAP"
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(
            calls, ["--allow-missing-endpoint --require-missing-endpoint"]
        )

        for flag, approval in (
            (False, ""),
            (True, ""),
            (True, "WRONG_APPROVAL"),
        ):
            with self.subTest(flag=flag, approval=approval):
                completed, calls = self.run_previous_canary(
                    [], 204, flag=flag, approval=approval
                )
                self.assertNotEqual(completed.returncode, 0)
                self.assertEqual(calls, [])

        for status in (401, 403, 500):
            with self.subTest(blocked_status=status):
                completed, calls = self.run_previous_canary(
                    [],
                    status,
                    flag=True,
                    approval="ALLOW_ONE_LEGACY_CANARY_BOOTSTRAP",
                )
                self.assertNotEqual(completed.returncode, 0)
                self.assertEqual(
                    calls, ["--allow-missing-endpoint --require-missing-endpoint"]
                )

    def test_candidate_post_deploy_requires_exact_v2_canary_before_release_record(self):
        script = (ROOT / "deploy/vds/deploy_from_git.sh").read_text(encoding="utf-8")
        candidate_call = script.rindex('run_server_auth_canary || fail')
        release_record = script.index('mv -f "$temporary_record"')
        self.assertLess(candidate_call, release_record)
        candidate_line = script[candidate_call : script.index("\n", candidate_call)]
        self.assertNotIn("allow-missing", candidate_line)

    def test_control_bundle_contains_offline_canary_dependencies(self):
        workflow = (ROOT / ".github/workflows/deploy-production.yml").read_text(encoding="utf-8")
        for path in (
            "tools/credentialed_returns_canary.py",
            "tools/validate_auth_canary_token_file.py",
            "src/taksklad/__init__.py",
            "src/taksklad/returns_auth_canary.py",
        ):
            self.assertIn(path, workflow)
        self.assertRegex(
            workflow,
            r"credentialed_returns_canary\.py[\"']?\s+--help",
        )
        self.assertNotIn("TAKSKLAD_AUTH_CANARY_TOKEN", workflow)


if __name__ == "__main__":
    unittest.main()
