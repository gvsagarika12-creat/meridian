"""Add message_log.broadcast_id - which broadcast a send attempt belongs to.

    python add_message_log_broadcast_id.py
    TARGET_DATABASE_URL=... python add_message_log_broadcast_id.py

Same reasoning as add_provider_column.py: create_all() builds tables that do
not exist yet but never alters one that does, so a column added to a model
after the table was created has to be added by hand, once per copy of the
database. Safe to run twice.
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
        existing = {c["name"] for c in sa.inspect(conn).get_columns("message_log")}
        if "broadcast_id" in existing:
            print(f"  {label}: broadcast_id already there, nothing to do")
            return
        conn.execute(sa.text(
            "ALTER TABLE message_log ADD COLUMN broadcast_id INTEGER "
            "REFERENCES broadcasts(id)"))
        conn.execute(sa.text(
            "CREATE INDEX IF NOT EXISTS ix_message_log_broadcast_id "
            "ON message_log (broadcast_id)"))
        print(f"  {label}: broadcast_id added")


if __name__ == "__main__":
    target = (os.environ.get("TARGET_DATABASE_URL") or "").strip()
    if target:
        add_column(target, "hosted database")
    else:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from app.models import engine
        add_column(str(engine.url.render_as_string(hide_password=False)),
                   "local database")
