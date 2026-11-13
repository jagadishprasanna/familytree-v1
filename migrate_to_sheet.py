"""Copy a family.txt into your Google Sheet (one-time import).

    python migrate_to_sheet.py family.txt <sheet-id-or-url> [--force]

Needs GOOGLE_SERVICE_ACCOUNT_JSON (or GOOGLE_SERVICE_ACCOUNT_FILE) set, and the
sheet shared with the service account as Editor. Refuses to overwrite a sheet
that already has people unless --force is given.
"""
from __future__ import annotations

import argparse
import sys

from backends import FamilyError, SheetBackend, TextBackend, records_to_rows
from family_store import FamilyStore


def migrate(text_path: str, sheet: SheetBackend, force: bool = False) -> dict[str, int]:
    src = TextBackend(text_path)
    FamilyStore(src)  # validates the file (unknown ids, duplicates, ...)
    existing = [r for r in sheet.read() if r[0] == "PERSON"]
    if existing and not force:
        raise FamilyError(f"The sheet already has {len(existing)} people. Use --force to overwrite.")
    rows = records_to_rows(src.read())
    sheet.replace_all(rows)
    return {k: len(v) for k, v in rows.items()}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("textfile")
    ap.add_argument("sheet", help="spreadsheet id or URL")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    try:
        counts = migrate(a.textfile, SheetBackend.from_env(a.sheet, ttl=0), a.force)
    except FamilyError as e:
        sys.exit(f"Error: {e}")
    print(f"Imported {counts['PERSON']} people, {counts['MARRIED']} marriages, "
          f"{counts['PARENT']} parent links.")


if __name__ == "__main__":
    main()