"""Clinical record - the half of the platform that mirrors Tebra's Facesheet.

A patient here has two kinds of data:

* what they told us on a form  ->  Submission / Answer
* what the practice records about them  ->  the models below

Both hang off the same Client row, which is the whole point: in the real setup
those live in two systems and somebody matches them by hand. Here they are one
row from the start, so the combined sheet is a join rather than a reconciliation.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .models import Base

# Where a row came from. Recorded on every clinical row that a sync could one
# day overwrite, because the day the Tebra connection lands the app has to
# answer one question per row: may I replace this?
#
#   TYPED   a person entered it here. The app owns it. A sync must never
#           silently overwrite it - somebody chose those words.
#   SYNCED  a copy of a row in Tebra. Tebra owns it; refreshing replaces it.
#   SEED    demo data shipped with the app. Owned by nobody, safe to discard.
#
# Adding this after a thousand rows exist would mean guessing the provenance of
# every one of them, and a guess here is indistinguishable from a fact.
TYPED = "typed"
SYNCED = "synced"
SEED = "seed"

SOURCE_LABEL = {
    TYPED: "entered here",
    SYNCED: "from Tebra",
    SEED: "sample data",
}


class Medication(Base):
    """One prescription. Mirrors a line on the Tebra medication list:
    'Adderall 20 mg tablet (Prescribed on 8/25/2026)'."""

    __tablename__ = "medications"
    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"))
    name: Mapped[str] = mapped_column(String(255))
    dose: Mapped[str] = mapped_column(String(64), default="")        # "20 mg"
    form: Mapped[str] = mapped_column(String(64), default="")        # "tablet"
    prescribed_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    stopped_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    prescriber: Mapped[str] = mapped_column(String(255), default="")
    notes: Mapped[str] = mapped_column(Text, default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    recorded_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    source: Mapped[str] = mapped_column(String(16), default=TYPED)
    external_id: Mapped[str] = mapped_column(String(64), default="")

    client: Mapped["Client"] = relationship(back_populates="medications")  # noqa: F821

    @property
    def display(self) -> str:
        """'Adderall 20 mg tablet' - the way the facesheet writes it."""
        return " ".join(p for p in (self.name, self.dose, self.form) if p)

    @property
    def days_active(self) -> int | None:
        """How long they have been on it. None when the chart does not say.

        Never treat None as zero - a missing start date is 'we do not know',
        and a trial's 8-week rule cannot be judged from it.
        """
        if not self.prescribed_on:
            return None
        end = self.stopped_on or date.today()
        return (end - self.prescribed_on).days


class Problem(Base):
    """A diagnosis. Tebra calls this list 'Problems'."""

    __tablename__ = "problems"
    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"))
    description: Mapped[str] = mapped_column(String(255))
    icd10: Mapped[str] = mapped_column(String(16), default="")
    onset: Mapped[date | None] = mapped_column(Date, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    recorded_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    source: Mapped[str] = mapped_column(String(16), default=TYPED)
    external_id: Mapped[str] = mapped_column(String(64), default="")

    client: Mapped["Client"] = relationship(back_populates="problems")  # noqa: F821

    @property
    def display(self) -> str:
        return f"{self.description}" + (f" ({self.icd10})" if self.icd10 else "")


class Allergy(Base):
    __tablename__ = "allergies"
    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"))
    substance: Mapped[str] = mapped_column(String(255))
    reaction: Mapped[str] = mapped_column(String(255), default="")
    severity: Mapped[str] = mapped_column(String(32), default="")
    recorded_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    client: Mapped["Client"] = relationship(back_populates="allergies")  # noqa: F821


class Immunization(Base):
    """A vaccine, and when it was given.

    `administered` is deliberately nullable and shown blank rather than as "not
    given". A chart that lists the recommended adult vaccines with an empty date
    is saying "we have no record", which is a different clinical statement from
    "this patient was not vaccinated" - the patient may well have had it
    elsewhere. Tebra draws the same distinction and so does this.
    """

    __tablename__ = "immunizations"
    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"), index=True)
    name: Mapped[str] = mapped_column(String(160))
    administered: Mapped[date | None] = mapped_column(Date, nullable=True)
    dose: Mapped[str] = mapped_column(String(64), default="")
    notes: Mapped[str] = mapped_column(String(255), default="")
    recorded_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    client: Mapped["Client"] = relationship()          # noqa: F821


#  The adult schedule Tebra lists by default. Shown as rows with no date when
#  nothing is recorded, so the absence is visible rather than the row missing.
ADULT_VACCINES = ["Influenza (LAIV, TIV)", "Pneumococcal (PCV, PPSV)",
                  "Tetanus; Diphtheria; Pertussis (Tdap, Td)", "Zoster (Shingles)"]


class VitalSigns(Base):
    """One set of readings. Stored as text because charts record them that way -
    '5\\' 10"', '177 lbs 0 oz' - and parsing them loses what was written."""

    __tablename__ = "vitals"
    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"))
    taken_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    bp: Mapped[str] = mapped_column(String(32), default="")
    hr: Mapped[str] = mapped_column(String(32), default="")
    temp: Mapped[str] = mapped_column(String(32), default="")
    height: Mapped[str] = mapped_column(String(32), default="")
    weight: Mapped[str] = mapped_column(String(32), default="")
    bmi: Mapped[str] = mapped_column(String(32), default="")
    spo2: Mapped[str] = mapped_column(String(32), default="")

    client: Mapped["Client"] = relationship(back_populates="vitals")  # noqa: F821


class ClinicalNote(Base):
    """Free-text history. Tebra's facesheet shows PMHx and SHx blocks."""

    __tablename__ = "clinical_notes"
    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"))
    kind: Mapped[str] = mapped_column(String(32), default="PMHx")   # PMHx | SHx | Other
    body: Mapped[str] = mapped_column(Text, default="")
    recorded_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    client: Mapped["Client"] = relationship(back_populates="notes")  # noqa: F821


class LabOrder(Base):
    __tablename__ = "lab_orders"
    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"))
    name: Mapped[str] = mapped_column(String(255))
    ordered_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(String(64), default="Needs Results")

    client: Mapped["Client"] = relationship(back_populates="labs")  # noqa: F821
