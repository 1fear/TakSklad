"""Recover queue leases after deploy has intentionally stopped every worker."""

from .db import SessionLocal
from .event_leases import recover_inflight_event_leases_after_worker_stop


def main() -> int:
    with SessionLocal() as db:
        recovered = recover_inflight_event_leases_after_worker_stop(db)
    print(f"EVENT_LEASE_RECOVERY_OK recovered={recovered}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
