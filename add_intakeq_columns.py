"""Prepare the database for the IntakeQ import.

    python add_intakeq_columns.py                  # this machine's database
    TARGET_DATABASE_URL=... python add_intakeq_columns.py   # the hosted one

Two new columns on `clients`, and the two new tables the importer checkpoints
into. The tables are created by create_all() because they are new; the columns
are not, because SQLAlchemy never alters a table that already exists. Same
reasoning - and the same shape - as add_provider_column.py.

Safe to run twice. Every step checks first and says so rather than failing, so
nobody has to remember whether they already ran it against which copy.
"""

from __future__ import annotations

import os
import sys

import sqlalchemy as sa

#  (column, DDL, index DDL or None)
COLUMNS = [
    ("intakeq_client_id",
     "ALTER TABLE clients ADD COLUMN intakeq_client_id VARCHAR(64)",
     "CREATE UNIQUE INDEX IF NOT EXISTS ix_clients_intakeq_client_id "
     "ON clients (intakeq_client_id)"),
    ("source",
     "ALTER TABLE clients ADD COLUMN source VARCHAR(16) DEFAULT '' NOT NULL",
     None),
]


def normalise(url: str) -> str:
    if url.startswith("postgres"):
        return (url.replace("postgres://", "postgresql+psycopg://", 1)
                   .replace("postgresql://", "postgresql+psycopg://", 1))
    return url


def migrate(url: str, label: str) -> None:
    engine = sa.create_engine(normalise(url))

    with engine.begin() as conn:
        existing = {c["name"] for c in sa.inspect(conn).get_columns("clients")}
        for name, ddl, index_ddl in COLUMNS:
            if name in existing:
                print(f"  {label}: clients.{name} already there")
                continue
            conn.execute(sa.text(ddl))
            if index_ddl:
                conn.execute(sa.text(index_ddl))
            print(f"  {label}: clients.{name} added")

    #  The two new tables. Importing app.models registers every model including
    #  the import ones, and create_all touches only what is missing - so this is
    #  safe against a database that already has the rest of the schema.
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from app import intakeq                                        # noqa: F401
    from app.models import Base

    before = set(sa.inspect(engine).get_table_names())
    Base.metadata.create_all(engine, tables=[
        intakeq.ImportRun.__table__, intakeq.ImportedIntake.__table__])
    after = set(sa.inspect(engine).get_table_names())
    for table in ("import_runs", "imported_intakes"):
        if table in before:
            print(f"  {label}: {table} already there")
        elif table in after:
            print(f"  {label}: {table} created")
        else:
            print(f"  {label}: {table} FAILED to create")


if __name__ == "__main__":
    target = (os.environ.get("TARGET_DATABASE_URL") or "").strip()
    if target:
        migrate(target, "hosted database")
    else:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from app.models import engine
        migrate(str(engine.url.render_as_string(hide_password=False)),
                "local database")
