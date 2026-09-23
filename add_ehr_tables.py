"""Create the clinical record tables: encounters, prescriptions, orders,
charges and documents.

    python add_ehr_tables.py                            # this machine
    TARGET_DATABASE_URL=... python add_ehr_tables.py    # the hosted one

All five are new tables, so create_all builds them - unlike a new column, which
SQLAlchemy never adds to a table that already exists. Safe to run twice: it
reports what was already there instead of failing.
"""

from __future__ import annotations

import os
import sys

import sqlalchemy as sa

TABLES = ("encounters", "encounter_addenda", "prescriptions", "orders",
          "charges", "documents", "insurance_claims", "payments",
          "patient_statements")


def normalise(url: str) -> str:
    if url.startswith("postgres"):
        return (url.replace("postgres://", "postgresql+psycopg://", 1)
                   .replace("postgresql://", "postgresql+psycopg://", 1))
    return url


def migrate(url: str, label: str) -> None:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from app import ehr                                            # noqa: F401
    from app.models import Base

    engine = sa.create_engine(normalise(url))
    before = set(sa.inspect(engine).get_table_names())

    Base.metadata.create_all(engine, tables=[
        Base.metadata.tables[name] for name in TABLES])

    after = set(sa.inspect(engine).get_table_names())
    for name in TABLES:
        if name in before:
            print(f"  {label}: {name} already there")
        elif name in after:
            print(f"  {label}: {name} created")
        else:
            print(f"  {label}: {name} FAILED to create")


if __name__ == "__main__":
    target = (os.environ.get("TARGET_DATABASE_URL") or "").strip()
    if target:
        migrate(target, "hosted database")
    else:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from app.models import engine
        migrate(str(engine.url.render_as_string(hide_password=False)),
                "local database")
