"""Copy everything out of the old SQLite file into PostgreSQL.

Run once. Table order matters - parents before children - because Postgres
enforces the foreign keys SQLite was content to ignore.

Sequences are reset afterwards. Postgres keeps its own counter for serial primary
keys, and copying rows with explicit ids does not advance it; without the reset
the next insert collides with id 1.
"""

from __future__ import annotations

import sys
from datetime import date, datetime
from pathlib import Path

from sqlalchemy import create_engine, inspect, text

import app  # loads .env
from app.models import Base, DATABASE_URL

SQLITE_FILE = Path(__file__).resolve().parent / "intake.db"

# Parents first. A child row whose parent has not arrived yet is rejected.
ORDER = [
    "users", "folders", "forms", "questions", "question_items",
    "consent_forms", "form_consents", "clients",
    "medications", "problems", "allergies", "vitals", "clinical_notes",
    "lab_orders",
    "submissions", "answers", "consent_signatures",
    "message_log", "password_resets", "access_requests", "audit_events",
]


def _coerce(value, column_type):
    """SQLite is loose about types; Postgres is not.

    Booleans come back as 0/1 integers and dates as strings, and Postgres rejects
    both. Convert against the destination column's declared type rather than
    guessing from the value, so a genuine integer column keeps its integer.
    """
    if value is None:
        return None

    kind = column_type.__class__.__name__.upper()

    if "BOOLEAN" in kind:
        return bool(value) if not isinstance(value, str) else value.lower() in ("1", "true", "t")

    if "DATETIME" in kind or "TIMESTAMP" in kind:
        if isinstance(value, str):
            try:
                return datetime.fromisoformat(value)
            except ValueError:
                return None
        return value

    if "DATE" in kind:
        if isinstance(value, str):
            try:
                return date.fromisoformat(value[:10])
            except ValueError:
                return None
        return value

    return value


def main() -> None:
    if not SQLITE_FILE.exists():
        sys.exit(f"No {SQLITE_FILE.name} to migrate - nothing to do.")

    src = create_engine(f"sqlite:///{SQLITE_FILE}")
    dst = create_engine(DATABASE_URL)

    print(f"from : {SQLITE_FILE.name}")
    print(f"to   : {DATABASE_URL.split('@')[-1]}\n")

    Base.metadata.create_all(dst)

    src_tables = set(inspect(src).get_table_names())
    dst_tables = set(inspect(dst).get_table_names())
    moved = skipped = 0

    with src.connect() as s, dst.begin() as d:
        # Start clean, children first so nothing is left pointing at a deleted parent.
        for table in reversed(ORDER):
            if table in dst_tables:
                d.execute(text(f'DELETE FROM "{table}"'))

        for table in ORDER:
            if table not in src_tables:
                continue
            if table not in dst_tables:
                print(f"  {table:22} skipped - not in the new schema")
                skipped += 1
                continue

            rows = [dict(r) for r in s.execute(text(f'SELECT * FROM "{table}"')).mappings()]
            if not rows:
                print(f"  {table:22} empty")
                continue

            # Only columns the destination actually has; the schema has moved on.
            dst_info = {c["name"]: c["type"] for c in inspect(dst).get_columns(table)}
            cols = [c for c in rows[0] if c in dst_info]
            dropped = [c for c in rows[0] if c not in dst_info]

            payload = [{c: _coerce(r[c], dst_info[c]) for c in cols} for r in rows]
            names = ", ".join(f'"{c}"' for c in cols)
            binds = ", ".join(f":{c}" for c in cols)
            d.execute(text(f'INSERT INTO "{table}" ({names}) VALUES ({binds})'), payload)

            note = f"  (dropped {', '.join(dropped)})" if dropped else ""
            print(f"  {table:22} {len(rows):4} rows{note}")
            moved += len(rows)

        # Advance each id sequence past the rows just inserted.
        for table in ORDER:
            if table not in dst_tables:
                continue
            d.execute(text(
                f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), "
                f"COALESCE((SELECT MAX(id) FROM \"{table}\"), 1), true)"
            ))

    print(f"\n{moved} rows moved, {skipped} table(s) skipped.")
    print("Sequences reset. Verify, then delete intake.db.")


if __name__ == "__main__":
    main()
