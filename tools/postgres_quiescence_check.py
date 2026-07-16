#!/usr/bin/env python3
"""Counts-only proof that no other PostgreSQL client transaction is active."""

import json


def run():
    from sqlalchemy import text

    try:
        from app.db import SessionLocal
    except ModuleNotFoundError:
        from backend.app.db import SessionLocal

    with SessionLocal() as db:
        active = int(db.execute(text("""
            SELECT count(*)
            FROM pg_stat_activity
            WHERE datname = current_database()
              AND pid <> pg_backend_pid()
              AND backend_type = 'client backend'
              AND state IS DISTINCT FROM 'idle'
        """)).scalar_one())
        db.rollback()
    print(json.dumps({
        "schema_version": 1,
        "mode": "counts_only",
        "other_active_client_transactions": active,
        "quiescent": active == 0,
    }, sort_keys=True))
    return 0 if active == 0 else 3


if __name__ == "__main__":
    try:
        raise SystemExit(run())
    except SystemExit:
        raise
    except Exception as exc:
        print(json.dumps({
            "schema_version": 1,
            "mode": "failed_counts_only",
            "other_active_client_transactions": -1,
            "quiescent": False,
            "error_type": type(exc).__name__,
        }, sort_keys=True))
        raise SystemExit(4)
