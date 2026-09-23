"""Create the broadcasts table.

    python add_broadcast_table.py
    TARGET_DATABASE_URL=... python add_broadcast_table.py

Same shape as add_ehr_tables.py, in its own file because app/broadcasts.py is
its own module - a migration script per module keeps each one small enough to
read in one sitting, rather than one script accumulating every table the
project has ever added.
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
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from app import broadcasts                                     # noqa: F401
    from app.models import Base

    engine = sa.create_engine(normalise(url))
    before = "broadcasts" in sa.inspect(engine).get_table_names()
    Base.metadata.create_all(engine, tables=[Base.metadata.tables["broadcasts"]])
    after = "broadcasts" in sa.inspect(engine).get_table_names()
    if before:
        print(f"  {label}: broadcasts already there")
    elif after:
        print(f"  {label}: broadcasts created")
    else:
        print(f"  {label}: broadcasts FAILED to create")


if __name__ == "__main__":
    target = (os.environ.get("TARGET_DATABASE_URL") or "").strip()
    if target:
        migrate(target, "hosted database")
    else:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from app.models import engine
        migrate(str(engine.url.render_as_string(hide_password=False)),
                "local database")
