#!/usr/bin/env python3
import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import urllib.parse
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
    Path("deploy/vds/verify_google_backend_sync.sh"),
    Path("deploy/vds/verify_skladbot_coverage.sh"),
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
    "safe non-mandatory 2.0.3 rollout manifest",
]
EXPECTED_RELEASE_VERSION = "2.0.3"
EXPECTED_MIN_SUPPORTED_VERSION = "1.1.7"
EXPECTED_PACKAGE_TYPE = "onefile_exe"
EXPECTED_RELEASE_TAG = f"v{EXPECTED_RELEASE_VERSION}"


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
    if payload.get("latest_version") != EXPECTED_RELEASE_VERSION:
        problems.append(f"latest_version must be {EXPECTED_RELEASE_VERSION}")
    if payload.get("min_supported_version") != EXPECTED_MIN_SUPPORTED_VERSION:
        problems.append(f"min_supported_version must stay {EXPECTED_MIN_SUPPORTED_VERSION} for non-forced rollout")
    if payload.get("mandatory") not in (False, None):
        problems.append("mandatory must be false during staged rollout")
    if payload.get("package_type") != EXPECTED_PACKAGE_TYPE:
        problems.append(f"package_type must be {EXPECTED_PACKAGE_TYPE}")
    if not payload.get("download_url") or not payload.get("sha256"):
        problems.append("onefile download_url and sha256 must be set")
    if not payload.get("download_url_onedir") or not payload.get("sha256_onedir"):
        problems.append("onedir download_url_onedir and sha256_onedir must be set")
    for field_name in ("download_url", "download_url_onedir"):
        url = str(payload.get(field_name) or "")
        if url and not valid_release_download_url(url):
            problems.append(f"{field_name} must be an HTTPS release URL for {EXPECTED_RELEASE_TAG}")
    for field_name in ("sha256", "sha256_onedir"):
        checksum = str(payload.get(field_name) or "")
        if checksum and not valid_sha256(checksum):
            problems.append(f"{field_name} must be a lowercase SHA256 hex digest")

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
        package_type=payload.get("package_type"),
        download_url_set=bool(payload.get("download_url")),
        download_url_onedir_set=bool(payload.get("download_url_onedir")),
        sha256_valid=valid_sha256(str(payload.get("sha256") or "")),
        sha256_onedir_valid=valid_sha256(str(payload.get("sha256_onedir") or "")),
        git_clean=git_clean,
    )


def valid_sha256(value):
    return len(value) == 64 and all(char in "0123456789abcdef" for char in value)


def valid_release_download_url(url):
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc:
        return False
    if parsed.username or parsed.password:
        return False
    return f"/releases/download/{EXPECTED_RELEASE_TAG}/" in parsed.path


def sha256_url(url, timeout_seconds):
    digest = hashlib.sha256()
    request = urllib.request.Request(url, headers={"User-Agent": "TakSklad-release-preflight/1.0"})
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def check_update_manifest_downloads(root, timeout_seconds):
    path = root / VERSION_JSON
    try:
        payload = load_json(path)
    except Exception as exc:
        return result("update_manifest_downloads", False, error=f"cannot read version.json: {exc}")

    assets = [
        {
            "name": "onefile",
            "url": str(payload.get("download_url") or ""),
            "expected_sha256": str(payload.get("sha256") or ""),
        },
        {
            "name": "onedir",
            "url": str(payload.get("download_url_onedir") or ""),
            "expected_sha256": str(payload.get("sha256_onedir") or ""),
        },
    ]
    checked = []
    problems = []
    for asset in assets:
        name = asset["name"]
        url = asset["url"]
        expected = asset["expected_sha256"]
        if not url or not expected:
            problems.append(f"{name} URL/SHA is missing")
            checked.append({**asset, "actual_sha256": ""})
            continue
        try:
            actual = sha256_url(url, timeout_seconds)
        except Exception as exc:
            problems.append(f"{name} download failed: {exc.__class__.__name__}")
            checked.append({**asset, "actual_sha256": ""})
            continue
        checked.append({**asset, "actual_sha256": actual})
        if actual != expected:
            problems.append(f"{name} SHA mismatch")

    return result(
        "update_manifest_downloads",
        not problems,
        problems=problems,
        assets=checked,
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
    required_true_safety = (
        "version_json_staged_rollout",
        "github_release_published",
        "push_notifications_allowed",
        "mandatory_update_disabled",
    )
    for key in (*required_true_safety, "contains_secrets"):
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


def run_checks(root, health_url, timeout_seconds, skip_network=False, verify_downloads=False):
    checks = [
        check_required_files(root),
        check_version_json(root),
        check_acceptance_kit(root),
        check_windows_acceptance_flow(root),
        check_tracked_secrets(root),
    ]
    if verify_downloads:
        checks.append(check_update_manifest_downloads(root, timeout_seconds))
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
    parser.add_argument(
        "--verify-downloads",
        action="store_true",
        help="Download update artifacts from version.json and verify SHA256. Slow but useful before Windows rollout.",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    summary = run_checks(
        Path(args.root),
        health_url=args.health_url,
        timeout_seconds=max(1, args.timeout),
        skip_network=args.skip_network,
        verify_downloads=args.verify_downloads,
    )
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0 if summary["status"] == "ok" else 3


if __name__ == "__main__":
    sys.exit(main())
