"""Add the new charge and claim statuses to the Postgres enum types.

    python add_billing_statuses.py
    TARGET_DATABASE_URL=... python add_billing_statuses.py

SQLAlchemy maps these to real Postgres ENUM types, and a value added to the
Python class does not exist in the database until ALTER TYPE says so. Until
then every insert carrying the new value fails at the point of writing, which
is long after the code looks correct.

ALTER TYPE ... ADD VALUE cannot run inside a transaction block on older
Postgres, so each runs on its own connection with autocommit. Safe to run
twice: IF NOT EXISTS means an existing value is skipped rather than an error.
"""

from __future__ import annotations

import os
import sys

import sqlalchemy as sa

ADDITIONS = [
    ("chargestatus", ["pending_approval", "approved"]),
    ("claimstatus", ["waiting_adjudication", "needs_investigation"]),
]


def normalise(url: str) -> str:
    if url.startswith("postgres"):
        return (url.replace("postgres://", "postgresql+psycopg://", 1)
                   .replace("postgresql://", "postgresql+psycopg://", 1))
    return url


def migrate(url: str, label: str) -> None:
    engine = sa.create_engine(normalise(url))
    with engine.connect() as conn:
        types = {r[0] for r in conn.execute(sa.text(
            "SELECT typname FROM pg_type WHERE typtype = 'e'"))}

    for type_name, values in ADDITIONS:
        if type_name not in types:
            print(f"  {label}: type {type_name} does not exist - skipped")
            continue
        for value in values:
            with engine.connect().execution_options(
                    isolation_level="AUTOCOMMIT") as conn:
                existing = {r[0] for r in conn.execute(sa.text(
                    "SELECT e.enumlabel FROM pg_enum e JOIN pg_type t "
                    "ON t.oid = e.enumtypid WHERE t.typname = :t"),
                    {"t": type_name})}
                if value in existing:
                    print(f"  {label}: {type_name}.{value} already there")
                    continue
                conn.execute(sa.text(
                    f"ALTER TYPE {type_name} ADD VALUE IF NOT EXISTS '{value}'"))
                print(f"  {label}: {type_name}.{value} added")


if __name__ == "__main__":
    target = (os.environ.get("TARGET_DATABASE_URL") or "").strip()
    if target:
        migrate(target, "hosted database")
    else:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from app.models import engine
        migrate(str(engine.url.render_as_string(hide_password=False)),
                "local database")
