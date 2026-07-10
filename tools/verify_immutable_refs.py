#!/usr/bin/env python3
"""Validate immutable GitHub Actions and shipped container references."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess
import sys
from typing import Iterable


ACTION_LINE_RE = re.compile(
    r"^\s*(?:-\s*)?uses:\s*(?P<value>[^\s#]+)(?:\s+#\s*(?P<version>\S+))?\s*$"
)
ACTION_KEY_RE = re.compile(r"^\s*(?:-\s*)?uses\s*:")
ACTION_VALUE_RE = re.compile(r"^(?P<identity>[^@]+)@(?P<sha>[0-9a-f]{40})$")
DIGESTED_IMAGE_RE = re.compile(
    r"^(?P<reference>[^@\s]+)@(?P<digest>sha256:[0-9a-f]{64})$"
)
FROM_RE = re.compile(r"^\s*FROM\s+(?:--platform=\S+\s+)?(?P<image>\S+)", re.IGNORECASE)
COMPOSE_IMAGE_RE = re.compile(r"^\s*image:\s*(?P<image>[^\s#]+)")
VERSION_COMMENT_RE = re.compile(r"^v?[0-9][0-9A-Za-z._+-]*$")
MANIFEST_PATH = Path("supply-chain/immutable-refs.json")


def repository_paths(root: Path) -> list[Path]:
    completed = subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "-z",
        ],
        check=True,
        stdout=subprocess.PIPE,
    )
    return [root / value.decode("utf-8") for value in completed.stdout.split(b"\0") if value]


def workflow_paths(paths: Iterable[Path], root: Path) -> list[Path]:
    prefix = root / ".github" / "workflows"
    return sorted(
        path
        for path in paths
        if path.is_file()
        and path.is_relative_to(prefix)
        and path.suffix.lower() in {".yml", ".yaml"}
    )


def image_definition_paths(paths: Iterable[Path]) -> list[Path]:
    result: list[Path] = []
    for path in paths:
        name = path.name.lower()
        if not path.is_file():
            continue
        if name == "dockerfile" or name.startswith("dockerfile."):
            result.append(path)
        elif "compose" in name and path.suffix.lower() in {".yml", ".yaml"}:
            result.append(path)
    return sorted(result)


def validate_action_line(line: str, location: str) -> tuple[dict[str, str] | None, str | None]:
    if not ACTION_KEY_RE.match(line):
        return None, None
    match = ACTION_LINE_RE.match(line)
    if not match:
        return None, f"{location}: malformed action reference"
    value = match.group("value")
    if value.startswith("./"):
        return None, None
    if value.startswith("docker://"):
        image = value.removeprefix("docker://")
        parsed = DIGESTED_IMAGE_RE.match(image)
        if not parsed:
            return None, f"{location}: docker action is not pinned to sha256 digest"
        return {
            "kind": "image",
            "reference": parsed.group("reference"),
            "digest": parsed.group("digest"),
        }, None
    parsed = ACTION_VALUE_RE.match(value)
    if not parsed:
        return None, f"{location}: action must use a full lowercase 40-character commit SHA"
    version = match.group("version")
    if not version or not VERSION_COMMENT_RE.fullmatch(version):
        return None, f"{location}: pinned action needs a human-readable version comment"
    return {
        "kind": "action",
        "identity": parsed.group("identity"),
        "sha": parsed.group("sha"),
        "version": version,
    }, None


def validate_image_reference(value: str, location: str) -> tuple[dict[str, str] | None, str | None]:
    value = value.strip("'\"")
    parsed = DIGESTED_IMAGE_RE.match(value)
    if not parsed:
        return None, f"{location}: image must include an immutable sha256 digest"
    reference = parsed.group("reference")
    if reference.endswith(":latest") or ":latest@" in value:
        return None, f"{location}: latest image tag is forbidden"
    return {
        "kind": "image",
        "reference": reference,
        "digest": parsed.group("digest"),
    }, None


def load_manifest(root: Path) -> dict[str, object]:
    path = root / MANIFEST_PATH
    return json.loads(path.read_text(encoding="utf-8"))


def validate_manifest_policy(manifest: dict[str, object]) -> list[str]:
    errors: list[str] = []
    if manifest.get("schema_version") != 1:
        errors.append("immutable refs manifest schema_version must be 1")
    policy = manifest.get("update_policy")
    if not isinstance(policy, dict):
        return ["immutable refs manifest has no update_policy object"]
    if policy.get("change_channel") != "owner-approved-pull-request":
        errors.append("immutable refs updates must use owner-approved-pull-request")
    if policy.get("network_mode") != "read-only-resolution":
        errors.append("immutable refs update resolution must be read-only")
    if policy.get("automatic_commits") is not False:
        errors.append("immutable refs policy must disable automatic commits")
    if policy.get("automatic_pushes") is not False:
        errors.append("immutable refs policy must disable automatic pushes")
    return errors


def expected_manifest_entries(
    manifest: dict[str, object],
) -> tuple[dict[str, tuple[str, str]], dict[str, str], list[str]]:
    errors: list[str] = []
    actions: dict[str, tuple[str, str]] = {}
    images: dict[str, str] = {}
    raw_actions = manifest.get("actions")
    raw_images = manifest.get("images")
    if not isinstance(raw_actions, list):
        errors.append("immutable refs manifest actions must be a list")
        raw_actions = []
    if not isinstance(raw_images, list):
        errors.append("immutable refs manifest images must be a list")
        raw_images = []
    for raw in raw_actions:
        if not isinstance(raw, dict):
            errors.append("immutable refs manifest action entry must be an object")
            continue
        identity = str(raw.get("uses", ""))
        sha = str(raw.get("sha", ""))
        version = str(raw.get("version", ""))
        source = str(raw.get("source", ""))
        if not identity or not re.fullmatch(r"[0-9a-f]{40}", sha):
            errors.append(f"invalid action manifest entry: {identity or '<empty>'}")
            continue
        if not VERSION_COMMENT_RE.fullmatch(version) or not source.startswith("https://"):
            errors.append(f"incomplete action provenance entry: {identity}")
            continue
        if identity in actions:
            errors.append(f"duplicate action manifest entry: {identity}")
            continue
        actions[identity] = (sha, version)
    for raw in raw_images:
        if not isinstance(raw, dict):
            errors.append("immutable refs manifest image entry must be an object")
            continue
        reference = str(raw.get("reference", ""))
        digest = str(raw.get("digest", ""))
        source = str(raw.get("source", ""))
        if not reference or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
            errors.append(f"invalid image manifest entry: {reference or '<empty>'}")
            continue
        if not source.startswith("https://"):
            errors.append(f"incomplete image provenance entry: {reference}")
            continue
        if reference in images:
            errors.append(f"duplicate image manifest entry: {reference}")
            continue
        images[reference] = digest
    return actions, images, errors


def validate_repository(root: Path) -> tuple[list[str], dict[str, int]]:
    manifest = load_manifest(root)
    errors = validate_manifest_policy(manifest)
    expected_actions, expected_images, manifest_errors = expected_manifest_entries(manifest)
    errors.extend(manifest_errors)
    found_actions: dict[str, tuple[str, str]] = {}
    found_images: dict[str, str] = {}
    paths = repository_paths(root)

    workflows = workflow_paths(paths, root)
    for path in workflows:
        relative = path.relative_to(root)
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            entry, error = validate_action_line(line, f"{relative}:{number}")
            if error:
                errors.append(error)
            elif entry and entry["kind"] == "action":
                identity = entry["identity"]
                value = (entry["sha"], entry["version"])
                previous = found_actions.setdefault(identity, value)
                if previous != value:
                    errors.append(f"{relative}:{number}: action {identity} has inconsistent pins")
            elif entry and entry["kind"] == "image":
                reference = entry["reference"]
                digest = entry["digest"]
                previous = found_images.setdefault(reference, digest)
                if previous != digest:
                    errors.append(f"{relative}:{number}: image {reference} has inconsistent digests")

    image_files = image_definition_paths(paths)
    for path in image_files:
        relative = path.relative_to(root)
        is_dockerfile = path.name.lower() == "dockerfile" or path.name.lower().startswith("dockerfile.")
        stage_names: set[str] = set()
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            match = FROM_RE.match(line) if is_dockerfile else COMPOSE_IMAGE_RE.match(line)
            if not match:
                continue
            value = match.group("image").strip("'\"")
            if is_dockerfile and (value.lower() == "scratch" or value in stage_names):
                continue
            entry, error = validate_image_reference(value, f"{relative}:{number}")
            if error:
                errors.append(error)
                continue
            assert entry is not None
            reference = entry["reference"]
            digest = entry["digest"]
            previous = found_images.setdefault(reference, digest)
            if previous != digest:
                errors.append(f"{relative}:{number}: image {reference} has inconsistent digests")
            if is_dockerfile:
                alias = re.search(r"\s+AS\s+(\S+)\s*$", line, flags=re.IGNORECASE)
                if alias:
                    stage_names.add(alias.group(1))

    for identity, value in sorted(found_actions.items()):
        if expected_actions.get(identity) != value:
            errors.append(f"action pin is absent or differs in manifest: {identity}")
    for identity in sorted(set(expected_actions) - set(found_actions)):
        errors.append(f"manifest action is not used by a workflow: {identity}")
    for reference, digest in sorted(found_images.items()):
        if expected_images.get(reference) != digest:
            errors.append(f"image digest is absent or differs in manifest: {reference}")
    for reference in sorted(set(expected_images) - set(found_images)):
        errors.append(f"manifest image is not used by a shipped definition: {reference}")

    summary = {
        "workflows": len(workflows),
        "actions": len(found_actions),
        "image_files": len(image_files),
        "images": len(found_images),
    }
    return errors, summary


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    errors, summary = validate_repository(args.root.resolve())
    if errors:
        for error in errors:
            print(f"IMMUTABLE_REFS_ERROR: {error}", file=sys.stderr)
        return 1
    print(
        "IMMUTABLE_REFS_OK "
        f"workflows={summary['workflows']} actions={summary['actions']} "
        f"image_files={summary['image_files']} images={summary['images']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
