"""Give every archive row a name and a date of birth.

    python name_the_archive.py            # show what would change
    python name_the_archive.py --apply

The CSV the practice supplied is an anonymised analytics extract: patient
number, age, sex, condition, and what it cost. A real export from a records
system carries a name and a date of birth as well, and without them the Archive
screen cannot be searched the way a person actually searches - by the name in
front of them.

Two rules make the invented data safe to work with.

**It is deterministic.** The name for patient 417 is derived from 417, so
re-running this produces the same person every time. A demo where the same
number is a different person each morning teaches nobody anything.

**It agrees with what is already there.** The date of birth is calculated back
from the `Age` the file already records, and the first name is drawn from a list
matching the recorded `Gender`. Otherwise the app's own link check - which
compares the archive's age and sex against the patient's - would start
contradicting the archive it is checking.

Rows already linked to a registered patient take that patient's real name and
date of birth instead, so the archive and the practice agree about who number 2
is.

Nothing here is a real person. Replace all of it the day a genuine export
arrives - the importer reads Name and DOB columns when the file has them.
"""

from __future__ import annotations

import sys
from datetime import date, timedelta

from app.models import Client, SessionLocal, init_db
from app.hospital import HospitalRecord

FEMALE = ["Maria", "Linda", "Patricia", "Barbara", "Susan", "Jessica", "Sarah",
          "Karen", "Nancy", "Betty", "Dorothy", "Sandra", "Ashley", "Kimberly",
          "Donna", "Carol", "Michelle", "Emily", "Amanda", "Melissa", "Deborah",
          "Stephanie", "Rebecca", "Laura", "Sharon", "Cynthia", "Kathleen"]

MALE = ["James", "Robert", "John", "Michael", "David", "William", "Richard",
        "Joseph", "Thomas", "Charles", "Christopher", "Daniel", "Matthew",
        "Anthony", "Mark", "Donald", "Steven", "Paul", "Andrew", "Joshua",
        "Kenneth", "Kevin", "Brian", "George", "Timothy", "Ronald", "Jason"]

SURNAMES = ["Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller",
            "Davis", "Rodriguez", "Martinez", "Hernandez", "Lopez", "Gonzalez",
            "Wilson", "Anderson", "Thomas", "Taylor", "Moore", "Jackson",
            "Martin", "Lee", "Perez", "Thompson", "White", "Harris", "Sanchez",
            "Clark", "Ramirez", "Lewis", "Robinson", "Walker", "Young", "Allen",
            "King", "Wright", "Scott", "Torres", "Nguyen", "Hill", "Flores",
            "Green", "Adams", "Nelson", "Baker", "Hall", "Rivera", "Campbell",
            "Mitchell", "Carter", "Roberts"]


def invent(patient_id: int, gender: str, age: int | None) -> tuple[str, date | None]:
    """A stable name and date of birth for one patient number."""
    firsts = FEMALE if (gender or "").strip().lower().startswith("f") else MALE
    first = firsts[patient_id % len(firsts)]
    # A second factor so 1, 28 and 55 do not all share a surname.
    last = SURNAMES[(patient_id * 7 + patient_id // len(firsts)) % len(SURNAMES)]

    born = None
    if age:
        # Back-calculate from the recorded age, spread across the year so the
        # dates do not all land on 1 January.
        year = date.today().year - age
        born = date(year, 1, 1) + timedelta(days=(patient_id * 137) % 365)
    return f"{first} {last}", born


def main(apply: bool) -> None:
    init_db()
    db = SessionLocal()

    # A registered patient's real details win over an invented pair.
    known = {c.hospital_id: c for c in
             db.query(Client).filter(Client.hospital_id.isnot(None)).all()}

    rows = db.query(HospitalRecord).all()
    changed, from_patients = 0, 0
    preview = []

    for r in rows:
        patient = known.get(r.patient_id)
        if patient:
            name = patient.name
            born = patient.dob
            from_patients += 1
        else:
            name, born = invent(r.patient_id, r.gender, r.age)

        if r.name == name and r.dob == born:
            continue
        changed += 1
        if len(preview) < 10:
            preview.append((r.patient_id, name, born, r.age, r.gender,
                            "registered patient" if patient else ""))
        if apply:
            r.name, r.dob = name, born

    print(f"{'no.':>5}  {'name':22} {'date of birth':14} {'age':>4} {'sex':7} where from")
    print("-" * 78)
    for pid, name, born, age, gender, note in preview:
        print(f"{pid:>5}  {name:22} {str(born or '-'):14} {str(age or '-'):>4} "
              f"{gender:7} {note or 'invented'}")

    print(f"\n{changed} of {len(rows)} rows would change "
          f"({from_patients} take a registered patient's real details)")

    if not apply:
        print("\nPreview only. Re-run with --apply.")
        return
    db.commit()
    print("applied")


if __name__ == "__main__":
    main("--apply" in sys.argv)
