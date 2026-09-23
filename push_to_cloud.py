"""Copy this machine's data into the hosted database.

    set TARGET_DATABASE_URL=postgresql://...      (Windows: set, PowerShell: $env:)
    python push_to_cloud.py                       # show what would move
    python push_to_cloud.py --apply               # move it
    python push_to_cloud.py --apply --replace     # clear the target first

--replace wipes the tables listed in ORDER on the target before copying. It is
for a target holding nothing but the sample data the app seeds itself with on
first run. User accounts are never in ORDER, so they survive it.

The connection string is read from the environment, never from the command line,
so it does not end up in your shell history.

What moves: patients, forms, questions, submissions, answers, the clinical chart
and the whole hospital archive.

What does not: user accounts. Passwords are per-deployment on purpose - the
account you created on the public URL stays the only way in, and a password
chosen for a laptop should not become the password for a public website.

Tables are copied parent-first so foreign keys are always satisfied, and each
table's id sequence is reset afterwards, or the first row the hosted app writes
would collide with a copied one.
"""

from __future__ import annotations

import os
import sys

from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

from app import models  # noqa: F401  - registers every mapper
from app.models import Base

# Parent before child. Users are absent deliberately; see the module docstring.
ORDER = [
    "folders", "consent_forms", "forms", "form_consents", "questions",
    "question_items", "clients", "submissions", "answers",
    "consent_signatures", "medications", "problems", "allergies", "vitals",
    "clinical_notes", "lab_orders", "hospital_records", "message_log",
    "audit_events",
]


def _target_url() -> str:
    raw = (os.environ.get("TARGET_DATABASE_URL") or "").strip()
    if not raw:
        raise SystemExit(
            "Set TARGET_DATABASE_URL first - the Neon connection string from\n"
            "Vercel > Storage > neon-claret-xylophone > Show secret.\n\n"
            '  PowerShell:  $env:TARGET_DATABASE_URL = "postgresql://..."\n'
            '  Git Bash:    export TARGET_DATABASE_URL="postgresql://..."')
    if raw.startswith("postgres://"):
        raw = "postgresql+psycopg://" + raw[len("postgres://"):]
    elif raw.startswith("postgresql://"):
        raw = "postgresql+psycopg://" + raw[len("postgresql://"):]
    return raw


def main(apply: bool, replace: bool) -> None:
    src = create_engine(models.LOCAL_DEFAULT)
    dst = create_engine(_target_url())

    Base.metadata.create_all(dst)          # harmless if they already exist

    Src, Dst = sessionmaker(bind=src), sessionmaker(bind=dst)
    s, d = Src(), Dst()

    print(f"{'table':22} {'here':>7} {'there':>7}")
    print("-" * 40)
    plan = []
    for name in ORDER:
        table = Base.metadata.tables.get(name)
        if table is None:
            continue
        here = s.execute(select(table)).mappings().all()
        there = d.execute(text(f"select count(*) from {name}")).scalar_one()
        print(f"{name:22} {len(here):>7} {there:>7}")
        plan.append((name, table, here, there))

    if not apply:
        print("\nPreview only. Re-run with --apply to copy.")
        return

    occupied = [n for n, _t, here, there in plan if there and here]
    if occupied and not replace:
        print(f"\nRefusing to copy: {', '.join(occupied)} already hold rows there.")
        print("Copying on top would duplicate them. Re-run with --replace to clear")
        print("those tables first, or copy into an empty database.")
        return

    if replace:
        # Child before parent, the reverse of the copy order, so a foreign key
        # never points at a row that has just been deleted. `users` is not in
        # ORDER at all, so the account on the hosted site survives this.
        for name, _t, _here, there in reversed(plan):
            if there:
                d.execute(text(f"delete from {name}"))
        d.flush()
        print(f"\ncleared {sum(1 for _n, _t, _h, t in plan if t)} tables "
              f"(user accounts untouched)")

    moved = 0
    for name, table, rows, _there in plan:
        if not rows:
            continue
        payload = [dict(r) for r in rows]

        # Anything pointing at a user has to let go of it. User accounts are not
        # copied, so a local user_id names an account that does not exist on the
        # target - the foreign key refuses the insert. The audit entry itself is
        # worth keeping; the actor is a person who has no account over there, so
        # the honest record is that the action happened and by whom is unknown.
        if "user_id" in table.c:
            orphaned = sum(1 for row in payload if row.get("user_id") is not None)
            for row in payload:
                row["user_id"] = None
            if orphaned:
                print(f"  {name}: cleared {orphaned} actor reference(s) - "
                      f"those accounts are local only")

        d.execute(table.insert(), payload)
        moved += len(rows)
        print(f"  copied {len(rows):>5} into {name}")

    # Every id came across as-is, so the sequences still point at 1 and the next
    # insert would collide. Move each one past the highest id that now exists.
    for name, table, rows, _there in plan:
        if not rows or "id" not in table.c:
            continue
        d.execute(text(
            f"select setval(pg_get_serial_sequence('{name}', 'id'), "
            f"coalesce((select max(id) from {name}), 1))"))

    d.commit()
    print(f"\ncopied {moved} rows. Sequences reset.")


if __name__ == "__main__":
    main("--apply" in sys.argv, "--replace" in sys.argv)
