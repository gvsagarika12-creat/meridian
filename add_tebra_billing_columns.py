"""Prepare the database for pushing charges and payments to Tebra.

    python add_tebra_billing_columns.py                  # this machine's database
    TARGET_DATABASE_URL=... python add_tebra_billing_columns.py   # the hosted one

Two new columns - `charges.tebra_encounter_id` and `payments.tebra_payment_id` -
same shape as `clients.tebra_patient_id` in add_intakeq_columns.py: unique, so
the database itself refuses a second push even if a bug in the code that
checks first is ever introduced.

Safe to run twice. Every step checks first and says so rather than failing.
"""

from __future__ import annotations

import os
import sys

import sqlalchemy as sa

#  (table, column, DDL, index DDL)
COLUMNS = [
    ("charges", "tebra_encounter_id",
     "ALTER TABLE charges ADD COLUMN tebra_encounter_id VARCHAR(64)",
     "CREATE UNIQUE INDEX IF NOT EXISTS ix_charges_tebra_encounter_id "
     "ON charges (tebra_encounter_id)"),
    ("payments", "tebra_payment_id",
     "ALTER TABLE payments ADD COLUMN tebra_payment_id VARCHAR(64)",
     "CREATE UNIQUE INDEX IF NOT EXISTS ix_payments_tebra_payment_id "
     "ON payments (tebra_payment_id)"),
]


def normalise(url: str) -> str:
    if url.startswith("postgres"):
        return (url.replace("postgres://", "postgresql+psycopg://", 1)
                   .replace("postgresql://", "postgresql+psycopg://", 1))
    return url


def migrate(url: str, label: str) -> None:
    engine = sa.create_engine(normalise(url))

    with engine.begin() as conn:
        inspector = sa.inspect(conn)
        for table, name, ddl, index_ddl in COLUMNS:
            existing = {c["name"] for c in inspector.get_columns(table)}
            if name in existing:
                print(f"  {label}: {table}.{name} already there")
                continue
            conn.execute(sa.text(ddl))
            conn.execute(sa.text(index_ddl))
            print(f"  {label}: {table}.{name} added")


if __name__ == "__main__":
    target = (os.environ.get("TARGET_DATABASE_URL") or "").strip()
    if target:
        migrate(target, "hosted database")
    else:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from app.models import engine
        migrate(str(engine.url.render_as_string(hide_password=False)),
                "local database")
