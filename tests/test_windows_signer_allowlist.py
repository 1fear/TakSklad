import hashlib
import ssl
import tempfile
import unittest
from pathlib import Path

from tools.verify_windows_signer_allowlist import load_allowlist
from tools.verify_windows_signing_chain import verify_chain


ROOT = Path(__file__).resolve().parents[1]
FINGERPRINT = "1" * 64
APPROVED_FINGERPRINT = (
    "c95ccd968831b3b55a1f2c949e66f3b39c5f69badf29a70887b43a036f14bb19"
)


class WindowsSignerAllowlistTests(unittest.TestCase):
    def test_current_source_pins_only_the_approved_signer(self):
        self.assertEqual(
            load_allowlist(ROOT / "src/taksklad/update_service.py"),
            frozenset({APPROVED_FINGERPRINT}),
        )

    def test_committed_public_certificate_matches_the_pinned_fingerprint(self):
        pem = (
            ROOT / "supply-chain/taksklad-internal-windows-codesign.pem"
        ).read_text(encoding="ascii")
        certificate_der = ssl.PEM_cert_to_DER_cert(pem)
        self.assertEqual(
            hashlib.sha256(certificate_der).hexdigest(),
            APPROVED_FINGERPRINT,
        )

    def test_committed_internal_ca_chain_is_valid_and_pinned(self):
        leaf_sha256, root_sha256 = verify_chain()
        self.assertEqual(leaf_sha256, APPROVED_FINGERPRINT)
        self.assertEqual(
            root_sha256,
            "a6a8df2e724a01ff25ad64eddec1a81e60c9a5658e050636f1630aa9a4fdff8b",
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
