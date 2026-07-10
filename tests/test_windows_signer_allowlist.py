import tempfile
import unittest
from pathlib import Path

from tools.verify_windows_signer_allowlist import load_allowlist


ROOT = Path(__file__).resolve().parents[1]
FINGERPRINT = "1" * 64


class WindowsSignerAllowlistTests(unittest.TestCase):
    def test_current_source_is_explicitly_fail_closed(self):
        self.assertEqual(
            load_allowlist(ROOT / "src/taksklad/update_service.py"),
            frozenset(),
        )

    def test_literal_allowlist_entry_is_parsed(self):
        source = (
            "TRUSTED_WINDOWS_SIGNER_CERT_SHA256 = "
            f"frozenset({{{FINGERPRINT!r}}})\n"
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "trust.py"
            path.write_text(source, encoding="utf-8")
            self.assertEqual(load_allowlist(path), frozenset({FINGERPRINT}))

    def test_comment_or_unrelated_string_cannot_satisfy_allowlist(self):
        source = (
            f"# future signer {FINGERPRINT}\n"
            f"UNRELATED = {FINGERPRINT!r}\n"
            "TRUSTED_WINDOWS_SIGNER_CERT_SHA256 = frozenset()\n"
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "trust.py"
            path.write_text(source, encoding="utf-8")
            self.assertEqual(load_allowlist(path), frozenset())

    def test_nonliteral_allowlist_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "trust.py"
            path.write_text(
                "TRUSTED_WINDOWS_SIGNER_CERT_SHA256 = frozenset(load_values())\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "literal"):
                load_allowlist(path)

    def test_nested_reassignment_is_rejected(self):
        source = (
            "TRUSTED_WINDOWS_SIGNER_CERT_SHA256 = "
            f"frozenset({{{FINGERPRINT!r}}})\n"
            "if True:\n"
            "    TRUSTED_WINDOWS_SIGNER_CERT_SHA256 = frozenset()\n"
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "trust.py"
            path.write_text(source, encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "cannot be reassigned"):
                load_allowlist(path)


if __name__ == "__main__":
    unittest.main()
