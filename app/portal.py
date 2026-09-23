"""The patient portal: a persistent account, a dashboard, and a message thread.

Distinct from `app/patient.py`. That module is the one-time-link flow - a
token that opens one form once and is revoked by being looked up, never a
credential anyone types twice. This module is the opposite shape: a patient
signs in as themselves, repeatedly, to see their own record and talk to their
care team. Both are legitimately "patient-facing"; they solve different
problems and neither should be bent to also do the other's job.

**A patient session and a staff session cannot be confused, structurally.**
`auth.current_user()` reads only the session key "uid"; the functions here
read only "patient_id". Neither side's code ever checks the other key, so a
bug in one cannot accidentally grant the other's access - this is the same
isolation property `auth.begin_mfa_challenge` relies on for a half-completed
staff login, applied to a second, independent identity rather than a second,
temporary state of the same one. Signing in as either clears the whole
session first, so a single browser is always exactly one identity, never a
blend of both.

**Portal access is issued, not requested.** A patient cannot sign up for
their own account. Self-registration would mean accepting an email address as
proof of identity for someone who may already exist in the archive under a
different one - exactly the ambiguity `identity.py` was built to resist for
staff, and worse here, because the two consequences of getting it wrong are a
stranger reading a chart and a real patient locked out of their own. Staff
issue access from a chart they already have open, the same trust boundary
that already governs adding a medication or linking a hospital number.

**What a patient sees, and what they deliberately do not.** Their own
medications, appointments, and the forms they have been sent - not the
clinician's notes, not a screening verdict, not another patient's anything.
The chart's free-text sections are a clinician's working notes, written for a
colleague, not phrased for the person they are about; showing them
unfiltered is not transparency, it is a different document pretending to be
the same one.

**The message thread is between the patient and the practice, not an
automated system.** Nothing here answers a clinical question. A message
about a medication goes to a person who can actually answer it, the same as
a phone call would, and is logged the same way a phone call is not - which is
the entire advantage of building this instead of leaving patients to call.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from fastapi import Request
from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship, Session

from .models import Base

IDLE_TIMEOUT = timedelta(minutes=15)
MAX_ATTEMPTS = 5
LOCKOUT = timedelta(minutes=15)

# Keyed apart from auth.py's own _attempts dict by a prefix, not a second
# dict, so the single is_locked/record_failure/clear_failures implementation
# in auth.py is the only lockout logic that exists anywhere in the app - one
# behaviour to get right, not two that could quietly diverge.
LOCKOUT_PREFIX = "portal:"


class PatientMessage(Base):
    """One message, one direction, one row.

    No conversation object above it - the thread for a patient is simply
    every row with their client_id, ordered by time. A message cannot be
    edited or withdrawn once sent, the same as a phone call once made: the
    record is of what was actually said, not what somebody wishes they had
    said.
    """

    __tablename__ = "patient_messages"
    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"), index=True)

    sender_role: Mapped[str] = mapped_column(String(16))       # "patient" | "staff"
    staff_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True)
    body: Mapped[str] = mapped_column(Text)

    sent_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    read_by_patient_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    read_by_staff_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    client: Mapped["Client"] = relationship()                  # noqa: F821
    staff_user: Mapped["User | None"] = relationship()         # noqa: F821

    @property
    def from_patient(self) -> bool:
        return self.sender_role == "patient"


MAX_MESSAGE_LENGTH = 2000


# --------------------------------------------------------------------- session
#
# The whole isolation property lives in these four functions reading and
# writing exactly one key, "patient_id", that auth.py's own functions never
# look at.


def sign_in(request: Request, client) -> None:
    request.session.clear()
    request.session["patient_id"] = client.id
    request.session["patient_seen"] = datetime.utcnow().isoformat()


def sign_out(request: Request) -> None:
    request.session.clear()


def current_patient(request: Request, db: Session):
    from .models import Client

    pid = request.session.get("patient_id")
    if not pid:
        return None
    return db.get(Client, pid)


def touch(request: Request) -> bool:
    """Refresh the idle timer. False means the session timed out.

    A shorter window than staff's twenty minutes, and its own session key -
    "patient_seen", not "seen" - so the two idle clocks can never be read
    against the wrong session's timeout by a future edit that forgets which
    constant belongs to which key.
    """
    seen = request.session.get("patient_seen")
    if not seen:
        return False
    try:
        last = datetime.fromisoformat(seen)
    except ValueError:
        return False
    if datetime.utcnow() - last > IDLE_TIMEOUT:
        return False
    request.session["patient_seen"] = datetime.utcnow().isoformat()
    return True


async def portal_auth_middleware(request: Request, call_next):
    """Guards /portal/* only. Everything outside it is none of this
    middleware's business, the same way the staff middleware now treats
    /portal as none of its own."""
    from fastapi.responses import RedirectResponse

    path = request.url.path
    if not path.startswith("/portal"):
        return await call_next(request)
    if path in ("/portal/login",) or path.startswith("/portal/logout"):
        return await call_next(request)

    if not request.session.get("patient_id"):
        return RedirectResponse("/portal/login", status_code=303)
    if not touch(request):
        request.session.clear()
        return RedirectResponse("/portal/login?timeout=1", status_code=303)
    return await call_next(request)


# ------------------------------------------------------------------ dashboard
#
# Pure functions over a Client already loaded - no query of their own, so a
# route that has already fetched the patient with the right eager-loads pays
# nothing extra to ask these for a summary.


def unread_message_count(db, client_id: int) -> int:
    return (db.query(PatientMessage)
            .filter_by(client_id=client_id, sender_role="staff",
                       read_by_patient_at=None).count())


def next_appointment(appointments, today: date | None = None):
    """The soonest scheduled appointment from an already-fetched list.

    Takes the list rather than a client and a db, matching how every other
    screen in this app already fetches a patient's appointments - once, with
    its own query - rather than through a relationship on Client that does
    not exist. Adding one only for this function would be a second way to
    reach the same rows, and two ways to ask the same question is how they
    drift.
    """
    today = today or date.today()
    upcoming = [a for a in appointments if a.on_day >= today
               and a.status.value == "scheduled"]
    return min(upcoming, key=lambda a: (a.on_day, a.at_time or ""), default=None)
