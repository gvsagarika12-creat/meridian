"""Add MFA columns to users, and create the mfa_backup_codes table.

    python add_mfa_columns.py
    TARGET_DATABASE_URL=... python add_mfa_columns.py

Same reasoning as every other migration script here: create_all() builds
tables that do not exist yet but never alters one that does, so the three new
columns on `users` have to be added by hand. The backup-codes table is new, so
create_all() handles that part on its own. Safe to run twice.
"""

from __future__ import annotations

import os
import sys

import sqlalchemy as sa

COLUMNS = [
    ("mfa_secret_encrypted", "ALTER TABLE users ADD COLUMN mfa_secret_encrypted TEXT"),
    ("mfa_enabled",
     "ALTER TABLE users ADD COLUMN mfa_enabled BOOLEAN DEFAULT FALSE NOT NULL"),
    ("mfa_enrolled_at", "ALTER TABLE users ADD COLUMN mfa_enrolled_at TIMESTAMP"),
]


def normalise(url: str) -> str:
    if url.startswith("postgres"):
        return (url.replace("postgres://", "postgresql+psycopg://", 1)
                   .replace("postgresql://", "postgresql+psycopg://", 1))
    return url


def migrate(url: str, label: str) -> None:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from app import mfa                                             # noqa: F401
    from app.models import Base

    engine = sa.create_engine(normalise(url))

    with engine.begin() as conn:
        existing = {c["name"] for c in sa.inspect(conn).get_columns("users")}
        for name, ddl in COLUMNS:
            if name in existing:
                print(f"  {label}: users.{name} already there")
                continue
            conn.execute(sa.text(ddl))
            print(f"  {label}: users.{name} added")

    before = "mfa_backup_codes" in sa.inspect(engine).get_table_names()
    Base.metadata.create_all(engine, tables=[Base.metadata.tables["mfa_backup_codes"]])
    after = "mfa_backup_codes" in sa.inspect(engine).get_table_names()
    if before:
        print(f"  {label}: mfa_backup_codes already there")
    elif after:
        print(f"  {label}: mfa_backup_codes created")
    else:
        print(f"  {label}: mfa_backup_codes FAILED to create")


if __name__ == "__main__":
    target = (os.environ.get("TARGET_DATABASE_URL") or "").strip()
    if target:
        migrate(target, "hosted database")
    else:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from app.models import engine
        migrate(str(engine.url.render_as_string(hide_password=False)),
                "local database")
