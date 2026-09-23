"""A message sent to more than one patient at once.

Everything else that leaves this app to a patient is one message to one
person, composed by the app itself, about one form. A broadcast is the
opposite of that on every axis: one message to many people, and the words are
whatever a staff member typed. That difference is why it lives in its own
module rather than as an option on the existing send flow - the safeguards
that flow relies on (a message that never names a form or a condition, so an
intercepted text discloses nothing) do not apply here, because the whole
point of a broadcast is to say something specific.

**So the danger moves from the message to the audience**, and the audience is
where this module puts its care:

* **Consent is checked per recipient, not once for the list.** A filter can
  match someone who has never consented to email or SMS, and matching them is
  not the same as being allowed to write to them. Every recipient goes through
  exactly the same allow_email / allow_sms gate `deliver()` already uses for a
  single form-link send - one gate, not two implementations of the same rule
  that can drift apart.

* **The recipient list is resolved twice - once to preview, once to send -
  and never trusted from the browser.** A count shown on a preview page is a
  string in HTML by the time the send button is pressed; recomputing the
  filter at send time is the only way to be sure the fifty people who
  actually receive a message are the fifty who were reviewed, not fifty
  chosen by whoever can edit a hidden form field.

* **Every attempt is logged as its own row**, success, skip and failure
  alike, the same discipline `deliver()` already applies to a single send. A
  broadcast that reports "sent to 40" with no way to ask "which 40" is not an
  audit trail, it is a rounding error waiting to be relied on.
"""

from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .models import Base


class Channel(str, enum.Enum):
    email = "email"
    sms = "sms"
    both = "both"


#  Mirrors SubmissionStatus without importing it as a filter vocabulary of its
#  own - "any", "not started", "partial", "completed", "never sent" are
#  questions a broadcast audience is filtered by, not states a submission is
#  ever actually in, and conflating the two would mean a new SubmissionStatus
#  value silently breaks a filter nobody was thinking about at the time.
FORM_STATUS = [
    ("", "Any form status"),
    ("not_started", "Form sent, not started"),
    ("partial", "Form partially completed"),
    ("completed", "Form completed"),
    ("none", "No form ever sent"),
]

VERDICTS = ["Looks eligible", "Needs review", "Not eligible", "No criteria"]


class Broadcast(Base):
    """One send: the message as written, the filter as applied, and the count.

    The filter is stored as plain columns rather than reconstructed from
    nothing, so a broadcast sent three months ago can still answer "who was
    this sent to and why" without anybody having to remember what "trial 4"
    meant at the time.
    """

    __tablename__ = "broadcasts"
    id: Mapped[int] = mapped_column(primary_key=True)

    subject: Mapped[str] = mapped_column(String(200), default="")
    body: Mapped[str] = mapped_column(Text, default="")
    channel: Mapped[Channel] = mapped_column(Enum(Channel), default=Channel.email)

    filter_trial_id: Mapped[int | None] = mapped_column(
        ForeignKey("trials.id"), nullable=True)
    filter_verdict: Mapped[str] = mapped_column(String(32), default="")
    filter_form_status: Mapped[str] = mapped_column(String(16), default="")

    recipient_count: Mapped[int] = mapped_column(Integer, default=0)
    sent_count: Mapped[int] = mapped_column(Integer, default=0)
    skipped_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)

    sent_by: Mapped[str] = mapped_column(String(160), default="")
    sent_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    filter_trial: Mapped["Trial | None"] = relationship()          # noqa: F821
    entries: Mapped[list["MessageLog"]] = relationship(            # noqa: F821
        order_by="MessageLog.id")

    @property
    def preview(self) -> str:
        return (self.body or "")[:140]


MAX_BODY = 1000


def matching_clients(db, *, trial_id: int | None, verdict: str, form_status: str):
    """Every active patient this filter selects, batched the way a real
    practice's worth of patients requires.

    Same shape as the two report screens and the caseload screen before them:
    fetch everybody once with what screening needs eager-loaded, fetch the
    whole archive in one query, and evaluate in Python. A broadcast filter run
    per-patient against the database is the identical N+1 this app has already
    paid for twice and fixed twice.
    """
    from sqlalchemy.orm import joinedload, selectinload

    from .hospital import records_for_many
    from .models import Answer, Client, Submission, SubmissionStatus
    from .trials import Trial, evaluate

    clients = (db.query(Client).filter_by(archived=False)
               .options(selectinload(Client.medications),
                        selectinload(Client.submissions_list)
                        .selectinload(Submission.answers)
                        .joinedload(Answer.question))
               .all())

    trial = db.get(Trial, trial_id) if trial_id else None
    archives = (records_for_many(db, (c.hospital_id for c in clients))
                if trial else {})

    def form_state(client) -> str:
        subs = client.submissions_list
        if not subs:
            return "none"
        if any(s.status in (SubmissionStatus.submitted, SubmissionStatus.reviewed)
               for s in subs):
            return "completed"
        if any(s.status == SubmissionStatus.partial for s in subs):
            return "partial"
        return "not_started"

    out = []
    for c in clients:
        if trial:
            screening = evaluate(c, trial, archives.get(c.hospital_id or 0, []))
            if verdict and screening.verdict != verdict:
                continue
        elif verdict:
            #  A verdict filter with no trial chosen matches nobody rather
            #  than everybody - "Needs review" with no trial selected has no
            #  defensible meaning, and guessing one is how the wrong list
            #  gets a message about a study they were never screened for.
            continue
        if form_status and form_state(c) != form_status:
            continue
        out.append(c)
    return out


def send(db, broadcast: Broadcast, clients: list, *, request, config) -> None:
    """Actually deliver the message, one attempt per recipient, every attempt logged.

    Reuses backend_for() and MessageLog exactly as the single-patient send
    does - one delivery mechanism and one audit trail for both, not a second
    implementation of "try to send, then write down what happened" that could
    quietly diverge from the first.
    """
    from . import messaging
    from .models import MessageLog, log

    channels = (["email", "sms"] if broadcast.channel == Channel.both
                else [broadcast.channel.value])

    sent = skipped = failed = 0
    for client in clients:
        for channel in channels:
            recipient = (client.email if channel == "email" else client.phone) or ""
            consented = (client.allow_email if channel == "email"
                        else client.allow_sms)

            if not recipient:
                status, detail = "skipped", f"No {channel} address on file"
            elif not consented:
                status, detail = "skipped", f"No consent recorded for {channel}"
            else:
                msg = messaging.Message(
                    to=recipient, channel=channel,
                    subject=broadcast.subject or config.get("name", "Practice notice"),
                    body=broadcast.body)
                backend = messaging.backend_for(channel, db)
                try:
                    backend.send(msg)
                    if isinstance(backend, messaging.FileBackend):
                        status, detail = "not sent", (
                            "No mail server configured - written to outbox.log")
                    else:
                        status, detail = "sent", ""
                except messaging.SendFailed as exc:
                    status, detail = "failed", str(exc)

            db.add(MessageLog(client_id=client.id, broadcast_id=broadcast.id,
                              channel=channel, recipient=recipient,
                              status=status, detail=detail,
                              backend=messaging.backend_for(channel, db).name))
            if status == "sent":
                sent += 1
            elif status == "failed":
                failed += 1
            else:
                skipped += 1

    broadcast.sent_count, broadcast.skipped_count, broadcast.failed_count = (
        sent, skipped, failed)
    log(db, f"Broadcast sent to {sent}/{len(clients)} recipients",
        "broadcast", broadcast.id)
