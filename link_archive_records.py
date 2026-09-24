"""Link every registered patient to a real row in the hospital archive.

    python link_archive_records.py

The archive (`data/hospital data analysis.csv`, ~984 rows - the Tebra
stand-in) and the registered patient list are deliberately separate: the
archive is a general hospital's visit history, not this practice's own
patients. But the Records screen's Match column, the Excel export's
Archive columns, and any trial criterion using `archive_condition` /
`has_hospital_record` all need a patient's `hospital_id` to actually be
set before they have anything to compare against - and most seeded
patients start with none.

This gives every registered patient who doesn't already have one a
distinct, unused archive `patient_id`, picked at random but never reused
across two patients. It changes `Client.hospital_id` only - nothing in
the archive itself is touched, added, or removed.

Safe to re-run: only patients with `hospital_id IS NULL` are touched, so
a second run has nothing left to do.
"""

from __future__ import annotations

import random

from app.hospital import HospitalRecord
from app.models import Client, SessionLocal, init_db

random.seed(7)


def main() -> None:
    init_db()
    db = SessionLocal()

    unlinked = (db.query(Client).filter_by(archived=False)
                .filter(Client.hospital_id.is_(None))
                .order_by(Client.id).all())
    if not unlinked:
        print("Every registered patient already has a hospital_id - nothing to do.")
        db.close()
        return

    used = {h for (h,) in db.query(Client.hospital_id)
            .filter(Client.hospital_id.isnot(None)).all()}
    available = [pid for (pid,) in db.query(HospitalRecord.patient_id).distinct().all()
                if pid not in used]
    random.shuffle(available)

    if len(available) < len(unlinked):
        print(f"Only {len(available)} unused archive records for "
             f"{len(unlinked)} unlinked patients - linking as many as possible.")

    for client, patient_id in zip(unlinked, available):
        client.hospital_id = patient_id
        print(f"  linked {client.name!r} (client {client.id}) -> hospital_id {patient_id}")

    db.commit()

    total = db.query(Client).filter_by(archived=False).count()
    linked = db.query(Client.hospital_id).filter(Client.hospital_id.isnot(None)).count()
    print(f"\n{linked} of {total} registered patients now linked to the archive.")
    db.close()


if __name__ == "__main__":
    main()
