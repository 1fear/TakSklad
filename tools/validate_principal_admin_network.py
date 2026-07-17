#!/usr/bin/env python3
"""Validate the bounded ephemeral principal-admin Docker network without leaking config."""

from __future__ import annotations

import json
import re
import sys


def main(argv=None) -> int:
    values = list(sys.argv[1:] if argv is None else argv)
    if (
        len(values) != 4
        or not re.fullmatch(r"[0-9a-f]{12,64}", values[2])
        or not re.fullmatch(r"[0-9a-f-]{36}", values[3])
    ):
        print("PRINCIPAL_NETWORK_BLOCKED reason=usage", file=sys.stderr)
        return 2
    expected_name, expected_project, postgres_id, operation_id = values
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, list) or len(payload) != 1:
            raise ValueError
        network = payload[0]
        if (
            network.get("Name") != expected_name
            or network.get("Driver") != "bridge"
            or network.get("Scope") != "local"
            or network.get("Internal") is not True
            or network.get("Ingress") is not False
            or network.get("Attachable") is not False
            or (network.get("Options") or {}) != {}
        ):
            raise ValueError
        labels = network.get("Labels") or {}
        if (
            labels.get("com.taksklad.principal.owner") != expected_project
            or labels.get("com.taksklad.principal.operation") != operation_id
            or set(labels) != {
                "com.taksklad.principal.owner", "com.taksklad.principal.operation"
            }
        ):
            raise ValueError
        members = network.get("Containers") or {}
        if not isinstance(members, dict) or set(members) - {postgres_id}:
            raise ValueError
    except (ValueError, json.JSONDecodeError, OSError, AttributeError):
        print("PRINCIPAL_NETWORK_BLOCKED reason=unexpected_topology", file=sys.stderr)
        return 1
    print("attached=1" if postgres_id in members else "attached=0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
