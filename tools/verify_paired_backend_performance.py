#!/usr/bin/env python3
"""Compare a candidate to the approved commit on the same host at the same time."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from tools import benchmark_backend


ROOT = Path(__file__).resolve().parents[1]
EVIDENCE_DIR = ROOT / ".release-state" / "performance"
APPROVED_BASELINE = EVIDENCE_DIR / "backend-baseline-approved.json"
OUTPUT_PATH = EVIDENCE_DIR / "paired-compare-reference.json"
MEASUREMENT_CONTRACT_KEYS = ("runner", "profiles", "budgets")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
ENV_ALLOWLIST = (
    "PATH", "HOME", "USER", "SHELL", "LANG", "LC_ALL", "TERM",
    "DOCKER_HOST", "XDG_RUNTIME_DIR", "SSH_AUTH_SOCK",
    "TAKSKLAD_POSTGRES_TEST_IMAGE",
)

WORKER_CODE = r"""
import json
import sys
import time
from pathlib import Path

from tools import benchmark_backend as benchmark

profile_name, barrier_root, side = sys.argv[1:4]
barrier_root = Path(barrier_root)
barrier_index = 0


def paired_barrier():
    global barrier_index
    barrier_index += 1
    own = barrier_root / f"{barrier_index:02d}-{side}.ready"
    peer_side = "candidate" if side == "control" else "control"
    peer = barrier_root / f"{barrier_index:02d}-{peer_side}.ready"
    own.write_text("ready\n", encoding="utf-8")
    deadline = time.monotonic() + 600
    while not peer.is_file():
        if time.monotonic() >= deadline:
            raise RuntimeError(f"paired workload barrier timed out index={barrier_index}")
        time.sleep(0.05)
    return {"paired": True, "barrier_index": barrier_index}


benchmark.wait_for_benchmark_quiescence = paired_barrier
benchmark.ensure_foreground_task_policy()
profile = benchmark.load_json(benchmark.PROFILES_PATH)["profiles"][profile_name]
with benchmark.disposable_database() as (database_url, runtime):
    dataset, dataset_path = benchmark.seed_profile(database_url, profile_name)
    benchmark.prepare_profile_benchmark(database_url)
    context = benchmark.workload_context(database_url, profile)
    results = benchmark.measure_profile_workloads(database_url, context, 100)
    host = benchmark.host_manifest(database_url, runtime)

sys.stdout.write(json.dumps({
    "dataset_manifest": str(dataset_path.relative_to(benchmark.ROOT)),
    "dataset_counts": dataset["table_counts"],
    "host": host,
    "results": results,
}, sort_keys=True) + "\n")
"""


class PairedPerformanceError(RuntimeError):
    pass


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def measurement_contract_failures(
    approved: dict[str, Any], current_hashes: dict[str, str],
) -> list[str]:
    expected = ((approved.get("host") or {}).get("working_tree_source_hashes") or {})
    return [
        f"paired measurement contract hash mismatch: {key}"
        for key in MEASUREMENT_CONTRACT_KEYS
        if expected.get(key) != current_hashes.get(key)
    ]


def synthetic_pair(*, control: float, candidate: float) -> dict[str, Any]:
    def side(value: float) -> dict[str, Any]:
        return {
            "results": {
                "queue_claim_50": {
                    "durations_ms": [value] * 500,
                    "p95_ms": value,
                    "p99_ms": value,
                    "query_count": {"min": 1, "median": 1, "max": 1},
                    "rows_returned": {"min": 50, "median": 50, "max": 50},
                }
            }
        }

    return {"control": side(control), "candidate": side(candidate)}


def candidate_absolute_failures(
    pairs: list[dict[str, Any]], budgets: dict[str, Any], *, workloads: tuple[str, ...],
) -> list[str]:
    failures = []
    for pair_number, pair in enumerate(pairs, 1):
        races = pair.get("races") or [pair]
        for race_number, race in enumerate(races, 1):
            results = race["candidate"]["results"]
            for workload in workloads:
                for metric, limit in budgets["workloads"][workload].items():
                    value = float(results[workload][metric])
                    if value > float(limit):
                        failures.append(
                            f"pair {pair_number} race {race_number} candidate "
                            f"{workload}.{metric}={value} exceeds absolute budget {limit}"
                        )
    return failures


def aggregate_pair_failures(
    pairs: list[dict[str, Any]], budgets: dict[str, Any], *, workloads: tuple[str, ...],
) -> tuple[list[str], dict[str, dict[str, float]]]:
    if len(pairs) < 3:
        raise ValueError("paired comparison requires at least three independent pairs")
    regression_limit = float(budgets.get("regression_limit_percent") or 0)
    if regression_limit != 10:
        raise ValueError("paired regression limit must remain exactly 10%")
    limit_ratio = 1 + regression_limit / 100
    failures = []
    medians: dict[str, dict[str, float]] = {}
    for workload in workloads:
        medians[workload] = {}
        for metric in ("p95_ms", "p99_ms"):
            ratios = []
            for pair in pairs:
                races = pair.get("races") or [pair]
                if pair.get("races") is not None and len(races) != 2:
                    raise ValueError("launch-balanced pair must contain exactly two races")
                control_samples = []
                candidate_samples = []
                for race in races:
                    control_durations = race["control"]["results"][workload].get("durations_ms") or []
                    candidate_durations = race["candidate"]["results"][workload].get("durations_ms") or []
                    if not control_durations or len(control_durations) != len(candidate_durations):
                        raise ValueError(
                            f"paired raw sample count mismatch: {workload}.{metric}"
                        )
                    control_samples.extend(float(value) for value in control_durations)
                    candidate_samples.extend(float(value) for value in candidate_durations)
                percentile = 95 if metric == "p95_ms" else 99
                control = float(benchmark_backend.percentile(control_samples, percentile))
                candidate = float(benchmark_backend.percentile(candidate_samples, percentile))
                if control <= 0:
                    raise ValueError(
                        f"paired control metric must be positive: {workload}.{metric}"
                    )
                ratios.append(candidate / control)
            median = float(benchmark_backend.percentile(ratios, 50))
            medians[workload][metric] = round(median, 6)
            if median > limit_ratio:
                failures.append(
                    f"median paired ratio {workload}.{metric}={median:.6f} exceeds 1.100000"
                )
    return failures, medians


def parity_failures(
    pairs: list[dict[str, Any]], *, workloads: tuple[str, ...],
) -> list[str]:
    failures = []
    for pair_number, pair in enumerate(pairs, 1):
        races = pair.get("races") or [pair]
        for race_number, race in enumerate(races, 1):
            for workload in workloads:
                control = race["control"]["results"][workload]
                candidate = race["candidate"]["results"][workload]
                if candidate["query_count"]["median"] > control["query_count"]["median"]:
                    failures.append(
                        f"pair {pair_number} race {race_number} {workload} query-count regression"
                    )
                if candidate["rows_returned"]["median"] != control["rows_returned"]["median"]:
                    failures.append(
                        f"pair {pair_number} race {race_number} {workload} row-count mismatch"
                    )
    return failures


def worker_environment() -> dict[str, str]:
    environment = {key: os.environ[key] for key in ENV_ALLOWLIST if key in os.environ}
    environment.update({
        "CI": "true",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": ".",
        "TAKSKLAD_ENV": "test",
        "TAKSKLAD_NO_PRODUCTION": "1",
        "TAKSKLAD_EXTERNAL_SENDS_DISABLED": "1",
        "TAKSKLAD_EVENT_LEASES_ENABLED": "0",
        "SKLADBOT_SKU_MAPPING_JSON": "",
    })
    return environment


def add_control_worktree(commit: str, destination: Path) -> None:
    completed = subprocess.run(
        ["git", "worktree", "add", "--detach", "--force", str(destination), commit],
        cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False,
    )
    if completed.returncode:
        raise PairedPerformanceError("cannot create approved-control worktree")


def remove_control_worktree(destination: Path) -> bool:
    completed = subprocess.run(
        ["git", "worktree", "remove", "--force", str(destination)],
        cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False,
    )
    subprocess.run(
        ["git", "worktree", "prune"], cwd=ROOT,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
    )
    return completed.returncode == 0 and not destination.exists()


def _decode_worker(process: subprocess.Popen[str], side: str) -> dict[str, Any]:
    try:
        stdout, stderr = process.communicate(timeout=1800)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()
        raise PairedPerformanceError(f"{side} worker timed out")
    if process.returncode:
        tail = "\n".join((stdout + "\n" + stderr).splitlines()[-10:])[-2000:]
        raise PairedPerformanceError(f"{side} worker failed: {tail}")
    try:
        return json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise PairedPerformanceError(f"{side} worker returned invalid JSON") from exc


def run_pair(
    *,
    profile: str,
    control_root: Path,
    candidate_root: Path,
    barrier_root: Path,
    candidate_first: bool = False,
) -> dict[str, Any]:
    barrier_root.mkdir(parents=True, exist_ok=True)
    environment = worker_environment()
    command = [sys.executable, "-c", WORKER_CODE, profile, str(barrier_root)]
    def start(side: str) -> subprocess.Popen[str]:
        root = candidate_root if side == "candidate" else control_root
        return subprocess.Popen(
            [*command, side], cwd=root, env=environment,
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )

    if candidate_first:
        candidate = start("candidate")
        control = start("control")
    else:
        control = start("control")
        candidate = start("candidate")
    try:
        control_result = _decode_worker(control, "control")
        candidate_result = _decode_worker(candidate, "candidate")
    except BaseException:
        for process in (control, candidate):
            if process.poll() is None:
                process.terminate()
        for process in (control, candidate):
            try:
                process.communicate(timeout=30)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
        raise
    return {"control": control_result, "candidate": candidate_result}


def run(profile: str, repeat: int, assert_budgets: bool) -> dict[str, Any]:
    if repeat != 3:
        raise ValueError("Phase 26 paired comparison requires exactly three pairs")
    approved = load_json(APPROVED_BASELINE)
    current_hashes = benchmark_backend.benchmark_contract_hashes()
    failures = measurement_contract_failures(approved, current_hashes)
    control_commit = str((approved.get("host") or {}).get("commit") or "")
    if not SHA_RE.fullmatch(control_commit):
        raise PairedPerformanceError("approved baseline control commit is invalid")
    exists = subprocess.run(
        ["git", "cat-file", "-e", f"{control_commit}^{{commit}}"], cwd=ROOT,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
    )
    if exists.returncode:
        raise PairedPerformanceError("approved baseline control commit is unavailable")

    worktree_parent = Path(tempfile.mkdtemp(prefix="paired-control-", dir=ROOT / ".release-state"))
    control_root = worktree_parent / "source"
    pair_root = Path(tempfile.mkdtemp(prefix="paired-runs-", dir=ROOT / ".release-state"))
    cleanup_ok = False
    pairs = []
    try:
        add_control_worktree(control_commit, control_root)
        control_hashes = {
            "runner": sha256_file(control_root / "tools/benchmark_backend.py"),
            "profiles": sha256_file(control_root / "performance/backend_profiles.json"),
            "budgets": sha256_file(control_root / "performance/backend_budgets.json"),
        }
        failures.extend(measurement_contract_failures(approved, control_hashes))
        if failures:
            raise PairedPerformanceError("; ".join(failures))
        for pair_number in range(1, repeat + 1):
            control_first = run_pair(
                profile=profile, control_root=control_root, candidate_root=ROOT,
                barrier_root=pair_root / f"pair-{pair_number}" / "control-first",
            )
            candidate_first = run_pair(
                profile=profile, control_root=control_root, candidate_root=ROOT,
                barrier_root=pair_root / f"pair-{pair_number}" / "candidate-first",
                candidate_first=True,
            )
            pairs.append({
                "balance": "pooled raw samples from control-first and candidate-first races",
                "races": [control_first, candidate_first],
            })
    finally:
        if control_root.exists():
            cleanup_ok = remove_control_worktree(control_root)
        else:
            cleanup_ok = True
        shutil.rmtree(worktree_parent, ignore_errors=True)
        shutil.rmtree(pair_root, ignore_errors=True)
        cleanup_ok = cleanup_ok and not worktree_parent.exists() and not pair_root.exists()
    if not cleanup_ok:
        raise PairedPerformanceError("paired comparison cleanup failed")

    workloads = tuple(benchmark_backend.WORKLOADS)
    absolute = candidate_absolute_failures(pairs, benchmark_backend.load_json(benchmark_backend.BUDGETS_PATH), workloads=workloads)
    regressions, median_ratios = aggregate_pair_failures(
        pairs, benchmark_backend.load_json(benchmark_backend.BUDGETS_PATH), workloads=workloads,
    )
    parity = parity_failures(pairs, workloads=workloads)
    failures = absolute + regressions + parity
    enforced = failures if assert_budgets else []
    evidence = {
        "schema": 1,
        "mode": "concurrent_paired_compare",
        "profile": profile,
        "repeat": repeat,
        "races_per_pair": 2,
        "control_commit": control_commit,
        "candidate_commit": subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
        ).stdout.strip(),
        "approved_baseline": str(APPROVED_BASELINE.relative_to(ROOT)),
        "approved_baseline_sha256": sha256_file(APPROVED_BASELINE),
        "regression_limit_percent": 10,
        "pairing": (
            "simultaneous control/candidate with per-workload filesystem barriers and "
            "swapped launch order; mirrored raw samples pooled before p95/p99"
        ),
        "measurement_contract_keys": list(MEASUREMENT_CONTRACT_KEYS),
        "median_paired_ratios": median_ratios,
        "pairs": pairs,
        "cleanup_zero": cleanup_ok,
        "production_mutations": 0,
        "external_sends": 0,
        "failures": enforced,
        "status": "pass" if not enforced else "fail",
    }
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return evidence


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", choices=("reference",), required=True)
    parser.add_argument("--repeat", type=int, required=True)
    parser.add_argument("--assert-budgets", action="store_true")
    args = parser.parse_args(argv)
    try:
        evidence = run(args.profile, args.repeat, args.assert_budgets)
    except (OSError, ValueError, PairedPerformanceError, subprocess.SubprocessError) as exc:
        sys.stdout.write(f"PAIRED_BACKEND_PERFORMANCE_FAIL error={str(exc)[-2000:]}\n")
        return 1
    sys.stdout.write(json.dumps({
        "status": evidence["status"],
        "control_commit": evidence["control_commit"],
        "candidate_commit": evidence["candidate_commit"],
        "repeat": evidence["repeat"],
        "regression_limit_percent": evidence["regression_limit_percent"],
        "median_paired_ratios": evidence["median_paired_ratios"],
        "failures": evidence["failures"],
        "production_mutations": 0,
        "external_sends": 0,
        "evidence": str(OUTPUT_PATH.relative_to(ROOT)),
    }, sort_keys=True) + "\n")
    return 0 if evidence["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
