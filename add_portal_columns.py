"""Add the patient portal: Client columns, and the patient_messages table.

    python add_portal_columns.py
    TARGET_DATABASE_URL=... python add_portal_columns.py

Same shape as every other migration script here: the three new columns on
`clients` need ALTER TABLE by hand, because create_all() never touches a table
that already exists; the new table is created by create_all() because it is
new. Safe to run twice.
"""

from __future__ import annotations

import os
import sys

import sqlalchemy as sa

COLUMNS = [
    ("portal_password_hash", "ALTER TABLE clients ADD COLUMN portal_password_hash TEXT"),
    ("portal_issued_at", "ALTER TABLE clients ADD COLUMN portal_issued_at TIMESTAMP"),
    ("portal_last_login", "ALTER TABLE clients ADD COLUMN portal_last_login TIMESTAMP"),
]


def normalise(url: str) -> str:
    if url.startswith("postgres"):
        return (url.replace("postgres://", "postgresql+psycopg://", 1)
                   .replace("postgresql://", "postgresql+psycopg://", 1))
    return url


def migrate(url: str, label: str) -> None:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from app import portal                                          # noqa: F401
    from app.models import Base

    engine = sa.create_engine(normalise(url))

    with engine.begin() as conn:
        existing = {c["name"] for c in sa.inspect(conn).get_columns("clients")}
        for name, ddl in COLUMNS:
            if name in existing:
                print(f"  {label}: clients.{name} already there")
                continue
            conn.execute(sa.text(ddl))
            print(f"  {label}: clients.{name} added")

    before = "patient_messages" in sa.inspect(engine).get_table_names()
    Base.metadata.create_all(engine, tables=[Base.metadata.tables["patient_messages"]])
    after = "patient_messages" in sa.inspect(engine).get_table_names()
    if before:
        print(f"  {label}: patient_messages already there")
    elif after:
        print(f"  {label}: patient_messages created")
    else:
        print(f"  {label}: patient_messages FAILED to create")


if __name__ == "__main__":
    target = (os.environ.get("TARGET_DATABASE_URL") or "").strip()
    if target:
        migrate(target, "hosted database")
    else:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from app.models import engine
        migrate(str(engine.url.render_as_string(hide_password=False)),
                "local database")
