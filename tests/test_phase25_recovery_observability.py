import json
import os
from pathlib import Path
import stat
import tempfile
import unittest

from tools.write_maintenance_marker import write_marker


ROOT = Path(__file__).resolve().parents[1]


class Phase25RecoveryObservabilityTests(unittest.TestCase):
    def test_marker_preserves_only_approved_fields_and_replaces_atomically(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "maintenance.json"
            first = write_marker(path, "backup", at="2026-07-11T10:00:00Z")
            inode = path.stat().st_ino
            second = write_marker(path, "restore_drill", at="2026-07-11T11:00:00+00:00")

            self.assertEqual(first, {"backup_success_at": "2026-07-11T10:00:00Z"})
            self.assertEqual(set(second), {"backup_success_at", "restore_drill_success_at"})
            self.assertNotEqual(path.stat().st_ino, inode)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o644)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), second)
            self.assertEqual(list(path.parent.glob(".maintenance-*")), [])

    def test_marker_refuses_symlink(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "target.json"
            target.write_text("{}", encoding="utf-8")
            link = root / "maintenance.json"
            os.symlink(target, link)
            with self.assertRaisesRegex(ValueError, "symlink"):
                write_marker(link, "backup")

    def test_successful_backup_and_restore_drill_publish_markers(self):
        backup = (ROOT / "deploy/vds/backup_postgres.sh").read_text(encoding="utf-8")
        restore = (ROOT / "deploy/vds/restore_drill.sh").read_text(encoding="utf-8")
        self.assertIn('write_maintenance_marker.py" backup', backup)
        self.assertGreater(backup.index('write_maintenance_marker.py" backup'), backup.index('mv "$staging_dir" "$bundle_dir"'))
        self.assertIn('write_maintenance_marker.py" restore_drill', restore)
        self.assertGreater(restore.index('write_maintenance_marker.py" restore_drill'), restore.index('dr_recovery.py" restore-drill'))

        compose = (ROOT / "deploy/vds/docker-compose.yml").read_text(encoding="utf-8")
        backend = compose.split("  backend-api:", 1)[1].split("\n  frontend:", 1)[0]
        self.assertIn("source: /run/taksklad-observability", backend)
        self.assertIn("target: /run/taksklad-observability", backend)
        self.assertIn("read_only: true", backend)


if __name__ == "__main__":
    unittest.main()
