#!/usr/bin/env python3
import argparse

from backend.app.db import SessionLocal
from backend.app.representative_contacts import import_representative_contacts_from_xlsx


def main() -> int:
    parser = argparse.ArgumentParser(description="Import sales representative contacts from XLSX into TakSklad DB.")
    parser.add_argument("xlsx_path", help="Path to XLSX with ТП, Раб номер, Лич номер and Раб зона columns.")
    parser.add_argument("--dry-run", action="store_true", help="Parse and validate without committing DB changes.")
    args = parser.parse_args()

    with SessionLocal() as db:
        summary = import_representative_contacts_from_xlsx(db, args.xlsx_path)
        if args.dry_run:
            db.rollback()
        else:
            db.commit()

    mode = "dry-run" if args.dry_run else "committed"
    print(
        f"representative_contacts_import {mode}: "
        f"rows={summary['rows']} created={summary['created']} "
        f"updated={summary['updated']} skipped={summary['skipped']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
