import argparse
import hashlib
import json
import os
import platform
import random
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

from taksklad import storage


def nearest_rank(values, percentile):
    ordered = sorted(values)
    rank = max(1, (len(ordered) * percentile + 99) // 100)
    return ordered[min(len(ordered), rank) - 1]


def summarize_ms(values_ns):
    values_ms = [value / 1_000_000 for value in values_ns]
    return {
        "p50_ms": round(nearest_rank(values_ms, 50), 3),
        "p95_ms": round(nearest_rank(values_ms, 95), 3),
        "p99_ms": round(nearest_rank(values_ms, 99), 3),
        "max_ms": round(max(values_ms), 3),
    }


def sanitized_host_manifest():
    benchmark_path = Path(__file__).resolve()
    storage_path = Path(storage.__file__).resolve()

    def sha256(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    return {
        "os": platform.system(),
        "os_release": platform.release(),
        "architecture": platform.machine(),
        "python": platform.python_version(),
        "sqlite": sqlite3.sqlite_version,
        "storage_profile": os.environ.get(
            "TAKSKLAD_STORAGE_PROFILE",
            "local-durable-single-event-commit",
        ),
        "benchmark_sha256": sha256(benchmark_path),
        "storage_sha256": sha256(storage_path),
        "windows_target_evidence": sys.platform == "win32",
    }


def run_benchmark(event_count):
    rng = random.Random(20260710)
    event_numbers = list(range(event_count))
    rng.shuffle(event_numbers)
    durations = []

    with tempfile.TemporaryDirectory(prefix="taksklad-storage-benchmark-") as temp_dir:
        original_data_file = storage.TAKSKLAD_DATA_FILE
        storage.TAKSKLAD_DATA_FILE = str(Path(temp_dir) / "TakSklad_data.json")
        try:
            for warmup in range(50):
                storage.append_queue_item("pending_saves", {"id": f"warmup-{warmup}", "codes": []})
            storage.replace_queue_section("pending_saves", [])

            for number in event_numbers:
                started = time.perf_counter_ns()
                storage.append_queue_item(
                    "pending_saves",
                    {"id": f"scan-{number:08d}", "codes": [f"synthetic-{number:08d}"]},
                )
                durations.append(time.perf_counter_ns() - started)

            items = storage.load_queue_section("pending_saves")
        finally:
            storage.TAKSKLAD_DATA_FILE = original_data_file

    ids = [item.get("id") for item in items]
    unique_ids = set(ids)
    latency = summarize_ms(durations)
    return {
        "seed": 20260710,
        "synthetic_events": event_count,
        "durability": "sqlite-wal-synchronous-full-one-commit-per-event",
        "latency": latency,
        "scan_to_durable_feedback_p95_ms": latency["p95_ms"],
        "expected_count": event_count,
        "actual_count": len(items),
        "unique_count": len(unique_ids),
        "duplicate_id_count": len(ids) - len(unique_ids),
        "lost_count": event_count - len(unique_ids),
        "host": sanitized_host_manifest(),
    }


def parse_args():
    parser = argparse.ArgumentParser(description="Benchmark durable TakSklad desktop queue storage")
    parser.add_argument("--synthetic-events", type=int, default=10000)
    parser.add_argument("--assert-p95-ms", type=float)
    parser.add_argument("--assert-no-loss", action="store_true")
    return parser.parse_args()


def main():
    args = parse_args()
    if args.synthetic_events <= 0:
        raise SystemExit("--synthetic-events must be positive")
    result = run_benchmark(args.synthetic_events)
    failures = []
    if args.assert_p95_ms is not None and result["latency"]["p95_ms"] > args.assert_p95_ms:
        failures.append(f"p95 {result['latency']['p95_ms']}ms exceeds {args.assert_p95_ms}ms")
    if result["scan_to_durable_feedback_p95_ms"] > 100:
        failures.append("scan-to-durable feedback p95 exceeds 100ms")
    if args.assert_no_loss and (result["lost_count"] or result["duplicate_id_count"]):
        failures.append("queue count mismatch")
    result["status"] = "pass" if not failures else "fail"
    result["failures"] = failures
    sys.stdout.write(json.dumps(result, ensure_ascii=False, sort_keys=True) + os.linesep)
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
