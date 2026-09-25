"""Add appointments.channel - how the visit happens (in person or online),
separate from `kind`, which is what the visit is for.

    python add_appointment_channel_column.py                  # this machine's database
    TARGET_DATABASE_URL=... python add_appointment_channel_column.py   # the hosted one

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
        existing = {c["name"] for c in sa.inspect(conn).get_columns("appointments")}
        if "channel" in existing:
            print(f"  {label}: channel already there, nothing to do")
            return
        conn.execute(sa.text(
            "CREATE TYPE appointment_channel AS ENUM ('in_person', 'online')"))
        conn.execute(sa.text(
            "ALTER TABLE appointments ADD COLUMN channel appointment_channel "
            "NOT NULL DEFAULT 'in_person'"))
        print(f"  {label}: channel added")


if __name__ == "__main__":
    target = (os.environ.get("TARGET_DATABASE_URL") or "").strip()
    if target:
        add_column(target, "hosted database")
    else:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from app.models import engine
        add_column(str(engine.url.render_as_string(hide_password=False)),
                   "local database")
