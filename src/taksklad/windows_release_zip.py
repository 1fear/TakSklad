"""Strict validation and extraction for the signed Windows onedir package."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import unicodedata
import zipfile


ROOT = "TakSklad"
MANIFEST_MEMBERS = {
    f"{ROOT}/version.json",
    f"{ROOT}/build_manifest.json",
}
MAX_MEMBERS = 4096
MAX_MEMBER_BYTES = 512 * 1024 * 1024
MAX_TOTAL_BYTES = 2 * 1024 * 1024 * 1024
HEX_RE = re.compile(r"^[0-9a-f]{64}$")
NATIVE_PATH = type(Path.cwd())
WINDOWS_ILLEGAL_COMPONENT = re.compile(r'[\x00-\x1f<>:"|?*]')
WINDOWS_RESERVED_COMPONENTS = {
    "con",
    "conin$",
    "conout$",
    "prn",
    "aux",
    "nul",
    *(f"com{number}" for number in range(1, 10)),
    *(f"lpt{number}" for number in range(1, 10)),
    *(f"com{number}" for number in ("¹", "²", "³")),
    *(f"lpt{number}" for number in ("¹", "²", "³")),
}


class WindowsReleaseZipError(ValueError):
    pass


def _digest_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _read_member(archive: zipfile.ZipFile, info: zipfile.ZipInfo) -> bytes:
    if info.file_size < 0 or info.file_size > MAX_MEMBER_BYTES:
        raise WindowsReleaseZipError("member_size_invalid")
    with archive.open(info, "r") as source:
        payload = source.read(MAX_MEMBER_BYTES + 1)
    if len(payload) != info.file_size or len(payload) > MAX_MEMBER_BYTES:
        raise WindowsReleaseZipError("member_size_mismatch")
    return payload


def _windows_component_key(component: str) -> str:
    if (
        not component
        or component in {".", ".."}
        or component.endswith((" ", "."))
        or WINDOWS_ILLEGAL_COMPONENT.search(component)
    ):
        raise WindowsReleaseZipError("member_path_invalid")
    normalized = unicodedata.normalize("NFC", component)
    if (
        not normalized
        or normalized in {".", ".."}
        or normalized.endswith((" ", "."))
        or WINDOWS_ILLEGAL_COMPONENT.search(normalized)
    ):
        raise WindowsReleaseZipError("member_path_invalid")
    folded = normalized.casefold()
    device_base = folded.split(".", 1)[0].rstrip(" .")
    if device_base in WINDOWS_RESERVED_COMPONENTS:
        raise WindowsReleaseZipError("member_path_invalid")
    return folded


def _validated_path(name: str, *, require_root: bool) -> tuple[PurePosixPath, str]:
    if (
        not name
        or "\\" in name
        or name.startswith(("/", "//"))
        or re.match(r"^[A-Za-z]:", name)
        or name.endswith("/")
    ):
        raise WindowsReleaseZipError("member_path_invalid")
    raw_parts = name.split("/")
    if any(part in {"", ".", ".."} for part in raw_parts):
        raise WindowsReleaseZipError("member_path_invalid")
    path = PurePosixPath(name)
    if str(path) != name:
        raise WindowsReleaseZipError("member_path_invalid")
    if require_root:
        if len(raw_parts) < 2 or raw_parts[0] != ROOT:
            raise WindowsReleaseZipError("member_root_invalid")
        relative_parts = raw_parts[1:]
        root_key = _windows_component_key(raw_parts[0])
        normalized_key = "/".join((root_key, *(_windows_component_key(part) for part in relative_parts)))
    else:
        normalized_key = "/".join(_windows_component_key(part) for part in raw_parts)
    return path, normalized_key


def _canonical_info(info: zipfile.ZipInfo) -> str:
    name = info.filename
    _validated_path(name, require_root=True)
    unix_mode = (info.external_attr >> 16) & 0xFFFF
    file_type = stat.S_IFMT(unix_mode)
    if file_type not in {0, stat.S_IFREG} or info.is_dir():
        raise WindowsReleaseZipError("member_type_invalid")
    if info.flag_bits & 0x1:
        raise WindowsReleaseZipError("encrypted_member_forbidden")
    return name


def _parse_json(payload: bytes, label: str) -> dict:
    try:
        value = json.loads(payload.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WindowsReleaseZipError(f"{label}_invalid") from exc
    if not isinstance(value, dict):
        raise WindowsReleaseZipError(f"{label}_invalid")
    return value


def _inventory(manifest: dict) -> dict[str, tuple[str, int]]:
    rows = manifest.get("package_files")
    if not isinstance(rows, list) or not rows or len(rows) > MAX_MEMBERS:
        raise WindowsReleaseZipError("package_inventory_invalid")
    result: dict[str, tuple[str, int]] = {}
    normalized_paths: set[str] = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"path", "sha256", "size"}:
            raise WindowsReleaseZipError("package_inventory_invalid")
        relative = str(row.get("path") or "")
        try:
            _path, normalized_path = _validated_path(relative, require_root=False)
        except WindowsReleaseZipError as exc:
            raise WindowsReleaseZipError("package_inventory_path_invalid") from exc
        if (
            normalized_path in normalized_paths
            or relative in {"version.json", "build_manifest.json"}
        ):
            raise WindowsReleaseZipError("package_inventory_path_invalid")
        digest = str(row.get("sha256") or "")
        size = row.get("size")
        if not HEX_RE.fullmatch(digest) or not isinstance(size, int) or not 0 <= size <= MAX_MEMBER_BYTES:
            raise WindowsReleaseZipError("package_inventory_metadata_invalid")
        normalized_paths.add(normalized_path)
        result[relative] = (digest, size)
    return result


def validate_windows_release_zip(
    zip_path: os.PathLike[str] | str,
    outer_manifest: dict,
    *,
    expected_source_sha: str | None = None,
) -> dict:
    required_outer = {
        "app_sha256_onedir",
        "auth_helper_sha256_onedir",
        "acceptance_wrapper_sha256",
    }
    if not isinstance(outer_manifest, dict) or any(
        not HEX_RE.fullmatch(str(outer_manifest.get(field) or "")) for field in required_outer
    ):
        raise WindowsReleaseZipError("outer_manifest_hashes_invalid")
    outer_version = str(
        outer_manifest.get("latest_version") or outer_manifest.get("app_version") or ""
    )
    outer_release_tag = str(outer_manifest.get("release_tag") or "")
    outer_source_sha = str(outer_manifest.get("source_sha") or "")
    outer_signer = str(outer_manifest.get("signer_certificate_sha256") or "")
    if (
        not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", outer_version)
        or outer_release_tag != f"v{outer_version}"
        or not re.fullmatch(r"[0-9a-f]{40}", outer_source_sha)
        or not HEX_RE.fullmatch(outer_signer)
        or outer_manifest.get("signature_required") is not True
        or outer_manifest.get("package_type") != "onedir_zip"
        or str(outer_manifest.get("acceptance_wrapper") or "windows_backend_acceptance.ps1")
        != "windows_backend_acceptance.ps1"
        or str(outer_manifest.get("auth_helper") or "TakSkladAuth.exe") != "TakSkladAuth.exe"
    ):
        raise WindowsReleaseZipError("outer_manifest_identity_invalid")
    if expected_source_sha is not None and outer_source_sha != expected_source_sha:
        raise WindowsReleaseZipError("outer_source_sha_mismatch")
    try:
        archive = zipfile.ZipFile(zip_path, "r")
    except (OSError, zipfile.BadZipFile) as exc:
        raise WindowsReleaseZipError("zip_invalid") from exc
    with archive:
        infos = archive.infolist()
        if not infos or len(infos) > MAX_MEMBERS:
            raise WindowsReleaseZipError("member_count_invalid")
        if sum(info.file_size for info in infos) > MAX_TOTAL_BYTES:
            raise WindowsReleaseZipError("archive_size_invalid")
        by_name: dict[str, zipfile.ZipInfo] = {}
        normalized_paths: set[str] = set()
        for info in infos:
            name = _canonical_info(info)
            _path, normalized_path = _validated_path(name, require_root=True)
            if name in by_name or normalized_path in normalized_paths:
                raise WindowsReleaseZipError("duplicate_or_case_collision")
            by_name[name] = info
            normalized_paths.add(normalized_path)
        if not MANIFEST_MEMBERS <= set(by_name):
            raise WindowsReleaseZipError("inner_manifest_missing")
        inner_version_payload = _read_member(archive, by_name[f"{ROOT}/version.json"])
        build_manifest_payload = _read_member(archive, by_name[f"{ROOT}/build_manifest.json"])
        inner_version = _parse_json(inner_version_payload, "version")
        build_manifest = _parse_json(
            build_manifest_payload, "build_manifest"
        )
        version_inventory = _inventory(inner_version)
        build_inventory = _inventory(build_manifest)
        if version_inventory != build_inventory:
            raise WindowsReleaseZipError("inner_inventory_mismatch")
        actual_relative = {name[len(ROOT) + 1 :] for name in by_name if name not in MANIFEST_MEMBERS}
        if actual_relative != set(version_inventory):
            raise WindowsReleaseZipError("archive_membership_mismatch")
        required_names = {
            "TakSklad.exe",
            "TakSkladAuth.exe",
            "windows_backend_acceptance.ps1",
        }
        if not required_names <= actual_relative:
            raise WindowsReleaseZipError("required_member_missing")
        for relative, (expected_digest, expected_size) in version_inventory.items():
            payload = _read_member(archive, by_name[f"{ROOT}/{relative}"])
            if len(payload) != expected_size or _digest_bytes(payload) != expected_digest:
                raise WindowsReleaseZipError("inventory_content_mismatch")

    expected_identity = {
        "TakSklad.exe": str(outer_manifest["app_sha256_onedir"]),
        "TakSkladAuth.exe": str(outer_manifest["auth_helper_sha256_onedir"]),
        "windows_backend_acceptance.ps1": str(outer_manifest["acceptance_wrapper_sha256"]),
    }
    for name, digest in expected_identity.items():
        if version_inventory.get(name, (None, None))[0] != digest:
            raise WindowsReleaseZipError("outer_inner_identity_mismatch")
    for manifest in (inner_version, build_manifest):
        if manifest.get("package_type") != "onedir_zip":
            raise WindowsReleaseZipError("inner_package_type_invalid")
        if manifest.get("source_sha") != outer_source_sha:
            raise WindowsReleaseZipError("inner_source_sha_mismatch")
        if manifest.get("app_version") != outer_version:
            raise WindowsReleaseZipError("inner_version_identity_mismatch")
        if manifest.get("signer_certificate_sha256") != outer_signer:
            raise WindowsReleaseZipError("inner_signer_identity_mismatch")
        if manifest.get("signature_required") is not True:
            raise WindowsReleaseZipError("inner_signature_policy_mismatch")
        if manifest.get("app_sha256") != expected_identity["TakSklad.exe"]:
            raise WindowsReleaseZipError("inner_app_identity_mismatch")
        if manifest.get("auth_helper_sha256") != expected_identity["TakSkladAuth.exe"]:
            raise WindowsReleaseZipError("inner_helper_identity_mismatch")
        if manifest.get("acceptance_wrapper_sha256") != expected_identity["windows_backend_acceptance.ps1"]:
            raise WindowsReleaseZipError("inner_wrapper_identity_mismatch")
    if (
        inner_version.get("release_tag") != outer_release_tag
        or inner_version.get("auth_helper") != "TakSkladAuth.exe"
        or inner_version.get("acceptance_wrapper") != "windows_backend_acceptance.ps1"
        or build_manifest.get("release_tag") != outer_release_tag
        or build_manifest.get("app_path_for_acceptance") != "TakSklad.exe"
        or build_manifest.get("auth_helper_path_for_acceptance") != "TakSkladAuth.exe"
        or build_manifest.get("acceptance_wrapper") != "windows_backend_acceptance.ps1"
    ):
        raise WindowsReleaseZipError("inner_manifest_identity_mismatch")
    return {
        "members": by_name,
        "inventory": version_inventory,
        "version": inner_version,
        "build_manifest": build_manifest,
        "manifest_hashes": {
            "version.json": _digest_bytes(inner_version_payload),
            "build_manifest.json": _digest_bytes(build_manifest_payload),
        },
    }


def extract_windows_release_zip(
    zip_path: os.PathLike[str] | str,
    destination: os.PathLike[str] | str,
    outer_manifest: dict,
    *,
    expected_source_sha: str | None = None,
) -> Path:
    validated = validate_windows_release_zip(
        zip_path, outer_manifest, expected_source_sha=expected_source_sha
    )
    destination = NATIVE_PATH(destination)
    if destination.exists():
        raise WindowsReleaseZipError("destination_must_not_exist")
    staging_root = destination.resolve(strict=False)
    planned_targets = {}
    for name in validated["members"]:
        path, _normalized = _validated_path(name, require_root=True)
        target = staging_root.joinpath(*path.parts)
        resolved_target = target.resolve(strict=False)
        if not resolved_target.is_relative_to(staging_root):
            raise WindowsReleaseZipError("extraction_target_outside_staging")
        planned_targets[name] = target
    destination.mkdir(mode=0o700, parents=False)
    if destination.is_symlink() or destination.resolve() != staging_root:
        raise WindowsReleaseZipError("staging_root_identity_mismatch")
    try:
        with zipfile.ZipFile(zip_path, "r") as archive:
            for name, info in validated["members"].items():
                target = planned_targets[name]
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                if not target.resolve(strict=False).is_relative_to(staging_root):
                    raise WindowsReleaseZipError("extraction_target_outside_staging")
                payload = _read_member(archive, info)
                with target.open("xb") as output:
                    output.write(payload)
                    output.flush()
                    os.fsync(output.fileno())
        root = destination / ROOT
        for relative, (digest, size) in validated["inventory"].items():
            path = root.joinpath(*PurePosixPath(relative).parts)
            payload = path.read_bytes()
            if len(payload) != size or _digest_bytes(payload) != digest:
                raise WindowsReleaseZipError("post_extract_identity_mismatch")
        for relative, digest in validated["manifest_hashes"].items():
            path = root / relative
            if _digest_bytes(path.read_bytes()) != digest:
                raise WindowsReleaseZipError("post_extract_manifest_identity_mismatch")
        return root
    except Exception:
        # Caller owns cleanup of the new isolated destination; never merge it into an install.
        raise
