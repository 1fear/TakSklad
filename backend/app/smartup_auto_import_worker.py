import argparse
import json
import logging
from datetime import datetime

from .db import SessionLocal
from .smartup_auto_import import (
    SmartupClient,
    build_smartup_auto_import_status,
    load_smartup_auto_import_config,
    parse_slot_time,
    parse_smartup_date,
    run_due_smartup_auto_imports,
    run_scheduled_smartup_auto_import_slot,
    sweep_incomplete_smartup_fulfillments,
    worker_sleep,
)
from .worker_observability import observed_worker_cycle


logging.basicConfig(level=logging.INFO)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config = load_smartup_auto_import_config()
    if args.command == "run-once":
        return run_once(args, config)
    if args.command == "status":
        return run_status(args, config)

    disabled_logged = False
    while True:
        try:
            with observed_worker_cycle("smartup_auto_import", config.poll_seconds):
                if not config.enabled:
                    if not disabled_logged:
                        logging.info("Smartup auto import worker is disabled")
                        disabled_logged = True
                else:
                    try:
                        with SessionLocal() as db:
                            sweep_result = sweep_incomplete_smartup_fulfillments(
                                db,
                                config,
                                client=SmartupClient(config),
                            )
                    except Exception:
                        sweep_result = {"status": "failed"}
                        logging.exception("Smartup fulfillment sweep failed; scheduled slots remain enabled")
                    with SessionLocal() as db:
                        results = run_due_smartup_auto_imports(db, config)
                    if sweep_result.get("status") not in {"idle", "skipped"}:
                        logging.info("Smartup fulfillment sweep: %s", sweep_result)
                    for result in results:
                        logging.info("Smartup auto import worker: %s", result)
        except Exception:
            logging.exception("Smartup auto import worker failed")
        worker_sleep(config)
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Smartup terminal orders auto import worker")
    subparsers = parser.add_subparsers(dest="command")
    run_once_parser = subparsers.add_parser("run-once", help="Run one Smartup automation slot manually")
    run_once_parser.add_argument("--slot", required=True, help="Slot label, for example 12:00, 15:00, 17:50")
    run_once_parser.add_argument("--date", default="", help="Export date, YYYY-MM-DD or DD.MM.YYYY. Defaults to today.")
    run_once_parser.add_argument(
        "--delivery-date",
        default="",
        help="Optional Smartup delivery_date filter, YYYY-MM-DD or DD.MM.YYYY.",
    )
    status_parser = subparsers.add_parser("status", help="Print read-only Smartup automation status")
    status_parser.add_argument("--limit", type=int, default=5, help="Number of recent Smartup events to include")
    status_parser.add_argument("--json", action="store_true", help="Compatibility flag; output is always JSON")
    return parser.parse_args(argv)


def run_once(args: argparse.Namespace, config) -> int:
    run_date = parse_smartup_date(args.date) if args.date else datetime.now(config.timezone).date()
    if run_date is None:
        raise SystemExit(f"Некорректная дата Smartup run-once: {args.date}")
    target_delivery_date = parse_smartup_date(args.delivery_date) if args.delivery_date else None
    if args.delivery_date and target_delivery_date is None:
        raise SystemExit(f"Некорректная дата отгрузки Smartup run-once: {args.delivery_date}")
    slot_time = parse_slot_time(args.slot)
    run_at = datetime.combine(run_date, slot_time, tzinfo=config.timezone)
    try:
        with SessionLocal() as db:
            result = run_scheduled_smartup_auto_import_slot(
                db,
                config,
                slot_label=args.slot,
                now=run_at,
                target_delivery_date=target_delivery_date,
            )
    except Exception as exc:
        logging.exception("Smartup auto import run-once failed")
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps(result, ensure_ascii=False, default=str, indent=2))
    return 0


def run_status(args: argparse.Namespace, config) -> int:
    with SessionLocal() as db:
        result = build_smartup_auto_import_status(db, config, limit=args.limit)
    print(json.dumps(result, ensure_ascii=False, default=str, indent=2))
    return 0 if result.get("status") == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
