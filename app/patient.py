"""The patient-facing flow: open a link, fill pages, sign consents, submit.

Design notes worth keeping in mind when changing this:

* **Tokens are opaque database rows, not signed payloads.** A signed token
  (itsdangerous et al.) is stateless and therefore cannot be revoked - once issued
  it is valid until it expires. A random 256-bit token looked up in the database
  can be cancelled the instant someone forwards a link to the wrong person, which
  for PHI is the property that matters.

* **Validation runs on the server.** The browser also marks required fields, but
  that is a convenience. A hidden or skipped question must never be trusted from
  the client, so `missing_required` re-checks everything at submit time.

* **Nothing is destructive.** Saving a page upserts answers; a patient can go back
  and change an answer until they submit. After submit the link stops accepting
  writes entirely.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from . import cache, config
from .models import (
    Answer, ConsentSignature, Form, Question, SessionLocal, Submission,
    SubmissionStatus, log,
)

router = APIRouter()

LINK_LIFETIME = timedelta(days=14)

# Types where the patient may tick several boxes, so the answer is a list.
MULTI_TYPES = {"checkbox"}


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def new_token() -> str:
    return secrets.token_urlsafe(32)


def client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    return (fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else ""))


def pages_of(form: Form) -> list[int]:
    return sorted({q.page for q in form.questions}) or [1]


def questions_on(form: Form, page: int) -> list[Question]:
    return [q for q in form.questions if q.page == page and not q.office_use_only]


# Item labels the practice has already recorded on the client. The patient should
# not retype what staff typed when they created the record - and a mismatch
# between the two is worth seeing, not manufacturing.
PREFILL = {
    "first name:": "first_name", "first name": "first_name",
    "last name:": "last_name", "last name": "last_name",
    "date of birth:": "dob", "date of birth": "dob",
    "email:": "email", "email": "email",
    "phone number": "phone",
    "city:": "city", "city": "city",
    "state:": "state", "state": "state",
    "zip code:": "postal_code", "zip code": "postal_code",
}


# Only the block asking for the patient's own details. Matching on field name
# alone leaked the patient's city and zip into the PHARMACY block, which has a
# "City" and a "Zip Code" of its own and means something entirely different.
SELF_BLOCKS = ("your information", "patient's information")


def prefill_for(submission: Submission, question) -> dict[str, str]:
    """Values to show in an unanswered mixed-controls block."""
    client = submission.client
    if not client or question.qtype.value != "mixed_controls":
        return {}
    text = (question.text or "").lower()
    if not any(marker in text for marker in SELF_BLOCKS):
        return {}
    out: dict[str, str] = {}
    for item in question.items:
        field = PREFILL.get(item.label.strip().lower())
        if not field:
            continue
        value = getattr(client, field, None)
        if value:
            out[item.label] = value.isoformat() if hasattr(value, "isoformat") else str(value)
    return out


def answers_map(db: Session, submission: Submission) -> dict[int, Answer]:
    """question_id -> Answer. Templates read `.values` or `.mapping` off it."""
    return {a.question_id: a for a in submission.answers}


def _posted_value(q: Question, posted) -> str | None:
    """Turn this question's posted field(s) into the string we store.

    Returns None when the question was not on the submitted page at all, which is
    different from "submitted but left blank" - the first must leave any existing
    answer alone, the second must clear it.
    """
    if q.qtype.value == "mixed_controls":
        # Each sub-field posts as i{item id}. Collect them into an object keyed by
        # the item's LABEL, for the same reason the matrix is keyed by row label:
        # items can be reordered or added later, and an index-keyed answer would
        # silently shift underneath every submission already taken.
        got: dict[str, str] = {}
        seen_any = False
        for item in q.items:
            key = f"i{item.id}"
            if key not in posted:
                continue
            seen_any = True
            raw = posted.getlist(key) if hasattr(posted, "getlist") else [posted[key]]
            raw = [v for v in raw if v not in (None, "")]
            if raw:
                got[item.label] = ", ".join(raw)
        return json.dumps(got) if seen_any else None

    if q.qtype.value == "matrix":
        # Each row posts separately as q{id}__{row index}. Collect them into an
        # object keyed by row label, so the answer survives rows being reordered
        # or reworded later.
        got: dict[str, str] = {}
        seen_any = False
        for i, label in enumerate(q.rows):
            key = f"q{q.id}__{i}"
            if key not in posted:
                continue
            seen_any = True
            choice = (posted[key] or "").strip()
            if choice:
                got[label] = choice
        return json.dumps(got) if seen_any else None

    key = f"q{q.id}"
    if key not in posted:
        return None
    raw = posted.getlist(key) if hasattr(posted, "getlist") else [posted[key]]
    raw = [v for v in raw if v not in (None, "")]
    if q.qtype.value in MULTI_TYPES:
        return json.dumps(raw)
    return raw[0] if raw else ""


def save_page(db: Session, submission: Submission, page: int, posted: dict) -> None:
    """Upsert answers for one page. Questions absent from the post are left alone."""
    existing = {a.question_id: a for a in submission.answers}
    for q in questions_on(submission.form, page):
        value = _posted_value(q, posted)
        if value is None:
            continue

        if q.id in existing:
            existing[q.id].value = value
            existing[q.id].answered_at = datetime.utcnow()
        elif value:
            db.add(Answer(submission_id=submission.id, question_id=q.id, value=value))

def missing_required(db: Session, submission: Submission) -> list[str]:
    """Human labels for everything required and not properly answered.

    Three rules, because "answered" means three different things:

    * a **matrix** needs *every* row rated. A symptom checklist with four of nine
      rows filled in is not an answered question, and treating it as one is how a
      half-completed clinical scale reaches a reviewer looking complete.

    * a **mixed-controls block** is checked per item, so the message names the
      field rather than the whole eighteen-field block. The block's own required
      flag is deliberately ignored here: marking "Please enter your information."
      required would otherwise make Middle Initials and Apt./Unit mandatory.

    * **everything else** just needs a value.

    Returns display strings rather than Question objects because a mixed-controls
    failure is about one item, which has no Question of its own.
    """
    answers = answers_map(db, submission)
    missing: list[str] = []

    for q in submission.form.questions:
        if q.office_use_only:
            continue
        answer = answers.get(q.id)

        if q.qtype.value == "mixed_controls":
            given = answer.mapping if answer else {}
            for item in q.items:
                if item.required and not given.get(item.label):
                    missing.append(f"{q.text[:60]} - {item.label}")
            continue

        if not q.required:
            continue

        if q.qtype.value == "matrix":
            rated = answer.mapping if answer else {}
            if not q.rows or any(not rated.get(row) for row in q.rows):
                missing.append(q.text[:90])
        elif not (answer and answer.values):
            missing.append(q.text[:90])

    return missing


# ------------------------------------------------------------------ the flow

@router.get("/f/{token}", response_class=HTMLResponse)
def open_form(token: str, request: Request, page: int | None = None,
              db: Session = Depends(get_db)):
    from .main import render

    sub = db.query(Submission).filter_by(token=token).first()
    if not sub:
        return render("patient_gone.html",
                      {"request": request, "practice": config.load(), "reason": "This link is not valid.", "staff": _staff_session(request)})
    if sub.is_expired:
        return render("patient_gone.html",
                      {"request": request, "practice": config.load(), "reason": "This link has expired. "
                       "Please contact the practice for a new one.",
                       "staff": _staff_session(request)})
    if sub.status in (SubmissionStatus.submitted, SubmissionStatus.reviewed):
        return render("patient_done.html", {"request": request, "practice": config.load(), "sub": sub, "already": True,
                                            "staff": _staff_session(request)})

    if sub.status == SubmissionStatus.sent:
        sub.status = SubmissionStatus.opened
        log(db, "Form Opened", "submission", sub.id, ip=client_ip(request))
        db.commit()

    pages = pages_of(sub.form)
    current = page if page is not None else sub.current_page
    current = max(0, min(current, len(pages)))

    return render("patient_form.html", {
        "request": request, "practice": config.load(), "sub": sub, "form": sub.form,
        "page": current, "pages": pages, "total": len(pages),
        "questions": questions_on(sub.form, pages[current - 1]) if current >= 1 else [],
        "answers": answers_map(db, sub),
        "percent": int(current / (len(pages) + 1) * 100),
        "consents": signable(sub.form),
        "client_first_name": sub.client.first_name if sub.client else "there",
        "preview": False, "errors": [], "staff": _staff_session(request),
    })


@router.post("/f/{token}/save")
async def save(token: str, request: Request, db: Session = Depends(get_db)):
    sub = db.query(Submission).filter_by(token=token).first()
    if not sub or not sub.is_open:
        return RedirectResponse(f"/f/{token}", status_code=303)

    posted = await request.form()
    page = int(posted.get("_page", 1))
    direction = posted.get("_go", "next")

    save_page(db, sub, pages_of(sub.form)[page - 1], posted)
    sub.status = SubmissionStatus.partial
    target = page + 1 if direction == "next" else max(0, page - 1)
    sub.current_page = target
    log(db, "Form Progress Saved", "submission", sub.id, ip=client_ip(request))
    db.commit()
    return RedirectResponse(f"/f/{token}?page={target}", status_code=303)


@router.post("/f/{token}/submit")
async def submit(token: str, request: Request, db: Session = Depends(get_db)):
    from .main import render

    sub = db.query(Submission).filter_by(token=token).first()
    if not sub or not sub.is_open:
        return RedirectResponse(f"/f/{token}", status_code=303)

    posted = await request.form()
    pages = pages_of(sub.form)
    if posted.get("_page"):
        save_page(db, sub, pages[int(posted["_page"]) - 1], posted)
        db.flush()

    # Server-side gate. The browser's `required` attribute is a convenience only.
    missing = missing_required(db, sub)
    if missing:
        db.commit()
        return render("patient_form.html", {
            "request": request, "practice": config.load(), "sub": sub, "form": sub.form,
            "page": len(pages), "pages": pages, "total": len(pages),
            "questions": questions_on(sub.form, pages[-1]),
            "answers": answers_map(db, sub), "percent": 95,
            "consents": signable(sub.form),
            "client_first_name": sub.client.first_name if sub.client else "there",
            "preview": False,
            "errors": missing, "staff": _staff_session(request),
        })

    signer = (posted.get("_signature") or "").strip()
    if signer:
        ip, ua = client_ip(request), request.headers.get("user-agent", "")[:500]
        #  signable(), not sub.form.consents: only documents the patient was
        #  actually shown are signed. A consent with no wording never reaches
        #  this loop, so it can never produce a signature whose document hash is
        #  the hash of nothing.
        for consent in signable(sub.form):
            body = f"{consent.name}\n{consent.body}"
            db.add(ConsentSignature(
                submission_id=sub.id, consent_id=consent.id, signed_name=signer,
                ip_address=ip, user_agent=ua,
                document_hash=hashlib.sha256(body.encode("utf-8")).hexdigest(),
            ))
            log(db, "Consent Form Signed", "submission", sub.id, ip=ip)

    # What the patient told us about themselves updates the record, so the
    # address they gave is the address the practice has. Staff entered only name,
    # email, DOB and phone; everything else arrives here.
    _apply_to_client(sub)

    sub.status = SubmissionStatus.submitted
    sub.submitted_at = datetime.utcnow()
    sub.read_by_staff = False
    #  The one invalidation that genuinely matters: a form arriving must show up
    #  on the navigation now, not up to the TTL later. Staff watch that badge.
    cache.drop_nav()
    log(db, "Form Submitted", "submission", sub.id, ip=client_ip(request))
    db.commit()
    return RedirectResponse(f"/f/{token}/done", status_code=303)


def _apply_to_client(submission: Submission) -> None:
    """Copy the patient's own details from their answers onto the client record.

    Only fills blanks and only from the block that asks about the patient
    themselves. An existing value is left alone - if staff typed one thing and the
    patient another, that disagreement is worth seeing on the form rather than
    being quietly overwritten in the chart.
    """
    client = submission.client
    if not client:
        return

    by_question = {a.question_id: a for a in submission.answers}
    for q in submission.form.questions:
        if q.qtype.value != "mixed_controls":
            continue
        if not any(marker in (q.text or "").lower() for marker in SELF_BLOCKS):
            continue
        answer = by_question.get(q.id)
        if not answer:
            continue

        for item in q.items:
            field = PREFILL.get(item.label.strip().lower())
            value = (answer.mapping.get(item.label) or "").strip()
            if not field or not value or getattr(client, field, None):
                continue
            if field == "dob":
                try:
                    client.dob = date.fromisoformat(value[:10])
                except ValueError:
                    pass
            else:
                setattr(client, field, value)


def signable(form: Form) -> list:
    """The consents attached to this form that a patient can actually sign.

    A consent with no wording is skipped rather than shown. Signing one would
    produce the worst artefact this app is capable of: an evidence bundle whose
    document hash is the hash of an empty string, recorded against a patient who
    was shown a title and nothing else. Twenty such signatures are not twenty
    consents, they are twenty identical hashes.

    Skipping rather than refusing the whole submission is deliberate. A consent
    nobody has written yet is a fault in the practice's setup, and taking it out
    on the patient - who cannot fix it and may be mid-form - would turn an
    administrative omission into an outage. Staff are warned where the omission
    actually is, on the consent screen and the form editor.
    """
    return [fc.consent for fc in form.consents if (fc.consent.body or "").strip()]


def _staff_session(request: Request) -> bool:
    """True when a signed-in member of staff is looking at a patient page.

    The patient screens are public, so they must stay a dead end for a patient -
    no navigation into the practice's records. But staff testing a form in the
    same window were stranded on the thank-you page with no way back, so they
    get a link and a patient does not.
    """
    try:
        return bool(request.session.get("uid"))
    except (AssertionError, AttributeError):
        return False


@router.get("/f/{token}/done", response_class=HTMLResponse)
def done(token: str, request: Request, db: Session = Depends(get_db)):
    from .main import render
    sub = db.query(Submission).filter_by(token=token).first()
    if not sub:
        return render("patient_gone.html",
                      {"request": request, "practice": config.load(),
                       "reason": "This link is not valid.", "staff": _staff_session(request)})
    return render("patient_done.html",
                  {"request": request, "practice": config.load(), "sub": sub,
                   "already": False, "staff": _staff_session(request)})
