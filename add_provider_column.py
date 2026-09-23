"""Add clients.provider_id - the clinician a patient is assigned to.

    python add_provider_column.py                  # this machine's database
    TARGET_DATABASE_URL=... python add_provider_column.py   # the hosted one

SQLAlchemy's create_all() builds tables that do not exist; it never alters one
that does. A column added to a model after the table was created therefore has
to be added by hand, once per copy of the database.

Safe to run twice: it checks for the column first and says so rather than
failing, so nobody has to remember whether they have already run it.
"""

from __future__ import annotations

import os
import sys

import sqlalchemy as sa


def add_column(url: str, label: str) -> None:
    engine = sa.create_engine(
        url.replace("postgres://", "postgresql+psycopg://", 1)
           .replace("postgresql://", "postgresql+psycopg://", 1)
        if url.startswith("postgres") else url)

    with engine.begin() as conn:
        existing = {c["name"] for c in sa.inspect(conn).get_columns("clients")}
        if "provider_id" in existing:
            print(f"  {label}: provider_id already there, nothing to do")
            return
        conn.execute(sa.text(
            "ALTER TABLE clients ADD COLUMN provider_id INTEGER "
            "REFERENCES users(id)"))
        conn.execute(sa.text(
            "CREATE INDEX IF NOT EXISTS ix_clients_provider_id "
            "ON clients (provider_id)"))
        print(f"  {label}: provider_id added")


if __name__ == "__main__":
    target = (os.environ.get("TARGET_DATABASE_URL") or "").strip()
    if target:
        add_column(target, "hosted database")
    else:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from app.models import engine
        add_column(str(engine.url.render_as_string(hide_password=False)),
                   "local database")
