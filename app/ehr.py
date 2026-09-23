"""The record a clinician writes, rather than the record a form fills in.

Four things live here: the encounter and its note, the prescription, the order,
and the charge. They are the four artefacts a visit actually produces, and the
platform had none of them - a patient could be screened, matched and imported,
and then the visit itself had nowhere to go.

**What is real here and what is deliberately not.** Every one of these has a
transmitting half that cannot be written in software:

* a prescription reaches a pharmacy through Surescripts, which requires DEA
  EPCS certification, identity-proofing of each prescriber, and two-factor
  signing per prescription - and for a psychiatric practice most of what is
  prescribed is controlled, so the certified path is the only lawful one;
* a lab order reaches a laboratory through an interface contract and LOINC
  mapping;
* a claim reaches a payer through a clearinghouse contract.

So this module records, signs, prints and tracks. It never claims to transmit,
and every screen it feeds says so. A prescription printed and handed to a
patient is a real prescription and always has been; a prescription this app
*said* it sent is a patient standing at a pharmacy counter with nothing waiting
for them.

**Signing is the point.** An unsigned note is a draft and may be edited freely.
A signed note is a legal record: it is locked, and a correction becomes an
addendum that names its author and its time, sitting after the original rather
than replacing it. Allowing a signed note to be silently rewritten is the single
most consequential thing an EHR can get wrong - it destroys the one property the
record is kept for.
"""

from __future__ import annotations

import enum
from datetime import date, datetime

from sqlalchemy import (Boolean, Date, DateTime, Enum, ForeignKey, Integer,
                        Numeric, String, Text)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .models import Base


# --- encounters and notes -------------------------------------------------


class NoteStatus(str, enum.Enum):
    draft = "draft"
    signed = "signed"


#  The note templates a practice actually uses. SOAP for a medical visit,
#  psychiatric intake and progress for this specialty, and a plain one for
#  everything that does not fit - because a template list with no escape hatch
#  is a template list people work around by putting everything in "Other".
TEMPLATES: dict[str, list[tuple[str, str]]] = {
    "SOAP": [
        ("subjective", "Subjective - what the patient reports"),
        ("objective", "Objective - examination and observed findings"),
        ("assessment", "Assessment - clinical impression"),
        ("plan", "Plan - treatment, follow-up, referrals"),
    ],
    "Psychiatric intake": [
        ("presenting", "Presenting problem and history"),
        ("psychiatric_history", "Past psychiatric history"),
        ("medical_history", "Past medical history and medications"),
        ("social", "Social, family and developmental history"),
        ("mse", "Mental state examination"),
        ("risk", "Risk assessment"),
        ("formulation", "Formulation and diagnosis"),
        ("plan", "Plan"),
    ],
    "Progress note": [
        ("interval", "Interval history since last visit"),
        ("mse", "Mental state examination"),
        ("response", "Response to treatment and side effects"),
        ("risk", "Risk assessment"),
        ("plan", "Plan"),
    ],
    "Free text": [("body", "Note")],
}


class Encounter(Base):
    """One visit, and the note written about it.

    Note body is stored as a dict of section key to text, so a template can gain
    a section without a migration and an old note keeps exactly the sections it
    was written with. A note that silently acquires empty sections because the
    template changed is a note that misrepresents what was recorded.
    """

    __tablename__ = "encounters"
    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"), index=True)
    provider_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True)

    seen_on: Mapped[date] = mapped_column(Date, default=date.today)
    reason: Mapped[str] = mapped_column(String(255), default="")
    template: Mapped[str] = mapped_column(String(64), default="SOAP")
    sections: Mapped[str] = mapped_column(Text, default="{}")      # JSON

    status: Mapped[NoteStatus] = mapped_column(
        Enum(NoteStatus), default=NoteStatus.draft)
    signed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    signed_by: Mapped[str] = mapped_column(String(160), default="")

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    client: Mapped["Client"] = relationship()                      # noqa: F821
    provider: Mapped["User | None"] = relationship()               # noqa: F821
    addenda: Mapped[list["Addendum"]] = relationship(
        back_populates="encounter", cascade="all, delete-orphan",
        order_by="Addendum.id")

    @property
    def is_signed(self) -> bool:
        return self.status == NoteStatus.signed

    @property
    def body(self) -> dict:
        import json

        try:
            loaded = json.loads(self.sections or "{}")
            return loaded if isinstance(loaded, dict) else {}
        except ValueError:
            return {}

    @property
    def filled(self) -> list[tuple[str, str]]:
        """(label, text) for the sections that were actually written.

        Empty sections are dropped rather than printed blank: a note that shows
        "Objective:" with nothing after it reads as an examination that found
        nothing, which is a clinical claim nobody made.
        """
        written = self.body
        labels = dict(TEMPLATES.get(self.template, TEMPLATES["Free text"]))
        return [(labels.get(key, key.replace("_", " ").title()), value)
                for key, value in written.items() if (value or "").strip()]


class Addendum(Base):
    """A correction to a signed note.

    Appended, never merged. The original stays exactly as it was signed, because
    that is what somebody signed - and an amended record that cannot show what it
    said before the amendment is not a record anybody can rely on.
    """

    __tablename__ = "encounter_addenda"
    id: Mapped[int] = mapped_column(primary_key=True)
    encounter_id: Mapped[int] = mapped_column(
        ForeignKey("encounters.id"), index=True)
    body: Mapped[str] = mapped_column(Text, default="")
    written_by: Mapped[str] = mapped_column(String(160), default="")
    written_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    encounter: Mapped["Encounter"] = relationship(back_populates="addenda")


# --- prescriptions --------------------------------------------------------


class RxStatus(str, enum.Enum):
    draft = "draft"
    signed = "signed"           # written and signed; ready to print or hand over
    dispensed = "dispensed"     # the practice recorded that it was collected
    cancelled = "cancelled"


class Prescription(Base):
    """A prescription this practice wrote.

    It is not sent anywhere. `printed_at` is the closest thing to a
    transmission event and means exactly what it says: a piece of paper was
    produced. The distinction is laboured in the UI too, because a prescriber
    who believes a prescription was routed to a pharmacy will not print it, and
    the patient finds out at the counter.

    Controlled substances are flagged rather than blocked. The practice knows
    which schedule a drug is in and the app does not; what the app can usefully
    do is refuse to imply that a controlled prescription was transmitted, since
    that route legally requires EPCS certification this software does not have.
    """

    __tablename__ = "prescriptions"
    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"), index=True)
    encounter_id: Mapped[int | None] = mapped_column(
        ForeignKey("encounters.id"), nullable=True, index=True)
    prescriber_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True)

    drug: Mapped[str] = mapped_column(String(255))
    strength: Mapped[str] = mapped_column(String(64), default="")
    form: Mapped[str] = mapped_column(String(64), default="")
    sig: Mapped[str] = mapped_column(String(500), default="")      # directions
    quantity: Mapped[str] = mapped_column(String(64), default="")
    refills: Mapped[int] = mapped_column(Integer, default=0)
    days_supply: Mapped[int | None] = mapped_column(Integer, nullable=True)
    substitution_allowed: Mapped[bool] = mapped_column(Boolean, default=True)
    is_controlled: Mapped[bool] = mapped_column(Boolean, default=False)
    pharmacy: Mapped[str] = mapped_column(String(255), default="")
    notes: Mapped[str] = mapped_column(Text, default="")

    status: Mapped[RxStatus] = mapped_column(Enum(RxStatus), default=RxStatus.draft)
    written_on: Mapped[date] = mapped_column(Date, default=date.today)
    signed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    signed_by: Mapped[str] = mapped_column(String(160), default="")
    printed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    client: Mapped["Client"] = relationship()                      # noqa: F821
    prescriber: Mapped["User | None"] = relationship()             # noqa: F821

    @property
    def is_signed(self) -> bool:
        return self.status in (RxStatus.signed, RxStatus.dispensed)

    @property
    def display(self) -> str:
        parts = [self.drug]
        if self.strength:
            parts.append(self.strength)
        if self.form:
            parts.append(self.form)
        return " ".join(parts)


# --- orders ---------------------------------------------------------------


class OrderStatus(str, enum.Enum):
    draft = "draft"
    placed = "placed"           # the requisition was produced and handed over
    resulted = "resulted"
    cancelled = "cancelled"


class Order(Base):
    """A lab or imaging request.

    Same shape and same honesty as a prescription: the app produces a
    requisition, records that it was placed, and holds the result when it comes
    back. Nothing is transmitted to a laboratory - that needs an interface
    contract and LOINC mapping, neither of which is a coding task.

    The result is stored as text plus an optional file, because a result that
    arrives as a PDF from a fax machine is the common case and refusing to
    record it until somebody retypes the numbers means it does not get recorded.
    """

    __tablename__ = "orders"
    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"), index=True)
    encounter_id: Mapped[int | None] = mapped_column(
        ForeignKey("encounters.id"), nullable=True, index=True)
    ordered_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True)

    kind: Mapped[str] = mapped_column(String(32), default="lab")   # lab | imaging
    name: Mapped[str] = mapped_column(String(255))
    reason: Mapped[str] = mapped_column(String(255), default="")
    priority: Mapped[str] = mapped_column(String(32), default="Routine")
    facility: Mapped[str] = mapped_column(String(255), default="")

    status: Mapped[OrderStatus] = mapped_column(
        Enum(OrderStatus), default=OrderStatus.draft)
    ordered_on: Mapped[date] = mapped_column(Date, default=date.today)
    placed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    result_text: Mapped[str] = mapped_column(Text, default="")
    result_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    result_abnormal: Mapped[bool] = mapped_column(Boolean, default=False)
    reviewed_by: Mapped[str] = mapped_column(String(160), default="")
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    client: Mapped["Client"] = relationship()                      # noqa: F821

    @property
    def awaiting_review(self) -> bool:
        """Resulted, and nobody has said they have seen it.

        The single most useful query in an orders module, and the one a
        practice is sued over: a result that arrived and sat unread.
        """
        return self.status == OrderStatus.resulted and not self.reviewed_at


# --- charges --------------------------------------------------------------


class ChargeStatus(str, enum.Enum):
    draft = "draft"
    ready = "ready"             # coded and checked, waiting to go to the biller
    submitted = "submitted"     # handed to the billing service or clearinghouse
    paid = "paid"
    denied = "denied"
    written_off = "written_off"


class Charge(Base):
    """What a visit is billed as.

    Amounts are Numeric, never float. A cent lost to binary rounding is a
    reconciliation nobody can close, and money in floating point is the oldest
    avoidable bug in this industry.

    `submitted` here means handed to whoever actually files claims. This app
    does not talk to a clearinghouse, and the screens say so - a billing module
    that implies it filed a claim produces an accounts-receivable report that is
    confidently wrong.
    """

    __tablename__ = "charges"
    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"), index=True)
    encounter_id: Mapped[int | None] = mapped_column(
        ForeignKey("encounters.id"), nullable=True, index=True)

    service_on: Mapped[date] = mapped_column(Date, default=date.today)
    cpt: Mapped[str] = mapped_column(String(16), default="")
    description: Mapped[str] = mapped_column(String(255), default="")
    icd10: Mapped[str] = mapped_column(String(120), default="")    # comma separated
    units: Mapped[int] = mapped_column(Integer, default=1)
    amount: Mapped[float] = mapped_column(Numeric(10, 2), default=0)
    payer: Mapped[str] = mapped_column(String(160), default="")

    status: Mapped[ChargeStatus] = mapped_column(
        Enum(ChargeStatus), default=ChargeStatus.draft)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    paid_amount: Mapped[float] = mapped_column(Numeric(10, 2), default=0)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    note: Mapped[str] = mapped_column(String(255), default="")

    client: Mapped["Client"] = relationship()                      # noqa: F821

    @property
    def outstanding(self) -> float:
        return float(self.amount or 0) - float(self.paid_amount or 0)


#  The codes a psychiatric practice bills most. A short list people recognise
#  beats a complete one they have to search - and anything missing can still be
#  typed, so the list helps without getting in the way.
COMMON_CPT = [
    ("90791", "Psychiatric diagnostic evaluation"),
    ("90792", "Psychiatric diagnostic evaluation with medical services"),
    ("99213", "Office visit, established patient, low complexity"),
    ("99214", "Office visit, established patient, moderate complexity"),
    ("99215", "Office visit, established patient, high complexity"),
    ("90832", "Psychotherapy, 30 minutes"),
    ("90834", "Psychotherapy, 45 minutes"),
    ("90837", "Psychotherapy, 60 minutes"),
    ("90833", "Psychotherapy, 30 min, with E/M"),
    ("90836", "Psychotherapy, 45 min, with E/M"),
    ("99401", "Preventive counselling, 15 minutes"),
    ("96127", "Brief emotional/behavioural assessment"),
]
