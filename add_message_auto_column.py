"""Add patient_messages.is_auto - marks the automatic acknowledgment apart
from anything a person wrote.

    python add_message_auto_column.py
    TARGET_DATABASE_URL=... python add_message_auto_column.py

Same shape as every other migration script here: a column added to a model
after its table already exists needs ALTER TABLE by hand. Safe to run twice.
"""

from __future__ import annotations

import os
import sys

import sqlalchemy as sa


def normalise(url: str) -> str:
    if url.startswith("postgres"):
        return (url.replace("postgres://", "postgresql+psycopg://", 1)
                   .replace("postgresql://", "postgresql+psycopg://", 1))
    return url


def migrate(url: str, label: str) -> None:
    engine = sa.create_engine(normalise(url))
    with engine.begin() as conn:
        existing = {c["name"] for c in sa.inspect(conn).get_columns("patient_messages")}
        if "is_auto" in existing:
            print(f"  {label}: patient_messages.is_auto already there")
            return
        conn.execute(sa.text(
            "ALTER TABLE patient_messages ADD COLUMN is_auto BOOLEAN "
            "DEFAULT FALSE NOT NULL"))
        print(f"  {label}: patient_messages.is_auto added")


if __name__ == "__main__":
    target = (os.environ.get("TARGET_DATABASE_URL") or "").strip()
    if target:
        migrate(target, "hosted database")
    else:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from app.models import engine
        migrate(str(engine.url.render_as_string(hide_password=False)),
                "local database")
