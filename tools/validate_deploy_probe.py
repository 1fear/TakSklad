#!/usr/bin/env python3
import json
import sys


def validate_payload(label, payload):
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
    if len(argv) != 1:
        sys.stderr.write("usage: validate_deploy_probe.py health|readiness\n")
        return 2
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            raise ValueError("response JSON must be an object")
        validate_payload(argv[0], payload)
    except (ValueError, json.JSONDecodeError) as exc:
        sys.stderr.write(f"{exc}\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
