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
    """A charge's journey from coded to closed.

    `pending_approval` and `approved` are a real workflow, not decoration: the
    person who codes a visit and the person who signs it off are usually
    different, and in a practice where they are the same person the step still
    exists so that "somebody checked this" is recorded rather than assumed.
    """

    draft = "draft"
    pending_approval = "pending_approval"
    approved = "approved"
    ready = "ready"             # kept: charges coded before approval existed
    submitted = "submitted"     # handed to the billing service or clearinghouse
    paid = "paid"
    denied = "denied"
    written_off = "written_off"


#  What may follow what. A status machine written down beats one implied by
#  which buttons a template happens to draw - the buttons can then be generated
#  from it, and a route can refuse a transition the UI never offered.
CHARGE_NEXT: dict[ChargeStatus, list[tuple[ChargeStatus, str]]] = {
    ChargeStatus.draft: [(ChargeStatus.pending_approval, "Send for approval")],
    ChargeStatus.pending_approval: [(ChargeStatus.approved, "Approve"),
                                    (ChargeStatus.draft, "Send back to draft")],
    ChargeStatus.approved: [(ChargeStatus.submitted, "Submit"),
                            (ChargeStatus.pending_approval, "Send back for approval")],
    ChargeStatus.ready: [(ChargeStatus.submitted, "Submit"),
                         (ChargeStatus.draft, "Send back to draft")],
    ChargeStatus.submitted: [(ChargeStatus.paid, "Mark paid"),
                             (ChargeStatus.denied, "Mark denied"),
                             (ChargeStatus.written_off, "Write off")],
    ChargeStatus.denied: [(ChargeStatus.pending_approval, "Rework"),
                          (ChargeStatus.written_off, "Write off")],
    ChargeStatus.paid: [],
    ChargeStatus.written_off: [],
}


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

    #  This charge's encounter in Tebra, once pushed - see app/tebra.py's
    #  push_charge(). Same reasoning as Client.tebra_patient_id: checking this
    #  first is what stops a charge posted twice becoming two encounters
    #  somebody on Tebra's side has to notice and remove by hand.
    tebra_encounter_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, unique=True, index=True)

    client: Mapped["Client"] = relationship()                      # noqa: F821
    claims: Mapped[list["Claim"]] = relationship(
        back_populates="charge", cascade="all, delete-orphan",
        order_by="Claim.id")
    payments: Mapped[list["Payment"]] = relationship(
        back_populates="charge", cascade="all, delete-orphan",
        order_by="Payment.id")

    @property
    def received(self) -> float:
        """Everything applied to this charge, from the payment records.

        Payments are the source of truth. `paid_amount` is kept only for rows
        coded before payments existed, and is used solely when there are no
        payment records - a total and a list of its parts that can disagree is
        a reconciliation that can never be closed, so only one of them counts
        at a time.
        """
        if self.payments:
            return float(sum(float(p.amount or 0) for p in self.payments))
        return float(self.paid_amount or 0)

    @property
    def outstanding(self) -> float:
        return float(self.amount or 0) - self.received

    @property
    def open_claim(self) -> "Claim | None":
        """The claim currently in play - the newest that has not been replaced."""
        replaced = {c.replaces_id for c in self.claims if c.replaces_id}
        live = [c for c in self.claims if c.id not in replaced]
        return live[-1] if live else None


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


# --- claims ---------------------------------------------------------------


class ClaimStatus(str, enum.Enum):
    prepared = "prepared"       # built here, not yet handed to the biller
    submitted = "submitted"     # given to the billing service or clearinghouse
    accepted = "accepted"       # the payer acknowledged receipt
    rejected = "rejected"       # bounced before adjudication - a format problem
    denied = "denied"           # adjudicated and refused - a coverage decision
    waiting_adjudication = "waiting_adjudication"
    needs_investigation = "needs_investigation"
    paid = "paid"
    appealed = "appealed"
    closed = "closed"


#  Rejected and denied are different things and the distinction is the whole
#  point of tracking claims. A rejection never reached adjudication - a missing
#  modifier, a wrong member id - and is fixed and resent. A denial is the payer
#  deciding it will not pay, and is appealed or written off. A practice that
#  treats them alike appeals format errors and resends coverage decisions, and
#  does neither well.
FIXABLE = (ClaimStatus.rejected,)
APPEALABLE = (ClaimStatus.denied,)
#  Neither refused nor settled. These are the ones that go quiet and are
#  forgotten, which is why they get their own tab rather than sitting inside
#  "everything else".
IN_FLIGHT = (ClaimStatus.submitted, ClaimStatus.accepted,
             ClaimStatus.waiting_adjudication, ClaimStatus.needs_investigation)


class Claim(Base):
    """One submission of one charge to one payer.

    Deliberately one charge per claim. Real claims can carry several service
    lines, and a practice billing surgical cases would need that - but a
    psychiatric visit is one line almost always, and a line-item model nobody
    needs is a model everybody has to read around. When the second line is
    genuinely required this grows a `claim_lines` table; until then it does not
    pretend to.

    `replaces` is the chain that matters. A rejected claim is corrected and
    resent, and the new claim points at the old one, so the history reads as
    "submitted, rejected for a missing modifier, corrected, paid" rather than
    as three unrelated rows.
    """

    __tablename__ = "insurance_claims"
    id: Mapped[int] = mapped_column(primary_key=True)
    charge_id: Mapped[int] = mapped_column(ForeignKey("charges.id"), index=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"), index=True)

    payer: Mapped[str] = mapped_column(String(160), default="")
    claim_number: Mapped[str] = mapped_column(String(64), default="")
    status: Mapped[ClaimStatus] = mapped_column(
        Enum(ClaimStatus), default=ClaimStatus.prepared)

    submitted_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    responded_on: Mapped[date | None] = mapped_column(Date, nullable=True)

    billed: Mapped[float] = mapped_column(Numeric(10, 2), default=0)
    #  What the payer says the service is worth. Everything above it is a
    #  contractual adjustment the practice may not bill the patient for, which
    #  is why it is recorded rather than inferred.
    allowed: Mapped[float] = mapped_column(Numeric(10, 2), default=0)
    paid: Mapped[float] = mapped_column(Numeric(10, 2), default=0)
    patient_responsibility: Mapped[float] = mapped_column(Numeric(10, 2), default=0)
    adjustment: Mapped[float] = mapped_column(Numeric(10, 2), default=0)

    denial_code: Mapped[str] = mapped_column(String(32), default="")
    denial_reason: Mapped[str] = mapped_column(String(255), default="")
    notes: Mapped[str] = mapped_column(Text, default="")

    replaces_id: Mapped[int | None] = mapped_column(
        ForeignKey("insurance_claims.id"), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    created_by: Mapped[str] = mapped_column(String(160), default="")

    charge: Mapped["Charge"] = relationship(back_populates="claims")
    client: Mapped["Client"] = relationship()                      # noqa: F821
    replaces: Mapped["Claim | None"] = relationship(remote_side=[id])

    @property
    def needs_work(self) -> bool:
        """Rejected or denied and not yet replaced or closed.

        The list a biller works from. A denial nobody has looked at is money
        the practice has decided, by inaction, not to collect.
        """
        return self.status in FIXABLE + APPEALABLE + (ClaimStatus.needs_investigation,)

    @property
    def outcome(self) -> str:
        if self.status == ClaimStatus.rejected:
            return "Rejected before adjudication - correct and resend"
        if self.status == ClaimStatus.denied:
            return "Denied by the payer - appeal or write off"
        if self.status == ClaimStatus.waiting_adjudication:
            return "With the payer, no decision yet"
        if self.status == ClaimStatus.needs_investigation:
            return "Something is wrong with this claim - somebody must look"
        return self.status.value.replace("_", " ")


# --- payments -------------------------------------------------------------


class PaymentSource(str, enum.Enum):
    payer = "payer"
    patient = "patient"
    adjustment = "adjustment"   # a write-down, not money


class Payment(Base):
    """Money received, as a record rather than a running total.

    A charge used to carry a `paid_amount` field, which answered "how much" and
    nothing else. A payment that cannot say when it arrived, from whom, by what
    method and against which claim is a number nobody can reconcile against a
    bank statement - and reconciliation is the entire job.

    Adjustments are recorded here too, as a source rather than a separate table:
    to the balance they behave identically, and splitting them means every
    outstanding calculation has to remember to consult two places.
    """

    __tablename__ = "payments"
    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"), index=True)
    #  Nullable: an unapplied patient payment is a real thing - somebody pays
    #  at the desk before the visit is coded - and refusing to record it until
    #  there is a charge to attach it to means it is not recorded.
    charge_id: Mapped[int | None] = mapped_column(
        ForeignKey("charges.id"), nullable=True, index=True)
    claim_id: Mapped[int | None] = mapped_column(
        ForeignKey("insurance_claims.id"), nullable=True)

    amount: Mapped[float] = mapped_column(Numeric(10, 2), default=0)
    source: Mapped[PaymentSource] = mapped_column(
        Enum(PaymentSource), default=PaymentSource.patient)
    method: Mapped[str] = mapped_column(String(32), default="")
    reference: Mapped[str] = mapped_column(String(120), default="")
    received_on: Mapped[date] = mapped_column(Date, default=date.today)
    note: Mapped[str] = mapped_column(String(255), default="")

    posted_by: Mapped[str] = mapped_column(String(160), default="")
    posted_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    #  This payment's id in Tebra, once pushed - see app/tebra.py's
    #  push_payment(). Same dedup reasoning as Charge.tebra_encounter_id.
    tebra_payment_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, unique=True, index=True)

    client: Mapped["Client"] = relationship()                      # noqa: F821
    charge: Mapped["Charge | None"] = relationship(back_populates="payments")


METHODS = ["Card", "Cash", "Cheque", "EFT / ERA", "Insurance payment",
           "Contractual adjustment", "Write-off"]


# --- statements -----------------------------------------------------------


class StatementKind(str, enum.Enum):
    initial = "initial"
    reminder = "reminder"
    final_notice = "final_notice"


class Delivery(str, enum.Enum):
    prepared = "prepared"       # produced, not yet sent
    sent = "sent"
    delivered = "delivered"
    failed = "failed"
    returned = "returned"       # post came back


class Statement(Base):
    """A bill sent to the patient for what insurance did not cover.

    The amount is stored rather than computed at display time. A statement is a
    claim about what was owed on the day it was produced, and re-deriving it
    later would quietly rewrite history the moment a payment landed - the
    patient holds a piece of paper saying one figure and the screen would show
    another, with no way to tell which they were sent.

    Escalation is a sequence the practice controls, not a status the app
    decides. initial -> reminder -> final notice is the order, and nothing here
    advances it automatically: a final notice sent because a cron job counted
    thirty days is how a practice sends a final notice to somebody whose cheque
    is in the post.
    """

    __tablename__ = "patient_statements"
    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"), index=True)

    kind: Mapped[StatementKind] = mapped_column(
        Enum(StatementKind), default=StatementKind.initial)
    amount: Mapped[float] = mapped_column(Numeric(10, 2), default=0)
    #  What the balance was when this went out, kept alongside the amount so a
    #  part-payment afterwards does not make the statement look wrong.
    balance_at_send: Mapped[float] = mapped_column(Numeric(10, 2), default=0)

    method: Mapped[str] = mapped_column(String(32), default="Post")
    status: Mapped[Delivery] = mapped_column(Enum(Delivery), default=Delivery.prepared)
    sent_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    due_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    detail: Mapped[str] = mapped_column(String(255), default="")
    note: Mapped[str] = mapped_column(Text, default="")

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    created_by: Mapped[str] = mapped_column(String(160), default="")

    client: Mapped["Client"] = relationship()                      # noqa: F821

    @property
    def is_out(self) -> bool:
        return self.status in (Delivery.sent, Delivery.delivered)

    @property
    def label(self) -> str:
        return {StatementKind.initial: "Statement",
                StatementKind.reminder: "Reminder",
                StatementKind.final_notice: "Final notice"}[self.kind]


STATEMENT_METHODS = ["Post", "Email", "SMS", "Handed over", "Patient portal"]
