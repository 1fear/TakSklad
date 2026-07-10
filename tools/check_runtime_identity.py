#!/usr/bin/env python3
"""Build and query a disposable local backend image through HTTP /health."""

from __future__ import annotations

import argparse
import ast
from io import BytesIO
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import tempfile
import time


ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")
CONTEXT_FILES = ("Dockerfile", "requirements.lock", "alembic.ini")
CONTEXT_DIRECTORIES = ("app", "migrations", "sql")


def run(command: list[str], *, timeout: int = 300) -> subprocess.CompletedProcess:
    return subprocess.run(
        command,
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
        check=False,
    )


def backend_context_digest(backend: Path) -> str:
    digest = hashlib.sha256()
    paths = [backend / name for name in CONTEXT_FILES]
    for directory in CONTEXT_DIRECTORIES:
        paths.extend(
            path for path in (backend / directory).rglob("*")
            if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
        )
    for path in sorted(paths, key=lambda item: item.relative_to(backend).as_posix()):
        relative = path.relative_to(backend).as_posix()
        digest.update(relative.encode("utf-8") + b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def app_version(backend: Path) -> str:
    tree = ast.parse((backend / "app/settings.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "APP_VERSION" for target in node.targets
        ):
            value = ast.literal_eval(node.value)
            if isinstance(value, str):
                return value
    raise ValueError("APP_VERSION not found")


def verify(identity: dict[str, str], actual: dict) -> None:
    if not SHA_RE.fullmatch(identity["commit_sha"]):
        raise ValueError("git HEAD is not an exact commit SHA")
    if not DIGEST_RE.fullmatch(identity["image_digest"]):
        raise ValueError("local image ID is not an exact sha256 digest")
    if not VERSION_RE.fullmatch(identity["version"]):
        raise ValueError("app version is not exact")
    for key, value in identity.items():
        if actual.get(key) != value:
            raise ValueError(f"runtime {key} mismatch")
    if actual.get("status") != "ok":
        raise ValueError("runtime health is not ok")


def inspect_candidate() -> dict[str, str]:
    head_result = run(["git", "rev-parse", "HEAD"], timeout=10)
    if head_result.returncode:
        raise ValueError("cannot resolve git HEAD")
    commit_sha = head_result.stdout.strip()
    archive_bytes = subprocess.run(
        ["git", "archive", "--format=tar", "HEAD", "backend"],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
        check=False,
    )
    if archive_bytes.returncode:
        raise ValueError("cannot archive exact git HEAD backend context")
    with tempfile.TemporaryDirectory(prefix="taksklad-identity-context-") as temporary:
        with tarfile.open(fileobj=BytesIO(archive_bytes.stdout), mode="r:") as stream:
            if any(member.name.startswith("/") or ".." in Path(member.name).parts for member in stream.getmembers()):
                raise ValueError("unsafe path in git archive")
            stream.extractall(temporary)
        backend = Path(temporary) / "backend"
        context_digest = backend_context_digest(backend)
        tag = f"taksklad-local-identity:{context_digest[:20]}"
        inspect = run(["docker", "image", "inspect", tag, "--format", "{{.Id}}"], timeout=30)
        if inspect.returncode:
            build = run(["docker", "build", "--pull=false", "--tag", tag, str(backend)], timeout=300)
            if build.returncode:
                raise ValueError("disposable backend image build failed: " + build.stdout[-1200:].replace("\n", " "))
            inspect = run(["docker", "image", "inspect", tag, "--format", "{{.Id}}"], timeout=30)
        image_digest = inspect.stdout.strip()
        version = app_version(backend)
    identity = {"commit_sha": commit_sha, "image_digest": image_digest, "version": version}
    container = ""
    try:
        started = run([
            "docker", "run", "--detach", "--network", "none",
            "--env", "TAKSKLAD_ENV=local",
            "--env", "TAKSKLAD_INSECURE_LOCAL_ANONYMOUS=true",
            "--env", f"TAKSKLAD_COMMIT_SHA={commit_sha}",
            "--env", f"TAKSKLAD_IMAGE_DIGEST={image_digest}",
            tag,
        ], timeout=30)
        if started.returncode:
            raise ValueError("disposable backend container failed to start")
        container = started.stdout.strip()
        actual = None
        probe = None
        for _ in range(40):
            probe = run([
                "docker", "exec", container, "python", "-c",
                "import json,sys; from urllib.request import urlopen; sys.stdout.write(json.dumps(json.load(urlopen('http://127.0.0.1:8000/health', timeout=2)), sort_keys=True))",
            ], timeout=10)
            if probe.returncode == 0:
                actual = json.loads(probe.stdout.strip().splitlines()[-1])
                break
            time.sleep(0.25)
        if actual is None:
            logs = run(["docker", "logs", container], timeout=10)
            raise ValueError(
                "HTTP /health did not become ready: "
                + ((probe.stdout if probe else "") + logs.stdout)[-900:].replace("\n", " ")
            )
        verify(identity, actual)
    finally:
        if container:
            run(["docker", "rm", "--force", container], timeout=30)
    return {
        **identity,
        "backend_context_sha256": context_digest,
        "source_state": "committed-head",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--local-stack", action="store_true")
    args = parser.parse_args(argv)
    if not args.local_stack:
        parser.error("--local-stack is required")
    try:
        result = inspect_candidate()
    except (OSError, ValueError, json.JSONDecodeError, subprocess.SubprocessError) as exc:
        sys.stderr.write(f"RUNTIME_IDENTITY_FAIL error={exc}\n")
        return 1
    sys.stdout.write(
        "RUNTIME_IDENTITY_OK "
        + " ".join(f"{key}={value}" for key, value in result.items())
        + " endpoint=http://127.0.0.1:8000/health authority=disposable-local-image\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
