"""Data model and database setup.

Single practice, so no practice_id anywhere. Every table that touches patient data
is soft-deleted rather than removed, and AuditEvent is append-only by design - there
is no update or delete path for it anywhere in this codebase.
"""

from __future__ import annotations

import enum
import json
import os
from datetime import datetime, date

from sqlalchemy import (
    Boolean, Date, DateTime, Enum, ForeignKey, Integer, String, Text,
    UniqueConstraint, create_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker
from sqlalchemy.pool import NullPool

# Database URL comes from the environment so the same code runs against a local
# container, a managed instance, or anything else without an edit. app/__init__.py
# loads .env before this module is imported.
LOCAL_DEFAULT = "postgresql+psycopg://ipmg:ipmg-local-dev@localhost:5433/ipmg_intake"

# Hosts name this variable differently and spell the scheme differently, and
# both mismatches fail at import with an error that says nothing useful. Vercel
# and Neon inject POSTGRES_URL / DATABASE_URL beginning "postgres://" or
# "postgresql://"; SQLAlchemy 2 dropped "postgres://" entirely, and plain
# "postgresql://" reaches for psycopg2, which is not installed here. Normalising
# once is cheaper than a deploy that dies on the first query.
def _normalise(raw: str) -> str:
    if raw.startswith("postgres://"):
        return "postgresql+psycopg://" + raw[len("postgres://"):]
    if raw.startswith("postgresql://"):
        return "postgresql+psycopg://" + raw[len("postgresql://"):]
    return raw


def _database_url() -> str:
    # Named variables first, in the order a host is most likely to mean them.
    for key in ("DATABASE_URL", "POSTGRES_URL", "POSTGRES_PRISMA_URL",
                "POSTGRES_URL_NON_POOLING"):
        raw = (os.environ.get(key) or "").strip()
        if raw:
            return _normalise(raw)

    # Then anything else that is unmistakably a Postgres URL. Vercel's storage
    # integration lets you choose the variable prefix, so the connection string
    # can arrive as STORAGE_URL or MYDB_URL - and falling back to localhost
    # because the name was unexpected produces a deploy that starts cleanly and
    # then fails on every query, which is the worst way to fail.
    for key, raw in sorted(os.environ.items()):
        raw = (raw or "").strip()
        if key.endswith("_URL") and raw.startswith(("postgres://", "postgresql://",
                                                    "postgresql+psycopg://")):
            return _normalise(raw)

    return LOCAL_DEFAULT


DATABASE_URL = _database_url()

# pool_pre_ping matters for a containerised database: the container can be stopped
# and restarted under a running app, and without it the first query afterwards
# fails on a connection that is already dead.
# On a serverless host every request may land in a fresh process, so a local
# pool has nothing to reuse and only holds server-side connections open that
# nobody will claim. Let the platform's pooler do the pooling instead, and use
# its pooled connection string in DATABASE_URL.
_serverless = bool(os.environ.get("VERCEL") or os.environ.get("HOSTED"))
_pool = {"poolclass": NullPool} if _serverless else {"pool_pre_ping": True}

engine = create_engine(DATABASE_URL, echo=False, **_pool)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class QuestionType(str, enum.Enum):
    short_text = "short_text"
    long_text = "long_text"
    number = "number"
    date = "date"
    email = "email"
    phone = "phone"
    dropdown = "dropdown"
    radio = "radio"
    checkbox = "checkbox"
    yes_no = "yes_no"
    scale = "scale"
    matrix = "matrix"
    signature = "signature"
    file_upload = "file_upload"
    address = "address"
    mixed_controls = "mixed_controls"
    heading = "heading"
    rich_text = "rich_text"
    attestation = "attestation"


class UserRole(str, enum.Enum):
    # Self-registered accounts land here. They can sign in and see one screen
    # telling them to wait. Everything else is closed until somebody who already
    # has access gives them a real role.
    pending = "pending"
    owner = "owner"
    admin = "admin"
    practitioner = "practitioner"
    front_desk = "front_desk"
    read_only = "read_only"


class SubmissionStatus(str, enum.Enum):
    sent = "sent"
    opened = "opened"
    partial = "partial"
    submitted = "submitted"
    reviewed = "reviewed"


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True)
    name: Mapped[str] = mapped_column(String(255))
    role: Mapped[UserRole] = mapped_column(Enum(UserRole), default=UserRole.practitioner)
    password_hash: Mapped[str] = mapped_column(String(255), default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False)
    last_login: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Two-factor authentication - see app/mfa.py. The secret is a Fernet token,
    # not the raw base32 value: a user row is the thing that ends up in every
    # database backup, and a backup is exactly where a live second factor must
    # not be readable in the clear.
    mfa_secret_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    mfa_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    mfa_enrolled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    @property
    def initials(self) -> str:
        parts = [p for p in self.name.split() if p]
        return "".join(p[0] for p in parts[:2]).upper() or "?"


class Folder(Base):
    """Form organisation. IntakeQ shows these as blue folder tiles with a count."""

    __tablename__ = "folders"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    position: Mapped[int] = mapped_column(Integer, default=0)
    forms: Mapped[list["Form"]] = relationship(back_populates="folder")


class Form(Base):
    """A questionnaire template. Consent forms attach to it many-to-many."""

    __tablename__ = "forms"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str] = mapped_column(Text, default="")
    colour: Mapped[str] = mapped_column(String(16), default="#9aa5b1")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_anonymous: Mapped[bool] = mapped_column(Boolean, default=False)
    version: Mapped[int] = mapped_column(Integer, default=1)

    # "questionnaire" goes to a patient; "note_template" is a clinician's own
    # document. IntakeQ keeps them in separate sections of My Forms, and they
    # behave differently - a note template is never sent to anybody - so the
    # distinction has to live on the row rather than in a naming convention.
    kind: Mapped[str] = mapped_column(String(16), default="questionnaire")
    folder_id: Mapped[int | None] = mapped_column(ForeignKey("folders.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    folder: Mapped[Folder | None] = relationship(back_populates="forms")
    questions: Mapped[list["Question"]] = relationship(
        back_populates="form", cascade="all, delete-orphan", order_by="Question.position"
    )
    consents: Mapped[list["FormConsent"]] = relationship(
        back_populates="form", cascade="all, delete-orphan"
    )

    @property
    def page_count(self) -> int:
        pages = {q.page for q in self.questions} or {1}
        return max(pages)


class Question(Base):
    __tablename__ = "questions"
    id: Mapped[int] = mapped_column(primary_key=True)
    form_id: Mapped[int] = mapped_column(ForeignKey("forms.id"))
    text: Mapped[str] = mapped_column(Text)
    help_text: Mapped[str] = mapped_column(Text, default="")
    qtype: Mapped[QuestionType] = mapped_column(Enum(QuestionType), default=QuestionType.short_text)
    required: Mapped[bool] = mapped_column(Boolean, default=False)
    position: Mapped[int] = mapped_column(Integer, default=0)
    page: Mapped[int] = mapped_column(Integer, default=1)
    options_raw: Mapped[str] = mapped_column(Text, default="")
    rows_raw: Mapped[str] = mapped_column(Text, default="")
    office_use_only: Mapped[bool] = mapped_column(Boolean, default=False)

    form: Mapped[Form] = relationship(back_populates="questions")
    items: Mapped[list["QuestionItem"]] = relationship(
        back_populates="question", cascade="all, delete-orphan",
        order_by="QuestionItem.position")

    @property
    def options(self) -> list[str]:
        """The answer choices. For a matrix, the scale running across the top."""
        return [o.strip() for o in (self.options_raw or "").split("\n") if o.strip()]

    @property
    def rows(self) -> list[str]:
        """Matrix row labels - the items being rated, one per line.

        A matrix is really N questions sharing one option set, and these are the N.
        Empty for every other question type.
        """
        return [r.strip() for r in (self.rows_raw or "").split("\n") if r.strip()]


class ItemKind(str, enum.Enum):
    """What one sub-field inside a Mixed Controls block looks like."""
    text = "text"
    date = "date"
    list = "list"        # shown as "Enter 1 option per line"
    checkbox = "checkbox"
    radio = "radio"


class QuestionItem(Base):
    """One labelled field inside a Mixed Controls question.

    IntakeQ's "Please enter your information." is a single question in the list but
    twenty fields on the page - name, DOB, marital status, race, phone, address.
    Modelling that as twenty separate questions would wreck the numbering the
    clinicians read; modelling it as one free-text box would lose every field.
    So a question owns items, and items own their own options.
    """

    __tablename__ = "question_items"
    id: Mapped[int] = mapped_column(primary_key=True)
    question_id: Mapped[int] = mapped_column(ForeignKey("questions.id"))
    label: Mapped[str] = mapped_column(String(255))
    kind: Mapped[ItemKind] = mapped_column(Enum(ItemKind), default=ItemKind.text)
    options_raw: Mapped[str] = mapped_column(Text, default="")
    required: Mapped[bool] = mapped_column(Boolean, default=False)
    position: Mapped[int] = mapped_column(Integer, default=0)
    width: Mapped[int] = mapped_column(Integer, default=1)   # 1 = half row, 2 = full

    question: Mapped["Question"] = relationship(back_populates="items")

    @property
    def options(self) -> list[str]:
        return [o.strip() for o in (self.options_raw or "").split("\n") if o.strip()]


class ConsentForm(Base):
    """Reusable consent documents, attachable to any questionnaire."""

    __tablename__ = "consent_forms"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    body: Mapped[str] = mapped_column(Text, default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class FormConsent(Base):
    __tablename__ = "form_consents"
    id: Mapped[int] = mapped_column(primary_key=True)
    form_id: Mapped[int] = mapped_column(ForeignKey("forms.id"))
    consent_id: Mapped[int] = mapped_column(ForeignKey("consent_forms.id"))
    form: Mapped[Form] = relationship(back_populates="consents")
    consent: Mapped[ConsentForm] = relationship()


class Client(Base):
    """The patient. Field set trimmed from IntakeQ's published Client object."""

    __tablename__ = "clients"
    id: Mapped[int] = mapped_column(primary_key=True)
    first_name: Mapped[str] = mapped_column(String(120), default="")
    last_name: Mapped[str] = mapped_column(String(120), default="")
    email: Mapped[str] = mapped_column(String(255), default="")
    phone: Mapped[str] = mapped_column(String(64), default="")
    dob: Mapped[date | None] = mapped_column(Date, nullable=True)
    city: Mapped[str] = mapped_column(String(120), default="")
    # Wide enough for a full state name ("District of Columbia"), not just a
    # two-letter code - the form now stores the readable version.
    state: Mapped[str] = mapped_column(String(64), default="")
    postal_code: Mapped[str] = mapped_column(String(16), default="")
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    # This patient's identity in IntakeQ, when they came from there. Unique and
    # indexed because it is what makes the import idempotent: without it a
    # second run has only a name and a birthday to go on, and re-running an
    # import is the most normal thing an operator ever does.
    intakeq_client_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, unique=True, index=True)

    # Where this row came from - see app/intakeq.py for the values. Blank on
    # every row that predates the import, which is honest: nobody knows now
    # whether a row typed in last March was typed or seeded, and writing a
    # guess into a provenance column defeats the column.
    source: Mapped[str] = mapped_column(String(16), default="")

    # This patient's chart id in Tebra, once one has been created for them.
    # Every push checks it first, which is the only thing standing between this
    # integration and two charts for one person - a failure that is silent, puts
    # their answers on one chart and their history on the other, and is usually
    # found months later by somebody reading a record that looks oddly empty.
    tebra_patient_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, unique=True, index=True)

    # The hospital's own patient number - the same number Tebra and the archive
    # use. Not this table's primary key and deliberately not a foreign key: it
    # is issued by another system, it is the only thing the two systems share,
    # and it stays with the patient across every visit. Nullable, because a
    # patient can be registered here before anyone has looked their number up.
    hospital_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)

    #  The clinician responsible for this patient. Nullable on purpose: a patient
    #  registered by reception before a clinician has picked them up is a normal
    #  state, and forcing a name in at that moment would mean staff choosing one
    #  at random to get past the form. Unassigned is a fact worth being able to
    #  see, not a gap to paper over.
    provider_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True, index=True)

    # Consent to be contacted on each channel, captured on a consent form.
    # Absent consent is not implied consent - both default to False.
    allow_email: Mapped[bool] = mapped_column(Boolean, default=False)
    allow_sms: Mapped[bool] = mapped_column(Boolean, default=False)
    contact_preference: Mapped[str] = mapped_column(String(16), default="email")

    # The clinical half of the record - see app/clinical.py.
    provider: Mapped["User | None"] = relationship("User", foreign_keys=[provider_id])

    medications: Mapped[list["Medication"]] = relationship(
        back_populates="client", cascade="all, delete-orphan")
    problems: Mapped[list["Problem"]] = relationship(
        back_populates="client", cascade="all, delete-orphan")
    allergies: Mapped[list["Allergy"]] = relationship(
        back_populates="client", cascade="all, delete-orphan")
    vitals: Mapped[list["VitalSigns"]] = relationship(
        back_populates="client", cascade="all, delete-orphan")
    notes: Mapped[list["ClinicalNote"]] = relationship(
        back_populates="client", cascade="all, delete-orphan")
    labs: Mapped[list["LabOrder"]] = relationship(
        back_populates="client", cascade="all, delete-orphan")
    submissions_list: Mapped[list["Submission"]] = relationship(
        back_populates="client", viewonly=True)

    @property
    def name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip() or "(no name)"

    @property
    def active_medications(self) -> list["Medication"]:
        return [m for m in self.medications if m.is_active]

    @property
    def active_problems(self) -> list["Problem"]:
        return [p for p in self.problems if p.is_active]

    @property
    def latest_vitals(self) -> "VitalSigns | None":
        return max(self.vitals, key=lambda v: v.taken_on or date.min, default=None)


class Submission(Base):
    __tablename__ = "submissions"
    id: Mapped[int] = mapped_column(primary_key=True)
    form_id: Mapped[int] = mapped_column(ForeignKey("forms.id"))
    form_version: Mapped[int] = mapped_column(Integer, default=1)
    client_id: Mapped[int | None] = mapped_column(ForeignKey("clients.id"), nullable=True)
    token: Mapped[str] = mapped_column(String(128), default="")
    status: Mapped[SubmissionStatus] = mapped_column(
        Enum(SubmissionStatus), default=SubmissionStatus.sent
    )
    sent_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    current_page: Mapped[int] = mapped_column(Integer, default=0)
    read_by_staff: Mapped[bool] = mapped_column(Boolean, default=False)

    form: Mapped[Form] = relationship()
    client: Mapped[Client | None] = relationship(back_populates="submissions_list")
    answers: Mapped[list["Answer"]] = relationship(
        back_populates="submission", cascade="all, delete-orphan")
    signatures: Mapped[list["ConsentSignature"]] = relationship(
        back_populates="submission", cascade="all, delete-orphan")

    @property
    def is_expired(self) -> bool:
        return bool(self.expires_at and datetime.utcnow() > self.expires_at)

    @property
    def is_open(self) -> bool:
        """Can the patient still fill this in?"""
        return not self.is_expired and self.status in (
            SubmissionStatus.sent, SubmissionStatus.opened, SubmissionStatus.partial)


class Answer(Base):
    """One patient response.

    `value` shape depends on the question type:
      scalar types  -> plain text        "Maria Alvarez"
      checkbox      -> JSON list         ["Alcohol", "Cannabis"]
      matrix        -> JSON object       {"Anxiety": "3 - Marked"}
    """

    __tablename__ = "answers"
    __table_args__ = (UniqueConstraint("submission_id", "question_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    submission_id: Mapped[int] = mapped_column(ForeignKey("submissions.id"))
    question_id: Mapped[int] = mapped_column(ForeignKey("questions.id"))
    value: Mapped[str] = mapped_column(Text, default="")
    answered_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    submission: Mapped["Submission"] = relationship(back_populates="answers")
    question: Mapped["Question"] = relationship()

    @property
    def mapping(self) -> dict[str, str]:
        """Row -> choice, for matrix answers. Empty dict for every other type."""
        if self.value.startswith("{"):
            try:
                loaded = json.loads(self.value)
                if isinstance(loaded, dict):
                    return loaded
            except json.JSONDecodeError:
                pass
        return {}

    @property
    def values(self) -> list[str]:
        """Always a list, whatever shape was stored."""
        if not self.value:
            return []
        if self.value.startswith("{"):
            return list(self.mapping.values())
        if self.value.startswith("["):
            try:
                return json.loads(self.value)
            except json.JSONDecodeError:
                pass
        return [self.value]

    @property
    def display(self) -> str:
        if self.mapping:
            return "; ".join(f"{row}: {choice}" for row, choice in self.mapping.items())
        return ", ".join(self.values)


class ConsentSignature(Base):
    """A signature is not an image - it is the evidence bundle around one.

    Name as typed, when, from where, which browser, and a hash of the exact
    document text signed. Without the hash you cannot later prove what the
    wording said at the moment of signing.
    """

    __tablename__ = "consent_signatures"
    id: Mapped[int] = mapped_column(primary_key=True)
    submission_id: Mapped[int] = mapped_column(ForeignKey("submissions.id"))
    consent_id: Mapped[int] = mapped_column(ForeignKey("consent_forms.id"))
    signed_name: Mapped[str] = mapped_column(String(255))
    signed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    ip_address: Mapped[str] = mapped_column(String(64), default="")
    user_agent: Mapped[str] = mapped_column(String(512), default="")
    document_hash: Mapped[str] = mapped_column(String(64), default="")

    submission: Mapped["Submission"] = relationship(back_populates="signatures")
    consent: Mapped["ConsentForm"] = relationship()


class AuditEvent(Base):
    """Append-only. Nothing in this codebase updates or deletes a row here.

    Surfaced in the UI as 'Latest Account Events', the way IntakeQ does it - which
    is a good pattern: the compliance log and the activity feed are the same thing,
    so the log stays honest because staff actually look at it.
    """

    __tablename__ = "audit_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    action: Mapped[str] = mapped_column(String(64))
    entity_type: Mapped[str] = mapped_column(String(64), default="")
    entity_id: Mapped[str] = mapped_column(String(64), default="")
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    ip_address: Mapped[str] = mapped_column(String(64), default="")
    at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class MessageLog(Base):
    """Every send attempt: channel, recipient, outcome.

    The link is deliberately absent. It is a credential that opens a patient's
    form, and this table is readable by staff who may not be entitled to open it.
    """

    __tablename__ = "message_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    submission_id: Mapped[int | None] = mapped_column(ForeignKey("submissions.id"), nullable=True)
    client_id: Mapped[int | None] = mapped_column(ForeignKey("clients.id"), nullable=True)
    # Which broadcast this attempt belongs to, for the ones sent to many
    # patients at once. Null for an ordinary single-patient form-link send.
    # A real column rather than matching rows by their timestamp, which would
    # blend two broadcasts sent minutes apart into each other's history.
    broadcast_id: Mapped[int | None] = mapped_column(
        ForeignKey("broadcasts.id"), nullable=True, index=True)
    channel: Mapped[str] = mapped_column(String(16))
    recipient: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(32))        # sent | failed | skipped
    detail: Mapped[str] = mapped_column(String(255), default="")
    backend: Mapped[str] = mapped_column(String(64), default="")
    at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    client: Mapped["Client | None"] = relationship()


class PasswordReset(Base):
    """A single-use, expiring token emailed to someone who forgot their password.

    Stored rather than signed so it can be revoked and so using it once burns it -
    a reset link that still works after the password changed is a back door.
    """

    __tablename__ = "password_resets"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    token: Mapped[str] = mapped_column(String(128), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    user: Mapped["User"] = relationship()

    @property
    def is_usable(self) -> bool:
        return self.used_at is None and datetime.utcnow() < self.expires_at


class AccessRequest(Base):
    """Someone asking for an account.

    Deliberately NOT a sign-up: it creates a request, not a login. An administrator
    approves it. On a system holding patient records, self-service registration
    would let anyone who finds the address give themselves a view of charts.
    """

    __tablename__ = "access_requests"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    email: Mapped[str] = mapped_column(String(255))
    note: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    handled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    handled_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)


def init_db() -> None:
    Base.metadata.create_all(engine)


def log(session, action: str, entity_type: str = "", entity_id: str = "",
        user_id: int | None = None, ip: str = "") -> None:
    """The only way to write an audit row.

    Every field is trimmed to the column it goes in. This looks like defensive
    noise and is not: `action` is 64 characters, an over-long one raises on
    flush, and the flush that fails is the one at the end of the operation that
    was being audited. A push to Tebra that had already created a chart was
    rolled back by exactly this - the side effect had happened, and the record
    of it was the thing that got discarded.

    A slightly shortened audit line is a small loss. A successful clinical
    action with no audit row, or an exception thrown after an external system
    has already been written to, is a large one.
    """
    session.add(AuditEvent(action=(action or "")[:64],
                           entity_type=(entity_type or "")[:64],
                           entity_id=str(entity_id)[:64],
                           user_id=user_id, ip_address=(ip or "")[:64]))


# Registering the clinical tables at import time, not inside init_db().
# Client declares relationships to Medication, Problem and the rest, so any module
# that imports models alone would otherwise fail to map. Placed at the bottom
# because clinical.py imports Base from here.
from . import clinical  # noqa: E402,F401
from . import intakeq  # noqa: E402,F401
from . import ehr  # noqa: E402,F401
from . import documents  # noqa: E402,F401
from . import broadcasts  # noqa: E402,F401
from . import mfa  # noqa: E402,F401
from . import hospital  # noqa: E402,F401
from . import trials  # noqa: E402,F401
from . import schedule  # noqa: E402,F401
from . import credentials  # noqa: E402,F401
