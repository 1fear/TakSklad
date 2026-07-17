import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
GUARD = PROJECT_ROOT / "tools" / "phase27_routing_candidate_guard.sh"
ORIGINAL_ENV = "ORIGINAL_ROUTING=preserved\n"
SYNTHETIC_SECRET = "synthetic-candidate-secret"


class Phase27RoutingCandidateGuardTests(unittest.TestCase):
    def run_stage(self, stage):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            state_dir = root / "state"
            state_dir.mkdir(mode=0o700)
            persisted = root / "persisted.env"
            backup = state_dir / "backup.env"
            persisted.write_text(ORIGINAL_ENV, encoding="utf-8")
            os.chmod(persisted, 0o600)
            backup.write_text(ORIGINAL_ENV, encoding="utf-8")
            os.chmod(backup, 0o600)
            sentinel = state_dir / "foreign-sentinel"
            sentinel.write_text("foreign-file-must-remain", encoding="utf-8")
            script = r'''
set -euo pipefail
source "$GUARD"
phase27_candidate_guard_init "$STATE_DIR" "$PERSISTED" "$BACKUP"
printf '%s\n' "$SYNTHETIC_SECRET" > "$PHASE27_CANDIDATE_ENV"
chmod 600 "$PHASE27_CANDIDATE_ENV"
phase27_candidate_guard_create_compose
printf '{"secret":"%s"}\n' "$SYNTHETIC_SECRET" > "$PHASE27_CANDIDATE_COMPOSE"
phase27_candidate_guard_verify_modes
case "$STAGE" in
  before-install) exit 21 ;;
  verifier-failure) exit 22 ;;
esac
install -m 600 "$PHASE27_CANDIDATE_ENV" "$PERSISTED"
phase27_candidate_guard_mark_installed
case "$STAGE" in
  after-install) exit 23 ;;
  restart-failure) exit 24 ;;
esac
phase27_candidate_guard_commit
'''
            completed = subprocess.run(
                ["bash", "-c", script],
                text=True,
                capture_output=True,
                check=False,
                env={
                    **os.environ,
                    "GUARD": str(GUARD),
                    "STATE_DIR": str(state_dir),
                    "PERSISTED": str(persisted),
                    "BACKUP": str(backup),
                    "STAGE": stage,
                    "SYNTHETIC_SECRET": SYNTHETIC_SECRET,
                },
            )
            rendered = completed.stdout + completed.stderr
            self.assertNotIn(SYNTHETIC_SECRET, rendered)
            self.assertFalse((state_dir / "phase27-env-candidate").exists())
            self.assertFalse((state_dir / "phase27-compose-candidate.json").exists())
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "foreign-file-must-remain")
            self.assertEqual(stat.S_IMODE(state_dir.stat().st_mode), 0o700)
            if stage == "success":
                self.assertEqual(completed.returncode, 0, completed.stderr)
                self.assertIn(SYNTHETIC_SECRET, persisted.read_text(encoding="utf-8"))
            else:
                self.assertNotEqual(completed.returncode, 0)
                self.assertEqual(persisted.read_text(encoding="utf-8"), ORIGINAL_ENV)

    def test_candidates_are_removed_and_env_is_correct_for_all_exit_stages(self):
        for stage in (
            "before-install",
            "verifier-failure",
            "after-install",
            "restart-failure",
            "success",
        ):
            with self.subTest(stage=stage):
                self.run_stage(stage)

    def test_cleanup_unlinks_only_exact_candidate_symlinks_without_following(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            state_dir = root / "state"
            state_dir.mkdir(mode=0o700)
            persisted = root / "persisted.env"
            backup = state_dir / "backup.env"
            persisted.write_text(ORIGINAL_ENV, encoding="utf-8")
            backup.write_text(ORIGINAL_ENV, encoding="utf-8")
            external_env = root / "external-env"
            external_compose = root / "external-compose"
            external_env.write_text("do-not-touch-env", encoding="utf-8")
            external_compose.write_text("do-not-touch-compose", encoding="utf-8")
            (state_dir / "phase27-env-candidate").symlink_to(external_env)
            (state_dir / "phase27-compose-candidate.json").symlink_to(external_compose)
            script = r'''
set -euo pipefail
source "$GUARD"
phase27_candidate_guard_init "$STATE_DIR" "$PERSISTED" "$BACKUP"
phase27_candidate_guard_commit
'''
            completed = subprocess.run(
                ["bash", "-c", script],
                text=True,
                capture_output=True,
                check=False,
                env={
                    **os.environ,
                    "GUARD": str(GUARD),
                    "STATE_DIR": str(state_dir),
                    "PERSISTED": str(persisted),
                    "BACKUP": str(backup),
                },
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(external_env.read_text(encoding="utf-8"), "do-not-touch-env")
            self.assertEqual(external_compose.read_text(encoding="utf-8"), "do-not-touch-compose")
            self.assertFalse((state_dir / "phase27-env-candidate").exists())
            self.assertFalse((state_dir / "phase27-compose-candidate.json").exists())


if __name__ == "__main__":
    unittest.main()
