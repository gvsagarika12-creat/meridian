"""Load the hospital's archive CSV into the database.

    python import_hospital_data.py                 # everything in data/
    python import_hospital_data.py path/to/file.csv   # one named file

The archive lives in data/ next to this script, so the practice's own copy
travels with the application instead of sitting in somebody's Downloads
folder where the next tidy-up deletes it.

Re-running replaces everything from the same source file rather than appending,
so an import that half-failed can simply be run again. That is safe here because
the archive is imported data, never edited in this app - the file on disk is the
authority, and the table is a copy of it.
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

from app.models import SessionLocal, init_db
from app.hospital import HospitalRecord

# The CSV's own headings, mapped to columns. Anything absent is left at its
# default rather than guessed at.
REQUIRED = ["Patient_ID", "Condition"]


def _date(value):
    """A date of birth in any of the shapes an export might use, or nothing."""
    from datetime import datetime
    raw = (value or "").strip()
    for pattern in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y", "%d %b %Y"):
        try:
            return datetime.strptime(raw, pattern).date()
        except ValueError:
            continue
    return None


def _int(value: str) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def load(path: Path) -> int:
    with path.open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))

    if not rows:
        raise SystemExit(f"{path.name} has no rows.")

    missing = [c for c in REQUIRED if c not in rows[0]]
    if missing:
        raise SystemExit(f"{path.name} is missing required column(s): {missing}\n"
                         f"Found: {list(rows[0])}")

    init_db()
    db = SessionLocal()
    source = path.name

    gone = db.query(HospitalRecord).filter_by(source=source).delete()
    if gone:
        print(f"replaced {gone} rows previously imported from {source}")

    kept = 0
    for row in rows:
        pid = _int(row.get("Patient_ID"))
        if pid is None:
            continue                       # a row with no patient number is unusable
        db.add(HospitalRecord(
            patient_id=pid,
            # A real export carries these; the anonymised analytics extract does
            # not. Read them when present rather than making the file conform.
            name=(row.get("Name") or row.get("Patient_Name") or "").strip(),
            dob=_date(row.get("DOB") or row.get("Date_of_Birth")),
            age=_int(row.get("Age")),
            gender=(row.get("Gender") or "").strip(),
            condition=(row.get("Condition") or "").strip(),
            procedure=(row.get("Procedure") or "").strip(),
            cost=_int(row.get("Cost")),
            length_of_stay=_int(row.get("Length_of_Stay")),
            readmission=(row.get("Readmission") or "").strip().lower() == "yes",
            outcome=(row.get("Outcome") or "").strip(),
            satisfaction=_int(row.get("Satisfaction")),
            source=source,
        ))
        kept += 1

    db.commit()
    return kept


DATA_DIR = Path(__file__).resolve().parent / "data"


def _targets(argv: list[str]) -> list[Path]:
    """The files to import: the ones named, or every CSV in data/."""
    if argv:
        chosen = [Path(a).expanduser() for a in argv]
        missing = [p for p in chosen if not p.is_file()]
        if missing:
            raise SystemExit("No such file: " + ", ".join(str(m) for m in missing))
        return chosen

    if not DATA_DIR.is_dir():
        raise SystemExit(f"No data folder at {DATA_DIR}. Put the archive CSV there, "
                         f"or name a file on the command line.")
    found = sorted(DATA_DIR.glob("*.csv"))
    if not found:
        raise SystemExit(f"No CSV files in {DATA_DIR}.")
    return found


if __name__ == "__main__":
    for csv_path in _targets(sys.argv[1:]):
        n = load(csv_path)
        print(f"imported {n} visit rows from {csv_path.name}")

    db = SessionLocal()
    total = db.query(HospitalRecord).count()
    ids = db.query(HospitalRecord.patient_id).distinct().count()
    print(f"archive now holds {total} rows covering {ids} patient numbers")
