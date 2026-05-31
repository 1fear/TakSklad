#!/usr/bin/env python3
import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_HEALTH_URL = "https://api.taksklad.uz/health"
ACCEPTANCE_DIR = Path("outputs/taksklad_acceptance")
MANIFEST_NAME = "acceptance_manifest.json"
VERSION_JSON = Path("version.json")
SECRET_FILE_NAMES = {
    "credentials.json",
    "TakSklad_data.json",
    "telegram_settings.json",
    "yandex_geocoder_key.txt",
    "pending_saves.json",
    "pending_prints.json",
    "pending_telegram.json",
    "pending_backend_events.json",
    ".env",
}
REQUIRED_FILES = [
    Path("tools/windows_backend_acceptance.ps1"),
    Path("tools/build_windows_test_archive.ps1"),
    Path("tools/release_go_no_go.py"),
    Path("deploy/vds/acceptance_status.sh"),
    Path("deploy/vds/verify_acceptance_marker.sh"),
    Path("deploy/vds/wait_acceptance_marker.sh"),
    Path("docs/windows-backend-acceptance.md"),
    Path("docs/manual-acceptance-runbook.md"),
]
WINDOWS_ACCEPTANCE_HELPER_REQUIRED_FRAGMENTS = [
    "build_manifest.json",
    "Cannot verify TakSklad.exe version",
    "$MinAppVersion = \"2.0.0\"",
    "$ExpectedBuildLabel = \"MVP 2.0\"",
    "APP_BUILD_LABEL",
    "app_build_label",
    "Compare-TakSkladVersion",
    "TAKSKLAD_BACKEND_ENABLED",
    "TAKSKLAD_BACKEND_READ_ORDERS_ENABLED",
]
WINDOWS_TEST_BUILD_REQUIRED_FRAGMENTS = [
    "build_manifest.json",
    "$ExpectedBuildLabel = \"MVP 2.0\"",
    "APP_BUILD_LABEL",
    "app_build_label",
    "ACCEPTANCE_RESULTS_TEMPLATE.md",
    "ACCEPTANCE_RESULTS.md",
    "Assert-TestPackageDoesNotContainLocalSecrets",
    "version.json has local changes",
    "stable 1.1.7",
]


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as file_obj:
        for chunk in iter(lambda: file_obj.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def result(name, ok, **details):
    return {"name": name, "ok": bool(ok), **details}


def check_required_files(root):
    missing = [str(path) for path in REQUIRED_FILES if not (root / path).exists()]
    return result("required_files", not missing, missing=missing)


def check_version_json(root):
    path = root / VERSION_JSON
    if not path.exists():
        return result("version_json", False, error="version.json not found")
    try:
        payload = load_json(path)
    except Exception as exc:
        return result("version_json", False, error=f"invalid json: {exc}")

    problems = []
    if payload.get("latest_version") != "1.1.7":
        problems.append("latest_version is not pinned to 1.1.7")
    if payload.get("min_supported_version") != "1.1.7":
        problems.append("min_supported_version is not pinned to 1.1.7")
    if payload.get("mandatory") not in (False, None):
        problems.append("mandatory must be false before rollout")
    if payload.get("download_url") or payload.get("download_url_onedir"):
        problems.append("download_url fields must stay empty before rollout")

    git_clean = None
    git = shutil.which("git")
    if git:
        inside_worktree = subprocess.run(
            [git, "rev-parse", "--is-inside-work-tree"],
            cwd=root,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if inside_worktree.returncode != 0 or inside_worktree.stdout.strip() != "true":
            git_clean = None
        else:
            completed = subprocess.run(
                [git, "diff", "--quiet", "--", str(VERSION_JSON)],
                cwd=root,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            git_clean = completed.returncode == 0
            if not git_clean:
                problems.append("version.json has local git diff")

    return result(
        "version_json",
        not problems,
        problems=problems,
        latest_version=payload.get("latest_version"),
        min_supported_version=payload.get("min_supported_version"),
        mandatory=payload.get("mandatory"),
        git_clean=git_clean,
    )


def check_acceptance_kit(root):
    manifest_path = root / ACCEPTANCE_DIR / MANIFEST_NAME
    if not manifest_path.exists():
        return result("acceptance_kit", False, error="acceptance manifest not found")
    try:
        manifest = load_json(manifest_path)
    except Exception as exc:
        return result("acceptance_kit", False, error=f"invalid manifest: {exc}")

    excel_path = root / ACCEPTANCE_DIR / str(manifest.get("excel_file") or "")
    result_template_path = root / ACCEPTANCE_DIR / str(manifest.get("result_template") or "")
    result_file_name = str(manifest.get("result_file") or "ACCEPTANCE_RESULTS.md")
    result_file_path = root / ACCEPTANCE_DIR / result_file_name
    problems = []
    actual_sha = ""
    if not excel_path.exists():
        problems.append(f"acceptance Excel not found: {excel_path.relative_to(root)}")
    else:
        actual_sha = sha256_file(excel_path)
        if actual_sha != manifest.get("excel_sha256"):
            problems.append("acceptance Excel SHA mismatch")
    if not result_template_path.exists():
        problems.append("acceptance result template not found")
    if not result_file_path.exists():
        problems.append("acceptance result file not found")
    marker = str(manifest.get("marker") or "")
    if "ACCEPTANCE" not in marker:
        problems.append("marker must contain ACCEPTANCE")
    safety = manifest.get("safety") or {}
    for key in ("no_version_json_change", "no_github_release", "no_push_notifications", "contains_secrets"):
        if key == "contains_secrets":
            if safety.get(key) is not False:
                problems.append("manifest safety.contains_secrets must be false")
        elif safety.get(key) is not True:
            problems.append(f"manifest safety.{key} must be true")

    return result(
        "acceptance_kit",
        not problems,
        problems=problems,
        marker=marker,
        excel_file=manifest.get("excel_file"),
        result_file=result_file_name,
        excel_sha256=actual_sha,
        expected=manifest.get("expected"),
    )


def tracked_files(root):
    git = shutil.which("git")
    if not git:
        return None
    completed = subprocess.run(
        [git, "ls-files"],
        cwd=root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if completed.returncode != 0:
        return None
    return [line.strip() for line in completed.stdout.splitlines() if line.strip()]


def check_tracked_secrets(root):
    files = tracked_files(root)
    if files is None:
        return result("tracked_secrets", True, skipped=True, reason="git unavailable")
    found = [
        file_path
        for file_path in files
        if Path(file_path).name in SECRET_FILE_NAMES and not file_path.endswith(".env.example")
    ]
    return result("tracked_secrets", not found, found=found)


def check_windows_acceptance_flow(root):
    problems = []
    helper_path = root / "tools/windows_backend_acceptance.ps1"
    build_path = root / "tools/build_windows_test_archive.ps1"

    try:
        helper_text = helper_path.read_text(encoding="utf-8")
    except Exception as exc:
        return result("windows_acceptance_flow", False, error=f"cannot read helper: {exc}")
    try:
        build_text = build_path.read_text(encoding="utf-8")
    except Exception as exc:
        return result("windows_acceptance_flow", False, error=f"cannot read build helper: {exc}")

    for fragment in WINDOWS_ACCEPTANCE_HELPER_REQUIRED_FRAGMENTS:
        if fragment not in helper_text:
            problems.append(f"windows acceptance helper missing fragment: {fragment}")
    for fragment in WINDOWS_TEST_BUILD_REQUIRED_FRAGMENTS:
        if fragment not in build_text:
            problems.append(f"windows test build helper missing fragment: {fragment}")

    return result("windows_acceptance_flow", not problems, problems=problems)


def check_public_backend_health(url, timeout_seconds):
    request = urllib.request.Request(url, headers={"User-Agent": "TakSklad-release-preflight/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            body = response.read(32 * 1024).decode("utf-8", errors="replace")
            status_code = response.status
    except Exception as exc:
        return result("public_backend_health", False, url=url, error=exc.__class__.__name__)
    try:
        payload = json.loads(body)
    except Exception:
        payload = {"raw": body[:300]}
    return result(
        "public_backend_health",
        status_code == 200 and payload.get("status") == "ok",
        url=url,
        status_code=status_code,
        response=payload,
    )


def run_checks(root, health_url, timeout_seconds, skip_network=False):
    checks = [
        check_required_files(root),
        check_version_json(root),
        check_acceptance_kit(root),
        check_windows_acceptance_flow(root),
        check_tracked_secrets(root),
    ]
    if skip_network:
        checks.append(result("public_backend_health", True, skipped=True))
    else:
        checks.append(check_public_backend_health(health_url, timeout_seconds))
    return {
        "status": "ok" if all(item["ok"] for item in checks) else "failed",
        "checks": checks,
    }


def parse_args():
    parser = argparse.ArgumentParser(description="TakSklad 2.0 local release preflight.")
    parser.add_argument("--root", default=str(PROJECT_ROOT), help="Project root.")
    parser.add_argument("--health-url", default=DEFAULT_HEALTH_URL, help="Public backend health URL.")
    parser.add_argument("--timeout", type=int, default=8, help="Network timeout seconds.")
    parser.add_argument("--skip-network", action="store_true", help="Do not call public backend health URL.")
    return parser.parse_args()


def main():
    args = parse_args()
    summary = run_checks(
        Path(args.root),
        health_url=args.health_url,
        timeout_seconds=max(1, args.timeout),
        skip_network=args.skip_network,
    )
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0 if summary["status"] == "ok" else 3


if __name__ == "__main__":
    sys.exit(main())
