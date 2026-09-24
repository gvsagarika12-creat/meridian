"""Experience surveys: did the visit go well, in the patient's own words.

Separate from the clinical record on purpose. A trial-screening questionnaire
asks what the practice needs to know about the patient; this asks the reverse
question, and nothing here should ever grow a clinical field - a survey that
starts collecting symptoms is a screening form wearing a different name.

Same token discipline as the intake flow (see app/patient.py): a random,
revocable database row, not a signed payload, because a survey link is a
credential exactly the way a form link is one.

The message never names a form, a diagnosis, or a visit reason - just that the
practice would like feedback - for the same reason app/messaging.py gives
form-links generic wording: email and SMS are unencrypted in transit and at
rest on the device.
"""

from __future__ import annotations

import enum
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import DateTime, Enum, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, Session, mapped_column, relationship

from . import config, messaging
from .models import Base, MessageLog, SessionLocal, log
from .patient import _staff_session, new_token

router = APIRouter()

LINK_LIFETIME = timedelta(days=30)


class SurveyStatus(str, enum.Enum):
    sent = "sent"
    completed = "completed"


class ExperienceSurvey(Base):
    __tablename__ = "experience_surveys"

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"), index=True)
    # The visit or form this follows, when there is one to point at. Nullable -
    # a survey can also go out on its own (a quarterly check-in), not only
    # right after a submission.
    submission_id: Mapped[int | None] = mapped_column(
        ForeignKey("submissions.id"), nullable=True)
    token: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    status: Mapped[SurveyStatus] = mapped_column(Enum(SurveyStatus), default=SurveyStatus.sent)
    rating_overall: Mapped[int | None] = mapped_column(Integer, nullable=True)
    comments: Mapped[str] = mapped_column(Text, default="")
    sent_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime)

    client: Mapped["Client"] = relationship()          # noqa: F821
    submission: Mapped["Submission | None"] = relationship()  # noqa: F821

    @property
    def is_expired(self) -> bool:
        return datetime.utcnow() > self.expires_at

    @property
    def is_open(self) -> bool:
        return self.status == SurveyStatus.sent and not self.is_expired


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _compose(channel: str, *, first_name: str, practice: dict, url: str) -> messaging.Message:
    name = first_name or "there"
    practice_name = practice.get("name", "your practice")
    phone = practice.get("phone", "")

    if channel == "sms":
        body = (f"{practice_name}: we'd value your feedback on your recent visit. "
                f"{url} Questions? {phone}")
        return messaging.Message(to="", subject="", body=body, channel="sms")

    body = (
        f"Hello {name},\n\n"
        f"{practice_name} would like to hear how your recent visit went. "
        f"It takes less than a minute:\n\n"
        f"{url}\n\n"
        f"Please do not forward this link - it opens your survey.\n\n"
        f"If you were not expecting this, please call us on {phone}.\n\n"
        f"{practice_name}"
    )
    return messaging.Message(to="", subject=f"How did we do? - {practice_name}",
                             body=body, channel="email")


def deliver(db: Session, request: Request, survey: ExperienceSurvey,
           channels: list[str]) -> None:
    """Same shape as app/main.py's deliver() for form links: attempt each
    requested channel and record the outcome either way, a refusal included."""
    client = survey.client
    base = str(request.base_url).rstrip("/")
    url = f"{base}/survey/{survey.token}"

    for channel in channels:
        recipient = (client.email if channel == "email" else client.phone) or ""
        consented = client.allow_email if channel == "email" else client.allow_sms

        if not recipient:
            entry = ("skipped", f"No {channel} address on the client record")
        elif not consented:
            entry = ("skipped", f"No consent recorded for {channel} contact")
        else:
            msg = _compose(channel, first_name=client.first_name,
                          practice=config.load(), url=url)
            msg.to = recipient
            backend = messaging.backend_for(channel, db)
            try:
                backend.send(msg)
                if isinstance(backend, messaging.FileBackend):
                    entry = ("not sent", "No mail server configured - written to "
                                         "outbox.log. Copy the patient link instead.")
                else:
                    entry = ("sent", "")
            except messaging.SendFailed as exc:
                entry = ("failed", str(exc))

        status, detail = entry
        db.add(MessageLog(submission_id=None, client_id=client.id, channel=channel,
                          recipient=recipient, status=status, detail=detail,
                          backend=messaging.backend_for(channel, db).name))
        log(db, f"Experience Survey Link {status.title()} ({channel})",
            "experience_survey", survey.id)


# ------------------------------------------------------------------ the flow

@router.get("/survey/{token}", response_class=HTMLResponse)
def open_survey(token: str, request: Request, db: Session = Depends(get_db)):
    from .main import render

    survey = db.query(ExperienceSurvey).filter_by(token=token).first()
    if not survey:
        return render("patient_gone.html", {
            "request": request, "practice": config.load(),
            "reason": "This link is not valid.", "staff": _staff_session(request)})
    if survey.status == SurveyStatus.completed:
        return render("survey_patient.html", {
            "request": request, "practice": config.load(), "survey": survey,
            "done": True, "errors": [], "staff": _staff_session(request)})
    if survey.is_expired:
        return render("patient_gone.html", {
            "request": request, "practice": config.load(),
            "reason": "This link has expired. Please contact the practice for a new one.",
            "staff": _staff_session(request)})

    return render("survey_patient.html", {
        "request": request, "practice": config.load(), "survey": survey,
        "done": False, "errors": [], "staff": _staff_session(request)})


@router.post("/survey/{token}/submit")
async def submit_survey(token: str, request: Request, db: Session = Depends(get_db)):
    from .main import render

    survey = db.query(ExperienceSurvey).filter_by(token=token).first()
    if not survey:
        return RedirectResponse(f"/survey/{token}", status_code=303)
    if not survey.is_open:
        return RedirectResponse(f"/survey/{token}", status_code=303)

    posted = await request.form()
    try:
        rating = int((posted.get("rating") or "").strip())
    except ValueError:
        rating = 0

    if rating < 1 or rating > 5:
        return render("survey_patient.html", {
            "request": request, "practice": config.load(), "survey": survey,
            "done": False, "staff": _staff_session(request),
            "errors": ["Please choose a rating from 1 to 5."]})

    survey.rating_overall = rating
    survey.comments = (posted.get("comments") or "").strip()[:2000]
    survey.status = SurveyStatus.completed
    survey.completed_at = datetime.utcnow()
    log(db, "Experience Survey Completed", "experience_survey", survey.id)
    db.commit()
    return RedirectResponse(f"/survey/{token}", status_code=303)
