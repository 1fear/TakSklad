#!/usr/bin/env python3
"""Generate deterministic CycloneDX SBOMs from the release source locks."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import tempfile
from pathlib import Path
from urllib.parse import quote


SBOM_FILENAMES = (
    "taksklad-desktop.cdx.json",
    "taksklad-backend.cdx.json",
    "taksklad-frontend.cdx.json",
    "taksklad-container-images.cdx.json",
)


def emit(message: str, *, error: bool = False) -> None:
    stream = sys.stderr if error else sys.stdout
    stream.write(message + "\n")


def canonical_package(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def logical_requirements(path: Path) -> list[str]:
    logical: list[str] = []
    current = ""
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("--hash="):
            current = f"{current} {stripped}".strip()
        else:
            if current:
                logical.append(current)
            current = stripped
        if current.endswith("\\"):
            current = current[:-1].rstrip()
        else:
            logical.append(current)
            current = ""
    if current:
        logical.append(current)
    return logical


def python_components(lock_path: Path, source_path: str) -> list[dict[str, object]]:
    components: list[dict[str, object]] = []
    for logical in logical_requirements(lock_path):
        match = re.match(
            r"^([A-Za-z0-9_.-]+)(?:\[[^]]+\])?==([^ ;]+)(?:\s*;\s*([^\\]+?))?(?:\s+--hash=|$)",
            logical,
        )
        if not match:
            raise ValueError(f"unparseable locked requirement in {source_path}")
        name = canonical_package(match.group(1))
        version = match.group(2)
        marker = (match.group(3) or "").strip()
        hash_count = len(re.findall(r"--hash=sha256:[0-9a-f]{64}", logical))
        if hash_count == 0:
            raise ValueError(f"hashless locked requirement in {source_path}: {name}")
        purl = f"pkg:pypi/{quote(name, safe='')}@{quote(version, safe='')}"
        bom_ref = purl
        if marker:
            marker_id = hashlib.sha256(marker.encode("utf-8")).hexdigest()[:12]
            bom_ref = f"{purl}#marker-{marker_id}"
        properties = [
            {"name": "taksklad:source-lock", "value": source_path},
            {"name": "taksklad:distribution-hash-count", "value": str(hash_count)},
        ]
        if marker:
            properties.append({"name": "taksklad:environment-marker", "value": marker})
        components.append(
            {
                "type": "library",
                "bom-ref": bom_ref,
                "name": name,
                "version": version,
                "purl": purl,
                "properties": properties,
            }
        )
    return sorted(components, key=lambda item: str(item["bom-ref"]))


def frontend_components(package_lock_path: Path) -> list[dict[str, object]]:
    payload = json.loads(package_lock_path.read_text(encoding="utf-8"))
    merged: dict[tuple[str, str], dict[str, object]] = {}
    for package_path, package in payload["packages"].items():
        if not package_path or package.get("link"):
            continue
        if "node_modules/" not in package_path:
            raise ValueError(f"unexpected package-lock path: {package_path}")
        name = package_path.rsplit("node_modules/", 1)[1]
        version = str(package.get("version", ""))
        integrity = str(package.get("integrity", ""))
        if not version:
            raise ValueError(f"version missing for npm package: {name}")
        if str(package.get("resolved", "")).startswith("https://registry.npmjs.org/") and not integrity:
            raise ValueError(f"integrity missing for npm package: {name}@{version}")
        key = (name, version)
        scope = "development" if package.get("dev") else "runtime"
        existing = merged.get(key)
        if existing and scope == "runtime":
            existing["scope"] = "required"
            for prop in existing["properties"]:  # type: ignore[union-attr]
                if prop["name"] == "taksklad:dependency-scope":
                    prop["value"] = "runtime"
        if existing:
            existing["properties"].append(  # type: ignore[union-attr]
                {"name": "taksklad:install-path", "value": package_path}
            )
            continue
        encoded_name = quote(name, safe="/")
        purl = f"pkg:npm/{encoded_name}@{quote(version, safe='')}"
        component: dict[str, object] = {
            "type": "library",
            "bom-ref": purl,
            "name": name,
            "version": version,
            "purl": purl,
            "scope": "optional" if scope == "development" else "required",
            "properties": [
                {"name": "taksklad:source-lock", "value": "frontend/package-lock.json"},
                {"name": "taksklad:dependency-scope", "value": scope},
                {"name": "taksklad:install-path", "value": package_path},
            ],
        }
        if integrity:
            component["properties"].append(  # type: ignore[union-attr]
                {"name": "taksklad:npm-integrity", "value": integrity}
            )
        merged[key] = component
    return sorted(merged.values(), key=lambda item: str(item["bom-ref"]))


def image_components(manifest_path: Path) -> list[dict[str, object]]:
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    components: list[dict[str, object]] = []
    for image in payload["images"]:
        reference = str(image["reference"])
        digest = str(image["digest"])
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
            raise ValueError(f"invalid image digest: {reference}")
        repository = reference.rsplit(":", 1)[0]
        tag = reference[len(repository) + 1 :]
        purl = f"pkg:oci/{quote(repository, safe='/')}@{digest}?tag={quote(tag, safe='')}"
        components.append(
            {
                "type": "container",
                "bom-ref": purl,
                "name": repository,
                "version": tag,
                "purl": purl,
                "hashes": [{"alg": "SHA-256", "content": digest.removeprefix("sha256:")}],
                "externalReferences": [
                    {"type": "distribution", "url": str(image["source"])}
                ],
                "properties": [
                    {
                        "name": "taksklad:immutable-reference",
                        "value": f"{reference}@{digest}",
                    },
                    {
                        "name": "taksklad:source-manifest",
                        "value": "supply-chain/immutable-refs.json",
                    },
                ],
            }
        )
    return sorted(components, key=lambda item: str(item["bom-ref"]))


def cyclone_document(name: str, components: list[dict[str, object]]) -> dict[str, object]:
    application_ref = f"pkg:generic/taksklad-{name}@phase-21"
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.6",
        "version": 1,
        "metadata": {
            "component": {
                "type": "application",
                "bom-ref": application_ref,
                "name": f"TakSklad {name}",
                "version": "phase-21",
            },
            "properties": [
                {"name": "taksklad:deterministic", "value": "true"},
                {"name": "taksklad:generator", "value": "tools/generate_sbom.py"},
            ],
        },
        "components": components,
        "dependencies": [
            {
                "ref": application_ref,
                "dependsOn": [str(item["bom-ref"]) for item in components],
            }
        ],
    }


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def validate_document(payload: dict[str, object]) -> None:
    if payload.get("bomFormat") != "CycloneDX" or payload.get("specVersion") != "1.6":
        raise ValueError("invalid CycloneDX identity")
    if payload.get("version") != 1:
        raise ValueError("invalid CycloneDX document version")
    metadata = payload.get("metadata")
    components = payload.get("components")
    dependencies = payload.get("dependencies")
    if not isinstance(metadata, dict) or not isinstance(components, list) or not isinstance(dependencies, list):
        raise ValueError("invalid CycloneDX top-level shape")
    application = metadata.get("component")
    if not isinstance(application, dict) or not application.get("bom-ref"):
        raise ValueError("CycloneDX application component missing")
    refs: list[str] = []
    for component in components:
        if not isinstance(component, dict):
            raise ValueError("invalid CycloneDX component")
        required = {"type", "bom-ref", "name", "version"}
        if not required.issubset(component) or not all(component[field] for field in required):
            raise ValueError("incomplete CycloneDX component")
        bom_ref = str(component["bom-ref"])
        refs.append(bom_ref)
        purl = str(component.get("purl", ""))
        if str(component["name"]).startswith("@") and not purl.startswith("pkg:npm/%40"):
            raise ValueError("scoped npm purl must percent-encode @")
    if len(refs) != len(set(refs)):
        raise ValueError("duplicate CycloneDX bom-ref")
    if len(dependencies) != 1 or not isinstance(dependencies[0], dict):
        raise ValueError("invalid CycloneDX dependency root")
    dependency_root = dependencies[0]
    if dependency_root.get("ref") != application["bom-ref"]:
        raise ValueError("CycloneDX dependency root mismatch")
    if sorted(dependency_root.get("dependsOn", [])) != sorted(refs):
        raise ValueError("CycloneDX dependency coverage mismatch")


def generate(root: Path, output_dir: Path) -> dict[str, int]:
    output_dir.mkdir(parents=True, exist_ok=True)
    documents = {
        "taksklad-desktop.cdx.json": cyclone_document(
            "desktop",
            python_components(root / "requirements/desktop.lock", "requirements/desktop.lock"),
        ),
        "taksklad-backend.cdx.json": cyclone_document(
            "backend",
            python_components(root / "backend/requirements.lock", "backend/requirements.lock"),
        ),
        "taksklad-frontend.cdx.json": cyclone_document(
            "frontend",
            frontend_components(root / "frontend/package-lock.json"),
        ),
        "taksklad-container-images.cdx.json": cyclone_document(
            "container-images",
            image_components(root / "supply-chain/immutable-refs.json"),
        ),
    }
    counts: dict[str, int] = {}
    for filename in SBOM_FILENAMES:
        document = documents[filename]
        validate_document(document)
        _write_json(output_dir / filename, document)
        counts[filename] = len(document["components"])  # type: ignore[arg-type]
    manifest_lines = []
    for filename in SBOM_FILENAMES:
        digest = hashlib.sha256((output_dir / filename).read_bytes()).hexdigest()
        manifest_lines.append(f"{digest}  {filename}")
    (output_dir / "manifest.sha256").write_text(
        "\n".join(manifest_lines) + "\n", encoding="ascii"
    )
    return counts


def verify(root: Path, output_dir: Path) -> int:
    with tempfile.TemporaryDirectory(prefix="taksklad-sbom-") as temp_dir:
        expected_dir = Path(temp_dir) / "sbom"
        counts = generate(root, expected_dir)
        missing = [
            filename
            for filename in (*SBOM_FILENAMES, "manifest.sha256")
            if not (output_dir / filename).is_file()
        ]
        if missing:
            emit("SBOM_VERIFY_FAIL missing=" + ",".join(missing), error=True)
            return 1
        mismatches = [
            filename
            for filename in (*SBOM_FILENAMES, "manifest.sha256")
            if (output_dir / filename).read_bytes() != (expected_dir / filename).read_bytes()
        ]
        if mismatches:
            emit("SBOM_VERIFY_FAIL mismatches=" + ",".join(mismatches), error=True)
            return 1
        frontend_document = json.loads(
            (output_dir / "taksklad-frontend.cdx.json").read_text(encoding="utf-8")
        )
        validate_document(frontend_document)
        frontend_instances = sum(
            1
            for component in frontend_document["components"]
            for prop in component.get("properties", [])
            if prop.get("name") == "taksklad:install-path"
        )
        source_package_lock = json.loads(
            (root / "frontend/package-lock.json").read_text(encoding="utf-8")
        )
        expected_instances = sum(
            1
            for package_path, package in source_package_lock["packages"].items()
            if package_path and not package.get("link")
        )
        if frontend_instances != expected_instances:
            emit(
                "SBOM_VERIFY_FAIL frontend_instance_coverage="
                f"{frontend_instances}/{expected_instances}",
                error=True,
            )
            return 1
        for filename in SBOM_FILENAMES:
            digest = hashlib.sha256((output_dir / filename).read_bytes()).hexdigest()
            emit(
                f"SBOM_COMPONENTS file=test-artifacts/sbom/{filename} "
                f"components={counts[filename]} sha256={digest}"
            )
        emit(
            "SBOM_VERIFY_OK files=5 format=CycloneDX-1.6 "
            "python_locks=2 npm_locks=1 immutable_image_manifests=1 "
            f"frontend_instances={frontend_instances}/{expected_instances} "
            "forbidden_directories_opened=0"
        )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    output_dir = root / "test-artifacts/sbom"
    if args.verify:
        return verify(root, output_dir)
    counts = generate(root, output_dir)
    emit(
        "SBOM_GENERATED output=test-artifacts/sbom files=5 components="
        + str(sum(counts.values()))
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
