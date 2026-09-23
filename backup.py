"""Back up the database to a single file, and restore from one.

    python backup.py                     # back up the local database
    python backup.py --cloud             # back up the hosted one instead
    python backup.py --list              # what backups exist
    python backup.py --restore <file>    # put one back

Backups land in backups/ as compressed pg_dump archives. A dump is a complete,
self-contained copy: every table, every row, the schema and the sequences. It
does not need this application to be readable - psql and pg_restore can open it
years from now, and so can any Postgres tool.

pg_dump runs inside the Docker container for the local database, because the
Windows host has no Postgres client installed. For the hosted database it runs
in the container too, connecting outward.

Old backups are kept. Deleting them is a decision for a person, not a default:
the backup you did not know you needed is always the one a cleanup removed.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BACKUPS = ROOT / "backups"
CONTAINER = "ipmg-postgres"
LOCAL_URL = "postgresql://ipmg:ipmg-local-dev@localhost:5432/ipmg_intake"


def _cloud_url() -> str:
    """The hosted connection string, from the file `vercel env pull` wrote."""
    pulled = ROOT / ".vercel" / ".env.production"
    if not pulled.is_file():
        raise SystemExit(
            "No hosted credentials. Run:\n"
            "  npx vercel env pull .vercel/.env.production --environment=production")
    text = pulled.read_text(encoding="utf-8")
    match = re.search(r'^DATABASE_URL="?([^"\n]+)', text, re.M)
    if not match or not match.group(1).startswith("post"):
        raise SystemExit("DATABASE_URL is not in .vercel/.env.production.")
    # pg_dump speaks plain postgresql://, not SQLAlchemy's +psycopg form.
    return match.group(1).replace("postgresql+psycopg://", "postgresql://")


# The local database is Postgres 16; Neon runs 18. pg_dump refuses to dump a
# server newer than itself, so the cloud backup needs a matching client - taken
# from a throwaway container rather than by installing anything on this machine.
CLOUD_IMAGE = "postgres:18-alpine"


def _tool(cloud: bool, args: list[str]) -> list[str]:
    if cloud:
        return ["docker", "run", "--rm", "-i", CLOUD_IMAGE] + args
    return ["docker", "exec", "-i", CONTAINER] + args


def _run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True)


def backup(cloud: bool) -> None:
    BACKUPS.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d-%H%M")
    where = "cloud" if cloud else "local"
    target = BACKUPS / f"ipmg-{where}-{stamp}.dump"
    url = _cloud_url() if cloud else LOCAL_URL

    print(f"backing up the {where} database ...")
    # -Fc is pg_dump's compressed custom format: smaller than SQL text, and
    # pg_restore can pull single tables out of it rather than all or nothing.
    result = _run(_tool(cloud, ["pg_dump", "-Fc", "--no-owner", "--no-acl",
                                "-d", url]))
    if result.returncode != 0:
        err = result.stderr.decode("utf-8", "replace").strip().splitlines()
        raise SystemExit("pg_dump failed:\n  " + "\n  ".join(err[-4:]))

    target.write_bytes(result.stdout)
    size = target.stat().st_size

    # A dump far smaller than expected usually means it connected to an empty
    # database - worth catching now rather than on the day it is needed.
    if size < 20_000:
        print(f"  WARNING: only {size:,} bytes. Check the database was not empty.")
    print(f"  {target.name}  ({size / 1024:.0f} KB)")
    print(f"  {target}")


def listing() -> None:
    if not BACKUPS.is_dir() or not any(BACKUPS.glob("*.dump")):
        print("No backups yet. Run: python backup.py")
        return
    print(f"{'file':38} {'size':>9}  taken")
    print("-" * 70)
    for f in sorted(BACKUPS.glob("*.dump"), reverse=True):
        st = f.stat()
        print(f"{f.name:38} {st.st_size / 1024:>7.0f} KB  "
              f"{datetime.fromtimestamp(st.st_mtime):%d %b %Y %H:%M}")


def restore(name: str, cloud: bool) -> None:
    path = Path(name)
    if not path.is_file():
        path = BACKUPS / name
    if not path.is_file():
        raise SystemExit(f"No such backup: {name}")

    where = "cloud" if cloud else "local"
    url = _cloud_url() if cloud else LOCAL_URL
    print(f"About to restore {path.name} into the {where.upper()} database.")
    print("Every table it contains will be DROPPED and recreated from the file.")
    print("Anything added since this backup was taken will be lost.\n")
    if input(f'Type "{where}" to confirm: ').strip() != where:
        raise SystemExit("Cancelled. Nothing was changed.")

    proc = subprocess.run(
        _tool(cloud, ["pg_restore", "--clean", "--if-exists", "--no-owner",
                      "--no-acl", "-d", url]),
        input=path.read_bytes(), capture_output=True)
    err = proc.stderr.decode("utf-8", "replace").strip()
    # pg_restore warns about dropping objects that were never there; with
    # --if-exists those lines are noise, not failure.
    real = [ln for ln in err.splitlines() if "does not exist" not in ln]
    if proc.returncode != 0 and real:
        raise SystemExit("pg_restore failed:\n  " + "\n  ".join(real[-5:]))
    print(f"\nrestored {path.name} into the {where} database")


if __name__ == "__main__":
    args = sys.argv[1:]
    is_cloud = "--cloud" in args

    if "--list" in args:
        listing()
    elif "--restore" in args:
        i = args.index("--restore")
        if i + 1 >= len(args):
            raise SystemExit("Which backup? python backup.py --restore <file>")
        restore(args[i + 1], is_cloud)
    else:
        backup(is_cloud)
