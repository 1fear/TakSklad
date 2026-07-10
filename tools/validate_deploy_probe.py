#!/usr/bin/env python3
import json
import argparse
import sys


def validate_payload(label, payload, *, expected_sha="", expected_digest=""):
    if expected_sha and payload.get("commit_sha") != expected_sha:
        raise ValueError("runtime commit SHA differs from verified manifest")
    if expected_digest and payload.get("image_digest") != expected_digest:
        raise ValueError("runtime image digest differs from verified manifest")
    if label == "health":
        if payload.get("status") != "ok":
            raise ValueError("health status is not ok")
        return
    if label != "readiness":
        raise ValueError("unknown endpoint label")

    policy = payload.get("policy") or {}
    database = payload.get("database") or {}
    migrations = payload.get("migrations") or {}
    if payload.get("ready") is not True or payload.get("status") not in {"ok", "degraded"}:
        raise ValueError("readiness ready/status contract failed")
    if database.get("status") != "ok":
        raise ValueError("readiness database contract failed")
    if migrations.get("status") != "ok":
        raise ValueError("readiness migration status failed")
    if not migrations.get("expected_head") or migrations.get("current_revision") != migrations.get("expected_head"):
        raise ValueError("readiness migration revision failed")
    if policy.get("mandatory_status") != "ok":
        raise ValueError("readiness mandatory policy failed")


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser()
    parser.add_argument("label", choices=("health", "readiness"))
    parser.add_argument("--expected-sha", default="")
    parser.add_argument("--expected-digest", default="")
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        return 2
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            raise ValueError("response JSON must be an object")
        validate_payload(
            args.label,
            payload,
            expected_sha=args.expected_sha,
            expected_digest=args.expected_digest,
        )
    except (ValueError, json.JSONDecodeError) as exc:
        sys.stderr.write(f"{exc}\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
