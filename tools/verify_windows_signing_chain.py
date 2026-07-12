#!/usr/bin/env python3
"""Fail-closed validation for the committed internal Windows signing chain."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import ExtendedKeyUsageOID

from tools.verify_windows_signer_allowlist import load_allowlist


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ROOT_CERT = ROOT / "supply-chain/taksklad-internal-windows-root-ca.pem"
DEFAULT_LEAF_CERT = ROOT / "supply-chain/taksklad-internal-windows-codesign.pem"
DEFAULT_ALLOWLIST = ROOT / "src/taksklad/update_service.py"


def _certificate(path: Path) -> x509.Certificate:
    return x509.load_pem_x509_certificate(path.read_bytes())


def verify_chain(
    *,
    root_path: Path = DEFAULT_ROOT_CERT,
    leaf_path: Path = DEFAULT_LEAF_CERT,
    allowlist_path: Path = DEFAULT_ALLOWLIST,
    required_leaf_sha256: str = "",
) -> tuple[str, str]:
    root = _certificate(root_path)
    leaf = _certificate(leaf_path)
    now = datetime.now(timezone.utc)

    root_constraints = root.extensions.get_extension_for_class(
        x509.BasicConstraints
    ).value
    root_usage = root.extensions.get_extension_for_class(x509.KeyUsage).value
    if not root_constraints.ca or root_constraints.path_length != 0:
        raise ValueError("WINDOWS_CODESIGN_ROOT_CA_INVALID")
    if not root_usage.key_cert_sign or not root_usage.crl_sign:
        raise ValueError("WINDOWS_CODESIGN_ROOT_KEY_USAGE_INVALID")
    if root.subject != root.issuer:
        raise ValueError("WINDOWS_CODESIGN_ROOT_NOT_SELF_ISSUED")

    leaf_constraints = leaf.extensions.get_extension_for_class(
        x509.BasicConstraints
    ).value
    leaf_usage = leaf.extensions.get_extension_for_class(x509.KeyUsage).value
    leaf_eku = leaf.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    if leaf_constraints.ca or leaf_usage.key_cert_sign or not leaf_usage.digital_signature:
        raise ValueError("WINDOWS_CODESIGN_LEAF_USAGE_INVALID")
    if set(leaf_eku) != {ExtendedKeyUsageOID.CODE_SIGNING}:
        raise ValueError("WINDOWS_CODESIGN_LEAF_EKU_INVALID")
    if leaf.issuer != root.subject:
        raise ValueError("WINDOWS_CODESIGN_ISSUER_MISMATCH")
    if not (root.not_valid_before_utc <= now <= root.not_valid_after_utc):
        raise ValueError("WINDOWS_CODESIGN_ROOT_EXPIRED_OR_NOT_YET_VALID")
    if not (leaf.not_valid_before_utc <= now <= leaf.not_valid_after_utc):
        raise ValueError("WINDOWS_CODESIGN_LEAF_EXPIRED_OR_NOT_YET_VALID")

    root_public_key = root.public_key()
    if not isinstance(root_public_key, rsa.RSAPublicKey):
        raise ValueError("WINDOWS_CODESIGN_ROOT_RSA_REQUIRED")
    root_public_key.verify(
        root.signature,
        root.tbs_certificate_bytes,
        padding.PKCS1v15(),
        root.signature_hash_algorithm,
    )
    root_public_key.verify(
        leaf.signature,
        leaf.tbs_certificate_bytes,
        padding.PKCS1v15(),
        leaf.signature_hash_algorithm,
    )

    leaf_sha256 = leaf.fingerprint(hashes.SHA256()).hex()
    root_sha256 = root.fingerprint(hashes.SHA256()).hex()
    allowlist = load_allowlist(allowlist_path)
    if allowlist != frozenset({leaf_sha256}):
        raise ValueError("WINDOWS_CODESIGN_IDENTITY_NOT_PINNED")
    required = required_leaf_sha256.strip().lower()
    if required and required != leaf_sha256:
        raise ValueError("WINDOWS_CODESIGN_IDENTITY_MISMATCH")
    return leaf_sha256, root_sha256


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT_CERT)
    parser.add_argument("--leaf", type=Path, default=DEFAULT_LEAF_CERT)
    parser.add_argument("--allowlist", type=Path, default=DEFAULT_ALLOWLIST)
    parser.add_argument("--require-leaf", default="")
    args = parser.parse_args()
    try:
        leaf_sha256, root_sha256 = verify_chain(
            root_path=args.root,
            leaf_path=args.leaf,
            allowlist_path=args.allowlist,
            required_leaf_sha256=args.require_leaf,
        )
    except (OSError, ValueError, x509.ExtensionNotFound) as exc:
        print(f"WINDOWS_SIGNING_CHAIN_ERROR: {exc}")
        return 1
    print(
        "WINDOWS_SIGNING_CHAIN_OK "
        f"leaf_sha256={leaf_sha256} root_sha256={root_sha256}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
