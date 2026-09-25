"""Appointments, and the reminders that fall out of them.

Two different things share this screen, and they are worth keeping apart.

An **appointment** is a fact somebody entered: this patient is seeing this
clinician at this time. It is stored.

A **reminder** is not stored at all. It is a question the data answers on being
asked - "whose form has been sitting unfilled for a week?", "who is due
tomorrow and has not returned their paperwork?". Storing those would mean
keeping them in step with the thing they describe, and a reminder that outlives
the reason for it is worse than no reminder: staff learn to ignore the list.

So reminders are derived on every page load, the same way the Match column is.
Complete the form and the reminder is gone, with nothing to tick off.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy import Date, DateTime, Enum, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .models import Base, Submission, SubmissionStatus


class Attendance(str, enum.Enum):
    scheduled = "scheduled"
    attended = "attended"
    cancelled = "cancelled"
    no_show = "no show"


class AppointmentChannel(str, enum.Enum):
    """How the visit happens - separate from `kind`, which is what the visit
    is for. A Follow-up can be either; conflating the two would mean losing
    one fact to record the other.

    Not named Channel: app/broadcasts.py already has a Channel enum (email /
    sms / both) for message delivery, and SQLAlchemy names a Postgres enum
    type after the Python class by default - two classes both called Channel
    would both want a type literally named "channel"."""
    in_person = "in_person"
    online = "online"


KINDS = ["Screening visit", "Consultation", "Follow-up", "Trial visit",
         "Telehealth", "Assessment", "Other"]


class Appointment(Base):
    """One booking. Times are stored naive, in the practice's own clock.

    No timezone handling: a single-site practice books in local time, reads in
    local time, and introducing UTC conversion here would mean every screen has
    to convert back - a lot of machinery to solve a problem this practice does
    not have.
    """

    __tablename__ = "appointments"

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"), index=True)
    provider_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"),
                                                    nullable=True, index=True)

    on_day: Mapped[date] = mapped_column(Date, index=True)
    at_time: Mapped[str] = mapped_column(String(5), default="")     # "14:30"
    minutes: Mapped[int] = mapped_column(Integer, default=30)

    kind: Mapped[str] = mapped_column(String(64), default="Screening visit")
    channel: Mapped[AppointmentChannel] = mapped_column(
        Enum(AppointmentChannel, name="appointment_channel"),
        default=AppointmentChannel.in_person)
    location: Mapped[str] = mapped_column(String(128), default="")
    notes: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[Attendance] = mapped_column(Enum(Attendance),
                                               default=Attendance.scheduled)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    client: Mapped["Client"] = relationship()        # noqa: F821
    provider: Mapped["User | None"] = relationship() # noqa: F821

    @property
    def when(self) -> str:
        return f"{self.at_time} " if self.at_time else ""

    @property
    def is_past(self) -> bool:
        return self.on_day < date.today()


# --- reminders -------------------------------------------------------------

CHASE_AFTER = timedelta(days=3)      # a form unopened this long is worth a nudge


@dataclass
class Reminder:
    urgency: str          # "overdue" | "soon" | "info"
    what: str
    who: str
    client_id: int
    detail: str = ""


def reminders(db, today: date | None = None) -> list[Reminder]:
    """Everything worth chasing, worked out fresh each time.

    Ordered by how much it matters, not by date: a form nobody has opened in a
    fortnight is more urgent than tomorrow's appointment, whatever the calendar
    says.
    """
    today = today or date.today()
    out: list[Reminder] = []

    # 1. Appointments today and tomorrow.
    upcoming = (db.query(Appointment)
                .filter(Appointment.status == Attendance.scheduled,
                        Appointment.on_day >= today,
                        Appointment.on_day <= today + timedelta(days=1))
                .order_by(Appointment.on_day, Appointment.at_time).all())
    for a in upcoming:
        when = "today" if a.on_day == today else "tomorrow"
        out.append(Reminder("soon", f"{a.kind} {when}", a.client.name, a.client_id,
                            f"{a.when}with {a.provider.name if a.provider else 'unassigned'}"))

    # 2. Forms sent and not returned. The number of days is the whole point -
    #    "outstanding" tells staff nothing about which one to chase first.
    open_forms = (db.query(Submission)
                  .filter(Submission.status.in_([SubmissionStatus.sent,
                                                 SubmissionStatus.opened,
                                                 SubmissionStatus.partial]))
                  .all())
    for s in open_forms:
        if not s.sent_at:
            continue
        waiting = (datetime.utcnow() - s.sent_at).days
        if waiting < CHASE_AFTER.days:
            continue
        started = s.status != SubmissionStatus.sent
        out.append(Reminder(
            "overdue" if waiting >= 7 else "info",
            f"{s.form.name} not returned",
            s.client.name if s.client else "—",
            s.client_id or 0,
            f"sent {waiting} days ago" + (", started but not finished" if started else "")))

    # 3. Appointments that came and went without being marked.
    stale = (db.query(Appointment)
             .filter(Appointment.status == Attendance.scheduled,
                     Appointment.on_day < today).all())
    for a in stale:
        out.append(Reminder("info", f"{a.kind} not marked attended", a.client.name,
                            a.client_id, f"was {a.on_day:%d %b}"))

    rank = {"overdue": 0, "soon": 1, "info": 2}
    out.sort(key=lambda r: rank.get(r.urgency, 9))
    return out


def month_grid(db, year: int, month: int, provider_id: int | None = None):
    """Weeks of days, each with its appointments, ready to render."""
    import calendar as cal

    first = date(year, month, 1)
    last = date(year, month, cal.monthrange(year, month)[1])
    # Pad to whole weeks, Sunday first, the way a wall calendar reads.
    start = first - timedelta(days=(first.weekday() + 1) % 7)
    end = last + timedelta(days=(5 - last.weekday()) % 7 + 1)

    q = db.query(Appointment).filter(Appointment.on_day >= start,
                                     Appointment.on_day <= end)
    if provider_id:
        q = q.filter(Appointment.provider_id == provider_id)

    by_day: dict[date, list[Appointment]] = {}
    for a in q.order_by(Appointment.at_time).all():
        by_day.setdefault(a.on_day, []).append(a)

    weeks, day = [], start
    while day <= end:
        week = []
        for _ in range(7):
            week.append((day, by_day.get(day, []), day.month == month))
            day += timedelta(days=1)
        weeks.append(week)
    return weeks


def due_count(db, today: date | None = None) -> int:
    """How many reminders there are, without building them all.

    Every page draws the navigation, so this runs on every page. Two counts is
    cheap; assembling the list and measuring it is not.
    """
    today = today or date.today()
    soon = (db.query(Appointment)
            .filter(Appointment.status == Attendance.scheduled,
                    Appointment.on_day <= today + timedelta(days=1))
            .count())
    waiting = (db.query(Submission)
               .filter(Submission.status.in_([SubmissionStatus.sent,
                                              SubmissionStatus.opened,
                                              SubmissionStatus.partial]),
                       Submission.sent_at
                       <= datetime.utcnow() - CHASE_AFTER)
               .count())
    return soon + waiting
