"""The hospital's own archive - the database the practice already holds.

This is the Tebra side of the platform, modelled the way the real thing behaves
rather than the way the demo happens to be wired. Two differences from
app/clinical.py matter:

* **No foreign key.** A hospital record is not owned by a Client row. It is a
  record that already existed, keyed by the hospital's own patient number, and
  it stays valid whether or not anybody has registered that person in this app.
  The join is `Client.hospital_id == HospitalRecord.patient_id` - a value match
  between two systems, which is exactly the join a coordinator does by hand.

* **Many rows per patient.** One visit is one row. This file happens to carry a
  single visit each, but the schema must not assume that: a patient who comes
  back gets another row under the same number, and the history is the point.

Nothing here is written by the app. It is imported, read, and compared against.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from .models import Base

# The source value for a visit recorded in this app rather than imported. It must
# never collide with a CSV filename, or the next import would delete it.
ENTERED_HERE = "Entered at the practice"


class HospitalRecord(Base):
    """One visit, as the hospital recorded it."""

    __tablename__ = "hospital_records"

    id: Mapped[int] = mapped_column(primary_key=True)

    # The hospital's patient number. Not unique - a returning patient reuses it,
    # which is the whole reason the number exists.
    patient_id: Mapped[int] = mapped_column(Integer, index=True)

    # The hospital identifies people by number, and that stays the join key -
    # names are ambiguous, get misspelled and change with marriage. These two
    # exist so a human can recognise a row and so a mistyped number can be
    # caught: a number whose name and date of birth do not match the patient in
    # front of you is the wrong number, and that is far easier to see than an
    # age that happens to be plausible.
    name: Mapped[str] = mapped_column(String(160), default="", index=True)
    dob: Mapped[date | None] = mapped_column(Date, nullable=True)

    age: Mapped[int | None] = mapped_column(Integer, nullable=True)
    gender: Mapped[str] = mapped_column(String(32), default="")
    condition: Mapped[str] = mapped_column(String(128), default="")
    procedure: Mapped[str] = mapped_column(String(128), default="")
    cost: Mapped[int | None] = mapped_column(Integer, nullable=True)
    length_of_stay: Mapped[int | None] = mapped_column(Integer, nullable=True)
    readmission: Mapped[bool] = mapped_column(Boolean, default=False)
    outcome: Mapped[str] = mapped_column(String(64), default="")
    satisfaction: Mapped[int | None] = mapped_column(Integer, nullable=True)

    imported_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    # Where this row came from: the CSV's filename for imported rows, or
    # ENTERED_HERE for one the practice recorded. The importer deletes by source
    # before loading, so a row this app wrote survives every re-import - and a
    # correction to the hospital's file still replaces the hospital's rows.
    source: Mapped[str] = mapped_column(String(255), default="")

    @property
    def entered_here(self) -> bool:
        return self.source == ENTERED_HERE

    __table_args__ = (Index("ix_hospital_patient_condition", "patient_id", "condition"),)

    @property
    def display(self) -> str:
        """'Diabetes - Insulin Therapy (Recovered)'."""
        parts = [self.condition]
        if self.procedure:
            parts.append(f"- {self.procedure}")
        if self.outcome:
            parts.append(f"({self.outcome})")
        return " ".join(p for p in parts if p)


def records_for_many(db, hospital_ids) -> dict[int, list[HospitalRecord]]:
    """Every visit for a whole list of patient numbers, in one query.

    The per-patient `records_for` below is right for one chart and wrong for a
    list: calling it in a loop over a caseload is a query per patient, which is
    invisible at the seven patients a demo has and fatal at the two thousand an
    IntakeQ import lands. Measured before this existed, the caseload screen
    projected to roughly thirty seconds and fifteen thousand queries at that
    size - past the point where a serverless request is killed, so the screen
    did not get slower, it stopped existing.

    Returns a dict so callers can index it exactly where they used to call
    records_for, and patients with no number or no visits map to an empty list
    rather than being absent - a missing key is a KeyError waiting for the one
    patient nobody tested with.
    """
    wanted = {int(h) for h in hospital_ids if h}
    found: dict[int, list[HospitalRecord]] = {h: [] for h in wanted}
    if not wanted:
        return found
    rows = (db.query(HospitalRecord)
            .filter(HospitalRecord.patient_id.in_(wanted))
            .order_by(HospitalRecord.id)
            .all())
    for row in rows:
        found.setdefault(row.patient_id, []).append(row)
    return found


def records_for(db, hospital_id: int | None) -> list[HospitalRecord]:
    """Every visit under one hospital number, newest identifier first.

    Returns an empty list for a patient who has no number yet, rather than
    raising: a patient the practice has not matched to the archive is an
    ordinary state, not an error.
    """
    if not hospital_id:
        return []
    return (db.query(HospitalRecord)
            .filter(HospitalRecord.patient_id == hospital_id)
            .order_by(HospitalRecord.id)
            .all())
