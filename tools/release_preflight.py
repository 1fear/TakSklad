#!/usr/bin/env python3
import argparse
import hashlib
import json
import os
import re
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
    Path("src/taksklad/config.py"),
    Path("src/taksklad/startup_check.py"),
    Path("src/taksklad/desktop_refresh_service.py"),
    Path("src/taksklad/app_runtime.py"),
    Path("src/taksklad/desktop_diagnostics.py"),
    Path("backend/app/operations_service.py"),
    Path("backend/app/health_service.py"),
    Path("tools/windows_backend_acceptance.ps1"),
    Path("tools/validate_deploy_probe.py"),
    Path("tools/build_windows_test_archive.ps1"),
    Path("tools/release_go_no_go.py"),
    Path("tools/verify_release_attestations.sh"),
    Path("deploy/vds/acceptance_status.sh"),
    Path("deploy/vds/deploy_from_git.sh"),
    Path("deploy/vds/docker-compose.yml"),
    Path("deploy/vds/verify_acceptance_marker.sh"),
    Path("deploy/vds/wait_acceptance_marker.sh"),
    Path("deploy/vds/verify_skladbot_coverage.sh"),
    Path("docs/windows-backend-acceptance.md"),
    Path("docs/manual-acceptance-runbook.md"),
    Path("docs/deploy-rollback-runbook.md"),
]
WINDOWS_ACCEPTANCE_HELPER_REQUIRED_FRAGMENTS = [
    "build_manifest.json",
    "Cannot verify TakSklad.exe because",
    "TakSkladAuth.exe",
    "$PinnedProductionSignerCertificateSha256",
    "$MinAppVersion = \"2.0.0\"",
    "$ExpectedBuildLabel = \"MVP 2.0\"",
    "APP_BUILD_LABEL",
    "app_build_label",
    "Compare-TakSkladVersion",
    "TAKSKLAD_BACKEND_ENABLED",
    "TAKSKLAD_BACKEND_READ_ORDERS_ENABLED",
    "TAKSKLAD_BACKEND_ONLY_REFRESH",
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
    "paused 1.1.7 nor forced $MinAppVersion rollout manifest",
]
def configured_app_version():
    config_text = (PROJECT_ROOT / "src" / "taksklad" / "config.py").read_text(encoding="utf-8")
    match = re.search(r'^APP_VERSION\s*=\s*"([^"]+)"', config_text, flags=re.MULTILINE)
    if not match:
        raise RuntimeError("APP_VERSION is missing from config.py")
    return match.group(1)


EXPECTED_RELEASE_VERSION = configured_app_version()
EXPECTED_MIN_SUPPORTED_VERSION = EXPECTED_RELEASE_VERSION
EXPECTED_PACKAGE_TYPE = "onedir_zip"
SUPPORTED_PUBLIC_PACKAGE_TYPES = {"onefile_exe", EXPECTED_PACKAGE_TYPE}
EXPECTED_RELEASE_TAG = f"v{EXPECTED_RELEASE_VERSION}"
EXPECTED_RELEASE_HOST = "github.com"
EXPECTED_RELEASE_REPO_PATH = f"/1fear/TakSklad/releases/download/{EXPECTED_RELEASE_TAG}/"
PAUSED_ROLLOUT_VERSION = "1.1.7"
PHASE_CANDIDATE = "candidate"
PHASE_FINAL = "final"
BACKEND_ONLY_CONTRACT_FRAGMENTS = {
    Path("src/taksklad/config.py"): [
        "TAKSKLAD_BACKEND_ENABLED = True",
        "TAKSKLAD_BACKEND_READ_ORDERS_ENABLED = True",
        "TAKSKLAD_BACKEND_ONLY_REFRESH",
        "TELEGRAM_DESKTOP_POLLING_ENABLED",
    ],
    Path("src/taksklad/startup_check.py"): [
        "telegram_desktop_polling",
        "backend_only_refresh",
        "pending_backend_events",
    ],
    Path("src/taksklad/desktop_refresh_service.py"): [
        "def backend_only_refresh_enabled",
        "Backend refresh недоступен",
        "pending_backend_events",
    ],
    Path("src/taksklad/desktop_diagnostics.py"): [
        "primary_source",
        "backend_only_refresh",
        "pending_backend_events",
    ],
    Path("backend/app/operations_service.py"): [
        "shadow_diagnostics",
        "backend_active_orders_source",
        "hot_path_stale_processing",
        "telegram_worker_state",
    ],
}
DEPLOY_RUNBOOK_REQUIRED_FRAGMENTS = {
    Path("docs/windows-backend-acceptance.md"): [
        "TakSkladAuth.exe",
        "/api/v1/returns/auth-canary/desktop",
        "2.0.45",
        "public channel",
    ],
    Path("docs/deploy-rollback-runbook.md"): [
        "release.json",
        "image@sha256",
        "current-release",
        "alembic downgrade",
    ],
    Path("docs/manual-acceptance-runbook.md"): [
        "--phase candidate",
        "--phase final",
        "2.0.45",
        "public channel",
        "TakSkladAuth.exe",
    ],
}
DEPLOYMENT_READINESS_CONTRACT_FRAGMENTS = {
    Path("backend/app/health_service.py"): [
        'EXPECTED_HEAD_REVISION = "20260716_0019"',
        'report["ready"]',
        'report["status"] = "unhealthy"',
    ],
    Path("deploy/vds/docker-compose.yml"): [
        "payload.get('ready') is True",
        "json.load(response)",
    ],
    Path("deploy/vds/deploy_from_git.sh"): [
        "tools/release_artifacts.py verify",
        'alembic -c alembic.ini upgrade head',
        "--no-build --pull never",
        "--wait --wait-timeout",
        '--expected-sha "$RELEASE_SOURCE_SHA"',
        '--expected-digest "$RELEASE_BACKEND_DIGEST"',
        "acceptance_status.sh --require-go",
        'TAKSKLAD_DEPLOY_ACCEPTANCE:-required',
        "tools/validate_deploy_probe.py",
    ],
    Path("tools/validate_deploy_probe.py"): [
        "readiness database contract failed",
        "readiness migration revision failed",
        "readiness mandatory policy failed",
    ],
}


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


def parse_version(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", value):
        return None
    return tuple(int(part) for part in value.split("."))


def check_version_json(root, *, phase, source_sha=None):
    path = root / VERSION_JSON
    if not path.exists():
        return result("version_json", False, error="version.json not found")
    try:
        payload = load_json(path)
    except Exception as exc:
        return result("version_json", False, error=f"invalid json: {exc}")

    if phase not in {PHASE_CANDIDATE, PHASE_FINAL}:
        return result("version_json", False, error="release phase is invalid")

    problems = []
    latest = payload.get("latest_version")
    minimum = payload.get("min_supported_version")
    latest_parts = parse_version(latest)
    minimum_parts = parse_version(minimum)
    expected_parts = parse_version(EXPECTED_RELEASE_VERSION)
    release_tag = str(payload.get("release_tag") or "")
    published_tag = f"v{latest}" if latest_parts else ""
    package_type = payload.get("package_type")

    if latest_parts is None or minimum_parts is None:
        problems.append("published channel versions must be semantic x.y.z values")
    elif minimum_parts > latest_parts:
        problems.append("min_supported_version must not exceed latest_version")
    if latest_parts is not None and expected_parts is not None and latest_parts > expected_parts:
        problems.append("published channel must not be newer than the candidate runtime")
    if release_tag != published_tag:
        problems.append("release_tag must match latest_version")
    if package_type not in SUPPORTED_PUBLIC_PACKAGE_TYPES:
        problems.append("package_type must be onefile_exe or onedir_zip")
    if not isinstance(payload.get("mandatory"), bool):
        problems.append("mandatory must be boolean")
    if payload.get("signature_type") != "authenticode" or payload.get("signature_required") is not True:
        problems.append("published channel must require Authenticode")
    if not valid_sha256(str(payload.get("signer_certificate_sha256") or "")):
        problems.append("signer_certificate_sha256 must be a lowercase SHA256 hex digest")
    if not re.fullmatch(r"[0-9a-f]{40}", str(payload.get("source_sha") or "")):
        problems.append("source_sha must be exactly 40 lowercase hex")
    if not valid_sha256(str(payload.get("dependency_lock_sha256") or "")):
        problems.append("dependency_lock_sha256 must be a lowercase SHA256 hex digest")

    required_assets = (("download_url", "sha256"),)
    if payload.get("download_url_onedir") or payload.get("sha256_onedir") or package_type == EXPECTED_PACKAGE_TYPE:
        required_assets += (("download_url_onedir", "sha256_onedir"),)
    for url_field, sha_field in required_assets:
        url = str(payload.get(url_field) or "")
        checksum = str(payload.get(sha_field) or "")
        if not valid_release_download_url(url, release_tag=published_tag):
            problems.append(f"{url_field} must be an HTTPS release URL for {published_tag or 'the published tag'}")
        if not valid_sha256(checksum):
            problems.append(f"{sha_field} must be a lowercase SHA256 hex digest")

    if phase == PHASE_FINAL:
        if not re.fullmatch(r"[0-9a-f]{40}", str(source_sha or "")):
            problems.append("final channel requires the explicit attested source SHA")
        elif payload.get("source_sha") != source_sha:
            problems.append("published source_sha must match the explicit attested source SHA")
        if latest != EXPECTED_RELEASE_VERSION or minimum != EXPECTED_MIN_SUPPORTED_VERSION:
            problems.append(f"final channel must require exact {EXPECTED_RELEASE_VERSION}")
        if payload.get("mandatory") is not True or payload.get("block_workflow") is not True:
            problems.append("final channel must be mandatory and block unsupported workflows")
        if package_type != "onefile_exe":
            problems.append("final package_type must be onefile_exe")
        if release_tag != EXPECTED_RELEASE_TAG:
            problems.append(f"final release_tag must be {EXPECTED_RELEASE_TAG}")
        if payload.get("auth_helper") != "TakSkladAuth.exe":
            problems.append("auth_helper must be TakSkladAuth.exe")
        helper_url = str(payload.get("auth_helper_download_url") or "")
        helper_sha = str(payload.get("auth_helper_sha256") or "")
        if not valid_release_download_url(helper_url, release_tag=EXPECTED_RELEASE_TAG):
            problems.append("auth_helper_download_url must be an HTTPS release URL for the final tag")
        if not valid_sha256(helper_sha):
            problems.append("auth_helper_sha256 must be a lowercase SHA256 hex digest")

    rollout_state = (
        "final-published"
        if phase == PHASE_FINAL and not problems
        else "candidate-published" if latest == EXPECTED_RELEASE_VERSION else "published-supported"
    )

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
        phase=phase,
        candidate_version=EXPECTED_RELEASE_VERSION,
        rollout_state=rollout_state,
        package_type=payload.get("package_type"),
        download_url_set=bool(payload.get("download_url")),
        download_url_onedir_set=bool(payload.get("download_url_onedir")),
        sha256_valid=valid_sha256(str(payload.get("sha256") or "")),
        sha256_onedir_valid=valid_sha256(str(payload.get("sha256_onedir") or "")),
        git_clean=git_clean,
    )


def valid_sha256(value):
    return len(value) == 64 and all(char in "0123456789abcdef" for char in value)


def valid_release_download_url(url, *, release_tag=EXPECTED_RELEASE_TAG):
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc:
        return False
    if parsed.username or parsed.password:
        return False
    if parsed.netloc.lower() != EXPECTED_RELEASE_HOST:
        return False
    expected_path = f"/1fear/TakSklad/releases/download/{release_tag}/"
    return parsed.path.startswith(expected_path)


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
    assets.append({
        "name": "auth_helper",
        "url": str(payload.get("auth_helper_download_url") or ""),
        "expected_sha256": str(payload.get("auth_helper_sha256") or ""),
    })
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
        "mandatory_update_enabled",
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


def check_text_fragment_contract(root, name, mapping):
    problems = []
    for path, fragments in mapping.items():
        file_path = root / path
        try:
            text = file_path.read_text(encoding="utf-8")
        except Exception as exc:
            problems.append(f"{path}: cannot read: {exc}")
            continue
        for fragment in fragments:
            if fragment not in text:
                problems.append(f"{path}: missing fragment: {fragment}")
    return result(name, not problems, problems=problems)


def check_backend_only_hot_path_contract(root):
    return check_text_fragment_contract(
        root,
        "backend_only_hot_path_contract",
        BACKEND_ONLY_CONTRACT_FRAGMENTS,
    )


def check_deploy_runbook_contract(root):
    return check_text_fragment_contract(
        root,
        "deploy_runbook_contract",
        DEPLOY_RUNBOOK_REQUIRED_FRAGMENTS,
    )


def check_deployment_readiness_contract(root):
    return check_text_fragment_contract(
        root,
        "deployment_readiness_contract",
        DEPLOYMENT_READINESS_CONTRACT_FRAGMENTS,
    )


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


def check_release_attestations(root, *, phase, source_sha=None):
    script = root / "tools" / "verify_release_attestations.sh"
    if not script.is_file():
        return result("release_attestations", False, error="attestation verifier is missing")
    command = ["bash", str(script), "--local"]
    mode = "local-candidate"
    if phase == PHASE_FINAL:
        if not re.fullmatch(r"[0-9a-f]{40}", str(source_sha or "")):
            return result("release_attestations", False, error="final source SHA is invalid")
        command = ["bash", str(script), "--sha", str(source_sha)]
        mode = "production-final"
    environment = os.environ.copy()
    environment["PYTHON_BIN"] = sys.executable
    try:
        completed = subprocess.run(
            command,
            cwd=root,
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=300,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return result(
            "release_attestations",
            False,
            mode=mode,
            error=exc.__class__.__name__,
        )
    return result(
        "release_attestations",
        completed.returncode == 0,
        mode=mode,
        exit_code=completed.returncode,
    )


def check_phase_contract(*, phase, skip_network, verify_downloads, source_sha):
    problems = []
    if phase not in {PHASE_CANDIDATE, PHASE_FINAL}:
        problems.append("release phase must be explicit")
    if phase == PHASE_CANDIDATE and source_sha:
        problems.append("candidate phase does not accept a production source SHA")
    if phase == PHASE_FINAL:
        if skip_network:
            problems.append("final phase cannot skip network verification")
        if not verify_downloads:
            problems.append("final phase requires immutable asset downloads")
        if not re.fullmatch(r"[0-9a-f]{40}", str(source_sha or "")):
            problems.append("final phase requires exact source SHA")
    return result("phase_contract", not problems, phase=phase, problems=problems)


def run_checks(
    root,
    health_url,
    timeout_seconds,
    *,
    phase,
    skip_network=False,
    verify_downloads=False,
    source_sha=None,
):
    phase_check = check_phase_contract(
        phase=phase,
        skip_network=skip_network,
        verify_downloads=verify_downloads,
        source_sha=source_sha,
    )
    checks = [
        phase_check,
        check_required_files(root),
        check_version_json(root, phase=phase, source_sha=source_sha),
        check_windows_acceptance_flow(root),
        check_backend_only_hot_path_contract(root),
        check_deploy_runbook_contract(root),
        check_deployment_readiness_contract(root),
        check_tracked_secrets(root),
    ]
    if phase_check["ok"]:
        checks.append(check_release_attestations(root, phase=phase, source_sha=source_sha))
    else:
        checks.append(result("release_attestations", False, skipped=True, reason="phase contract failed"))
    if skip_network:
        checks.append(result(
            "acceptance_kit",
            True,
            skipped=True,
            reason="source-only preflight does not read outputs",
        ))
    else:
        checks.append(check_acceptance_kit(root))
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


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="TakSklad 2.0 local release preflight.")
    parser.add_argument(
        "--phase",
        choices=(PHASE_CANDIDATE, PHASE_FINAL),
        required=True,
        help="Explicit candidate or final publication phase.",
    )
    parser.add_argument("--root", default=str(PROJECT_ROOT), help="Project root.")
    parser.add_argument("--health-url", default=DEFAULT_HEALTH_URL, help="Public backend health URL.")
    parser.add_argument("--timeout", type=int, default=8, help="Network timeout seconds.")
    parser.add_argument("--skip-network", action="store_true", help="Do not call public backend health URL.")
    parser.add_argument(
        "--verify-downloads",
        action="store_true",
        help="Download update artifacts from version.json and verify SHA256. Slow but useful before Windows rollout.",
    )
    parser.add_argument(
        "--source-sha",
        default="",
        help="Exact production source SHA; required only for final phase.",
    )
    args = parser.parse_args(argv)
    if args.phase == PHASE_FINAL:
        if args.skip_network:
            parser.error("final phase cannot use --skip-network")
        if not args.verify_downloads:
            parser.error("final phase requires --verify-downloads")
        if not re.fullmatch(r"[0-9a-f]{40}", args.source_sha):
            parser.error("final phase requires --source-sha with exactly 40 lowercase hex")
    elif args.source_sha:
        parser.error("candidate phase does not accept --source-sha")
    return args


def main():
    args = parse_args()
    summary = run_checks(
        Path(args.root),
        health_url=args.health_url,
        timeout_seconds=max(1, args.timeout),
        phase=args.phase,
        skip_network=args.skip_network,
        verify_downloads=args.verify_downloads,
        source_sha=args.source_sha or None,
    )
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0 if summary["status"] == "ok" else 3


if __name__ == "__main__":
    sys.exit(main())
