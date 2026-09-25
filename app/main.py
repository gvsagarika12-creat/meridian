"""FastAPI application - staff screens and the patient-facing form."""

from __future__ import annotations

import json
import os
import secrets
import subprocess
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlencode

from fastapi import Depends, FastAPI, Form as F, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload, selectinload

from .models import (
    AccessRequest, AuditEvent, Client, ConsentForm, ConsentSignature, Folder,
    Answer, Form, FormConsent, MessageLog, Question,
    ItemKind, QuestionItem, QuestionType, SessionLocal, Submission,
    SubmissionStatus, User, UserRole, log,
)
from . import auth, config, messaging, permissions as perms, records as rec,\
    users as um
from . import clinical
from .clinical import (
    Allergy, ClinicalNote, LabOrder, Medication, Problem, VitalSigns,
)
from .patient import (
    LINK_LIFETIME, client_ip, new_token, prefill_for, signable,
    router as patient_router,
)
from .hospital import (ENTERED_HERE, HospitalRecord, records_for,
                       records_for_many)
from . import identity
from . import credentials
from . import cache
from . import integrations
from . import intakeq
from . import broadcasts
from . import mfa
from . import portal
from . import documents
from . import ehr
from . import simulation
from . import tebra
from . import schedule
from . import trials
from .trials import Trial
from . import matching
from . import surveys
from .surveys import ExperienceSurvey, SurveyStatus
from .recovery import router as recovery_router
from .seed import seed

BASE = Path(__file__).resolve().parent
app = FastAPI(title=config.load()["name"])
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
templates = Jinja2Templates(directory=BASE / "templates")

class _StripMount:
    """Remove the serverless mount point from the request path.

    Vercel routes every request through a rewrite to /api/index, and that mount
    point arrives in the ASGI scope: a request for /setup reaches the app as
    /api/index/setup. Nothing then matches, the auth middleware sees a path that
    is not on its public list, redirects to /setup, and the browser loops until
    it gives up with ERR_TOO_MANY_REDIRECTS.

    Stripping it here - outside every other middleware - means routing, the
    public-path list and every generated URL all read the same corrected path.
    A locally-run app never carries the prefix, so this is a no-op there.
    """

    MOUNT = "/api/index"

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            path = scope.get("path") or "/"
            probe = any(k == b"x-probe" for k, _ in scope.get("headers", []))
            if path == self.MOUNT or path.startswith(self.MOUNT + "/"):
                scope = dict(scope)
                scope["path"] = path[len(self.MOUNT):] or "/"
                raw = scope.get("raw_path")
                if raw and raw.startswith(self.MOUNT.encode()):
                    scope["raw_path"] = raw[len(self.MOUNT):] or b"/"
            if scope.get("root_path"):
                scope = dict(scope)
                scope["root_path"] = ""
            if probe:
                body = (f"path={scope.get('path')!r} "
                        f"root={scope.get('root_path')!r} "
                        f"raw={scope.get('raw_path')!r}").encode()
                await send({"type": "http.response.start", "status": 200,
                            "headers": [(b"content-type", b"text/plain")]})
                await send({"type": "http.response.body", "body": body})
                return
        await self.app(scope, receive, send)


# Order matters and is counter-intuitive: the LAST middleware added is the
# OUTERMOST, so SessionMiddleware must be registered after auth_middleware in
# order to run before it. Reversed, the auth check sees no request.session at all.
app.middleware("http")(auth.auth_middleware)
#  Same session, same cookie, a completely separate check: this middleware
#  only ever looks at "/portal" paths and the "patient_id" key, auth_middleware
#  only ever looks at everything else and "uid" - see app/portal.py's module
#  docstring for why that separation, not a second cookie, is what actually
#  keeps the two identities from being confused.
app.middleware("http")(portal.portal_auth_middleware)
app.add_middleware(SessionMiddleware, secret_key=auth.session_secret(),
                   session_cookie="mbh_session", https_only=False, same_site="lax")


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def ago(when: datetime | None) -> str:
    if not when:
        return "-"
    secs = (datetime.utcnow() - when).total_seconds()
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{int(secs // 60)} minutes ago"
    if secs < 86400:
        return f"{int(secs // 3600)} hours ago"
    return f"{int(secs // 86400)} days ago"


templates.env.filters["ago"] = ago
templates.env.globals["can"] = perms.can
templates.env.globals["role_label"] = perms.role_label
templates.env.globals["P"] = perms
templates.env.globals["MIN_PW"] = auth.MIN_PASSWORD_LENGTH
# Where a clinical row came from - "entered here" / "from Tebra" / "sample data".
templates.env.globals["SOURCE"] = clinical.SOURCE_LABEL
templates.env.globals["sections_for"] = lambda name: ehr.TEMPLATES.get(
    name, ehr.TEMPLATES["Free text"])
templates.env.globals["today"] = date.today
templates.env.globals["COMMON_CPT"] = ehr.COMMON_CPT
templates.env.globals["CriterionKind"] = trials.CriterionKind
templates.env.globals["backup_codes_left"] = mfa.remaining_backup_codes
# The labels IntakeQ shows for each type, kept out of the enum so the
# stored value stays a stable identifier.
templates.env.globals["prefill_for"] = prefill_for
templates.env.globals["TYPE_LABELS"] = {
    "mixed_controls": "Mixed Controls",
    "radio": "Multiple Choice - Single Answer",
    "checkbox": "Multiple Choice - Multiple Answers",
    "long_text": "Open Answer",
    "short_text": "Short Answer",
    "matrix": "Matrix / Scale",
    "file_upload": "File Upload",
    "signature": "Signature",
    "attestation": "Attestation",
    "date": "Date",
}


def get_or_404(db: Session, model, pk):
    """db.get() returns None for a missing row; dereferencing that is a 500.
    A request for a record that isn't there is a 404, not a server fault."""
    obj = db.get(model, pk)
    if obj is None:
        raise HTTPException(status_code=404, detail=f"{model.__name__} {pk} not found")
    return obj


def needs(permission: str):
    """Route guard. 403 if the signed-in user's role does not carry `permission`."""
    def dependency(request: Request, db: Session = Depends(get_db)) -> User:
        user = auth.current_user(request, db)
        if not perms.can(user, permission):
            raise HTTPException(status_code=403, detail=permission)
        return user
    return dependency


@app.exception_handler(404)
async def not_found(request: Request, exc: HTTPException):
    db = SessionLocal()
    try:
        return templates.TemplateResponse(request, "404.html", {
            "request": request, "user": auth.current_user(request, db),
            "practice": config.load(), "unread": 0, "nav": "",
            "detail": str(exc.detail),
        }, status_code=404)
    finally:
        db.close()


@app.exception_handler(403)
async def forbidden(request: Request, exc: HTTPException):
    db = SessionLocal()
    try:
        user = auth.current_user(request, db)
        log(db, "Permission Denied", "permission", str(exc.detail),
            user_id=user.id if user else None)
        db.commit()
        return templates.TemplateResponse(request, "403.html", {
            "request": request, "user": user, "practice": config.load(),
            "unread": 0, "nav": "",
            "permission": perms.LABELS.get(str(exc.detail), str(exc.detail)),
        }, status_code=403)
    finally:
        db.close()


def render(name: str, context: dict):
    """Starlette 0.4x wants (request, name, context). Our context already carries
    the request, so call sites keep the simpler (name, context) shape."""
    return templates.TemplateResponse(context["request"], name, context)



# True when the app is served to a browser somewhere else rather than run as a
# desktop window on the user's own machine. Two things change: the browser does
# the downloading (there is no "your Downloads folder" on a server), and the
# disk may be read-only, so writing an export would fail outright.
HOSTED = bool(os.environ.get("VERCEL") or os.environ.get("HOSTED"))


def ctx(request: Request, db: Session, **extra) -> dict:
    base = {
        "request": request,
        "hosted": HOSTED,
        "user": auth.current_user(request, db),
        "practice": config.load(),
        # Both badges are cached for a few seconds. Every page draws them, and
        # nobody decides anything from a number in the navigation - so paying
        # two queries per page draw to be exactly current is the wrong trade.
        # Write paths call cache.drop_nav() so a badge is never visibly stale
        # while the thing that changed it is still on screen.
        "unread": cache.get_or_set(
            cache.NAV_UNREAD,
            lambda: db.query(Submission).filter_by(
                read_by_staff=False, status=SubmissionStatus.submitted).count()),
        # The badge beside Calendar. Counted, not listed, so every page pays for
        # one query rather than building the whole reminder list.
        "due": cache.get_or_set(cache.NAV_DUE, lambda: schedule.due_count(db)),
        #  Results that came back and nobody has said they have seen. The one
        #  clinical count worth a badge: an unread result is the failure with
        #  consequences, and it should be visible without going looking.
        "unfiled_docs": cache.get_or_set(
            "nav:unfiled",
            lambda: db.query(documents.Document).filter(
                documents.Document.client_id.is_(None)).count()),
        "unreviewed": cache.get_or_set(
            "nav:unreviewed",
            lambda: db.query(ehr.Order).filter(
                ehr.Order.status == ehr.OrderStatus.resulted,
                ehr.Order.reviewed_at.is_(None)).count()),
        #  Patients with an unread message. Counted as distinct clients, not
        #  raw message rows, so a patient who sent three messages in a row
        #  shows as one badge, not three - the number a staff member actually
        #  wants is "how many people are waiting on me".
        "unread_messages": cache.get_or_set(
            "nav:unread_messages",
            lambda: db.query(portal.PatientMessage.client_id)
                      .filter_by(sender_role="patient", read_by_staff_at=None)
                      .distinct().count()),
        # The Connections screen resolves each credential through the database,
        # so the template needs the session it was rendered with.
        "db": db,
    }
    base.update(extra)
    return base


# ---------------------------------------------------------------- staff screens

@app.get("/", response_class=HTMLResponse)
def home(request: Request, db: Session = Depends(get_db)):
    #  A clinician's home is their caseload, not the practice's activity feed.
    #  The dashboard below answers "what is happening across the practice",
    #  which is an administrator's question; a doctor signing in wants to know
    #  who is waiting on them. Same app, different first screen.
    me = auth.current_user(request, db)
    if me and me.role == UserRole.practitioner:
        return RedirectResponse("/my-patients", status_code=303)

    received = (db.query(Submission)
                .filter(Submission.status.in_([SubmissionStatus.submitted,
                                               SubmissionStatus.reviewed]))
                .order_by(Submission.submitted_at.desc()).limit(5).all())
    pending = (db.query(Submission).filter_by(status=SubmissionStatus.sent)
               .order_by(Submission.sent_at.desc()).limit(5).all())
    events = db.query(AuditEvent).order_by(AuditEvent.at.desc()).limit(10).all()

    total_patients = db.query(Client).filter_by(archived=False).count()

    today = date.today()
    todays_appts = (db.query(schedule.Appointment)
                    .filter(schedule.Appointment.on_day == today)
                    .options(joinedload(schedule.Appointment.client),
                             joinedload(schedule.Appointment.provider))
                    .order_by(schedule.Appointment.at_time).all())

    ratings = [r for (r,) in db.query(ExperienceSurvey.rating_overall)
              .filter(ExperienceSurvey.rating_overall.isnot(None)).all()]
    avg_experience = round(sum(ratings) / len(ratings), 1) if ratings else None

    #  Forms sent per day, this week. Bucketed in Python rather than with a
    #  database date-trunc function - the row count here is never more than a
    #  practice's weekly intake volume, and staying off a Postgres-specific
    #  function keeps this query portable.
    since = datetime.utcnow() - timedelta(days=6)
    by_day: dict[date, int] = {}
    for (sent_at,) in db.query(Submission.sent_at).filter(Submission.sent_at >= since).all():
        if sent_at:
            d = sent_at.date()
            by_day[d] = by_day.get(d, 0) + 1
    week = [((datetime.utcnow() - timedelta(days=i)).date(),
            by_day.get((datetime.utcnow() - timedelta(days=i)).date(), 0))
           for i in range(6, -1, -1)]
    week_peak = max((n for _, n in week), default=0) or 1

    #  The screening donut, same "one active trial or say so" rule
    #  app/records.py's _screen() already uses for the spreadsheet column.
    active_trials = db.query(Trial).filter_by(is_active=True).order_by(Trial.name).all()
    screening = None
    if len(active_trials) == 1:
        trial = active_trials[0]
        clients = (db.query(Client).filter_by(archived=False)
                   .options(selectinload(Client.medications),
                            selectinload(Client.submissions_list)
                            .selectinload(Submission.answers)
                            .joinedload(Answer.question))
                   .all())
        archives = records_for_many(db, (c.hospital_id for c in clients))
        counts = {"Looks eligible": 0, "Needs review": 0, "Not eligible": 0}
        for c in clients:
            v = trials.evaluate(c, trial, archives.get(c.hospital_id or 0, [])).verdict
            if v in counts:
                counts[v] += 1
        total_screened = sum(counts.values())
        colours = {"Looks eligible": "var(--good)", "Needs review": "var(--warn)",
                  "Not eligible": "var(--bad)"}
        stops, acc = [], 0.0
        for label, n in counts.items():
            if not n:
                continue
            pct = n / total_screened * 100
            stops.append(f"{colours[label]} {acc:.2f}% {acc + pct:.2f}%")
            acc += pct
        gradient = ("conic-gradient(" + ", ".join(stops) + ")") if stops else "conic-gradient(var(--rule) 0% 100%)"
        screening = {"trial": trial, "counts": counts, "total": total_screened, "gradient": gradient}

    return render(
        "dashboard.html",
        ctx(request, db, received=received, pending=pending, events=events, nav="home",
            total_patients=total_patients, todays_appts=todays_appts,
            avg_experience=avg_experience, week=week, week_peak=week_peak,
            active_trials=active_trials, screening=screening),
    )


@app.get("/setup", response_class=HTMLResponse)
def setup(request: Request):
    """First-run account creation. Closes itself once an account exists."""
    if auth.system_claimed():
        return RedirectResponse("/login", status_code=303)
    return render("setup.html", {"request": request, "practice": config.load(),
                                 "error": "", "name": "", "email": ""})


@app.post("/setup")
def do_setup(request: Request, name: str = F(""), email: str = F(""),
             password: str = F(""), confirm: str = F("")):
    if auth.system_claimed():
        return RedirectResponse("/login", status_code=303)

    error = ""
    if not name.strip():
        error = "Enter your name."
    elif "@" not in email or "." not in email.split("@")[-1]:
        error = "Enter a valid email address."
    elif len(password) < auth.MIN_PASSWORD_LENGTH:
        error = f"Password must be at least {auth.MIN_PASSWORD_LENGTH} characters."
    elif password != confirm:
        error = "The two passwords do not match."

    if error:
        return render("setup.html", {"request": request, "practice": config.load(),
                                     "error": error, "name": name, "email": email})

    user = auth.create_first_owner(name, email, password)
    auth.sign_in(request, user)
    return RedirectResponse("/", status_code=303)


@app.get("/login", response_class=HTMLResponse)
def login(request: Request, next: str = "/", timeout: int = 0, reset: int = 0,
          db: Session = Depends(get_db)):
    return render("login.html", {
        "request": request, "practice": config.load(), "next": next,
        "error": "Your session timed out. Please sign in again." if timeout else "",
        "notice": "Password changed. Sign in with your new password." if reset else "",
    })


@app.post("/login")
def do_login(request: Request, email: str = F(""), password: str = F(""),
             next: str = F("/"), db: Session = Depends(get_db)):
    def fail(message: str):
        return render("login.html", {"request": request, "practice": config.load(),
                                     "next": next, "error": message, "email": email,
                                     "notice": ""})

    remaining = auth.is_locked(email)
    if remaining:
        mins = max(1, int(remaining.total_seconds() // 60))
        log(db, "Login Blocked (locked out)", "user", email, ip=client_ip(request))
        db.commit()
        return fail(f"Too many failed attempts. Try again in {mins} minute(s).")

    user = db.query(User).filter(User.email == email.strip().lower()).first()
    if not user or not user.is_active or not auth.verify_password(password, user.password_hash):
        auth.record_failure(email)
        # Deliberately identical message for "no such user" and "wrong password",
        # so this cannot be used to enumerate who has an account.
        log(db, "Login Failed", "user", email, ip=client_ip(request))
        db.commit()
        return fail("Email or password is incorrect.")

    auth.clear_failures(email)

    if user.mfa_enabled:
        # The password is proven but the session is not signed in - see
        # begin_mfa_challenge's docstring for why that distinction is the
        # entire point. "next" travels in the query string of the redirect
        # rather than the session, so a bookmarked deep link still lands where
        # it was headed once the second factor clears.
        auth.begin_mfa_challenge(request, user)
        log(db, "Password verified, awaiting MFA code", "user", user.id,
            user_id=user.id, ip=client_ip(request))
        db.commit()
        target = next if next.startswith("/") and not next.startswith("//") else "/"
        return RedirectResponse(f"/login/verify?{urlencode({'next': target})}",
                                status_code=303)

    auth.sign_in(request, user)
    user.last_login = datetime.utcnow()
    log(db, "Staff Logged In", "user", user.id, user_id=user.id, ip=client_ip(request))
    db.commit()
    target = next if next.startswith("/") and not next.startswith("//") else "/"
    return RedirectResponse(target, status_code=303)


@app.get("/login/verify", response_class=HTMLResponse)
def login_verify(request: Request, next: str = "/", db: Session = Depends(get_db)):
    user = auth.pending_mfa_user(request, db)
    if not user:
        # No pending challenge, or it expired. Back to the start rather than a
        # bare error - the password already worked once, typing it again is a
        # small cost next to a dead end explaining a five-minute window nobody
        # was watching the clock for.
        return RedirectResponse("/login", status_code=303)
    return render("login_verify.html", {
        "request": request, "practice": config.load(), "next": next,
        "error": "", "email": user.email})


@app.post("/login/verify")
def login_verify_submit(request: Request, code: str = F(""),
                        backup_code: str = F(""), next: str = F("/"),
                        db: Session = Depends(get_db)):
    user = auth.pending_mfa_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)

    def fail(message: str):
        return render("login_verify.html", {
            "request": request, "practice": config.load(), "next": next,
            "error": message, "email": user.email})

    # The same lockout the password step uses, keyed the same way. A code is
    # six digits - roughly a million possibilities - and without a limit here
    # an attacker who has already learned or guessed a password could sit at
    # this screen and brute-force the rest.
    remaining = auth.is_locked(user.email)
    if remaining:
        mins = max(1, int(remaining.total_seconds() // 60))
        log(db, "MFA Verify Blocked (locked out)", "user", user.id,
            user_id=user.id, ip=client_ip(request))
        db.commit()
        return fail(f"Too many failed attempts. Try again in {mins} minute(s).")

    ok = False
    if backup_code.strip():
        ok = mfa.verify_backup_code(db, user, backup_code)
        if ok:
            left = mfa.remaining_backup_codes(db, user)
            log(db, f"Signed in with a backup code ({left} left)", "user",
                user.id, user_id=user.id, ip=client_ip(request))
    elif code.strip():
        ok = mfa.verify_code(user, code)

    if not ok:
        auth.record_failure(user.email)
        log(db, "MFA Verify Failed", "user", user.id, user_id=user.id,
            ip=client_ip(request))
        db.commit()
        return fail("That code is incorrect or has expired.")

    auth.clear_failures(user.email)
    auth.sign_in(request, user)
    user.last_login = datetime.utcnow()
    log(db, "Staff Logged In (MFA)", "user", user.id, user_id=user.id,
        ip=client_ip(request))
    db.commit()
    target = next if next.startswith("/") and not next.startswith("//") else "/"
    return RedirectResponse(target, status_code=303)


@app.get("/logout")
def logout(request: Request, db: Session = Depends(get_db)):
    user = auth.current_user(request, db)
    if user:
        log(db, "Staff Logged Out", "user", user.id, user_id=user.id)
        db.commit()
    auth.sign_out(request)
    return RedirectResponse("/login", status_code=303)


@app.get("/users", response_class=HTMLResponse)
def users_screen(request: Request, db: Session = Depends(get_db),
                 _=Depends(needs(perms.USERS_MANAGE))):
    # A generated password is shown exactly once, then dropped from the session.
    flash = request.session.pop("flash", None)
    return render("users.html", ctx(
        request, db, nav="users", flash=flash,
        staff=db.query(User).order_by(User.is_active.desc(), User.id).all(),
        requests=db.query(AccessRequest).filter_by(status="pending")
                   .order_by(AccessRequest.created_at).all(),
        awaiting=db.query(User).filter(User.role == UserRole.pending,
                                       User.is_active.is_(True))
                   .order_by(User.id).all(),
        roles=list(UserRole),
        error=request.session.pop("user_error", ""),
        note=request.session.pop("user_note", ""),
    ))


@app.post("/users/new")
def user_new(request: Request, email: str = F(""), name: str = F(""),
             role: str = F("front_desk"), db: Session = Depends(get_db),
             actor: User = Depends(needs(perms.USERS_MANAGE))):
    email = um.normalise_email(email)
    try:
        new_role = UserRole(role)
        um.check_create(actor, new_role)
        if not email or "@" not in email:
            raise um.Refused("Enter a valid email address.")
        if db.query(User).filter(User.email == email).first():
            raise um.Refused("An account with that email already exists.")
        if not name.strip():
            raise um.Refused("Enter the person's name.")
    except (um.Refused, ValueError) as exc:
        request.session["user_error"] = str(exc)
        return RedirectResponse("/users", status_code=303)

    password = um.temp_password()
    user = User(email=email, name=name.strip(), role=new_role,
                password_hash=auth.hash_password(password),
                must_change_password=True)
    db.add(user)
    db.flush()
    log(db, "Staff Account Created", "user", user.id, user_id=actor.id)
    db.commit()
    request.session["flash"] = {
        "title": f"Account created for {user.name}",
        "email": user.email, "password": password,
    }
    return RedirectResponse("/users", status_code=303)


@app.post("/users/requests/{req_id}/{decision}")
def handle_request(req_id: int, decision: str, request: Request,
                   role: str = F("front_desk"), db: Session = Depends(get_db),
                   actor: User = Depends(needs(perms.USERS_MANAGE))):
    """Approve turns a request into a real account with a temporary password."""
    req = get_or_404(db, AccessRequest, req_id)
    if req.status != "pending":
        return RedirectResponse("/users", status_code=303)

    if decision != "approve":
        req.status, req.handled_at, req.handled_by = "declined", datetime.utcnow(), actor.id
        log(db, "Access Request Declined", "access_request", req.email, user_id=actor.id)
        db.commit()
        return RedirectResponse("/users", status_code=303)

    try:
        new_role = UserRole(role)
        um.check_create(actor, new_role)
        if db.query(User).filter(User.email == req.email).first():
            raise um.Refused("An account with that email already exists.")
    except (um.Refused, ValueError) as exc:
        request.session["user_error"] = str(exc)
        return RedirectResponse("/users", status_code=303)

    password = um.temp_password()
    user = User(email=req.email, name=req.name, role=new_role,
                password_hash=auth.hash_password(password), must_change_password=True)
    db.add(user)
    req.status, req.handled_at, req.handled_by = "approved", datetime.utcnow(), actor.id
    db.flush()
    log(db, "Access Request Approved", "user", user.id, user_id=actor.id)
    db.commit()
    request.session["flash"] = {"title": f"Account created for {user.name}",
                                "email": user.email, "password": password}
    return RedirectResponse("/users", status_code=303)


@app.post("/users/{user_id}/role")
def user_role(user_id: int, request: Request, role: str = F(""),
              db: Session = Depends(get_db),
              actor: User = Depends(needs(perms.USERS_MANAGE))):
    target = get_or_404(db, User, user_id)
    try:
        new_role = UserRole(role)
        um.check_role_change(db, actor, target, new_role)
    except (um.Refused, ValueError) as exc:
        request.session["user_error"] = str(exc)
        return RedirectResponse("/users", status_code=303)

    was = target.role.value
    target.role = new_role
    log(db, f"Role Changed ({was} to {new_role.value})", "user", target.id, user_id=actor.id)
    db.commit()
    return RedirectResponse("/users", status_code=303)


@app.post("/users/{user_id}/active")
def user_active(user_id: int, request: Request, db: Session = Depends(get_db),
                actor: User = Depends(needs(perms.USERS_MANAGE))):
    target = get_or_404(db, User, user_id)
    if target.is_active:
        try:
            um.check_deactivate(db, actor, target)
        except um.Refused as exc:
            request.session["user_error"] = str(exc)
            return RedirectResponse("/users", status_code=303)
        target.is_active = False
        log(db, "Staff Account Deactivated", "user", target.id, user_id=actor.id)
    else:
        target.is_active = True
        log(db, "Staff Account Reactivated", "user", target.id, user_id=actor.id)
    db.commit()
    return RedirectResponse("/users", status_code=303)


@app.post("/users/{user_id}/reset")
def user_reset(user_id: int, request: Request, db: Session = Depends(get_db),
               actor: User = Depends(needs(perms.USERS_MANAGE))):
    target = get_or_404(db, User, user_id)
    try:
        um.check_reset(actor, target)
    except um.Refused as exc:
        request.session["user_error"] = str(exc)
        return RedirectResponse("/users", status_code=303)

    password = um.temp_password()
    target.password_hash = auth.hash_password(password)
    target.must_change_password = True
    auth.clear_failures(target.email)      # a reset also clears a lockout
    log(db, "Password Reset By Admin", "user", target.id, user_id=actor.id)
    db.commit()
    request.session["flash"] = {
        "title": f"New temporary password for {target.name}",
        "email": target.email, "password": password,
    }
    return RedirectResponse("/users", status_code=303)


@app.post("/users/{user_id}/mfa-reset")
def user_mfa_reset(user_id: int, request: Request, db: Session = Depends(get_db),
                   actor: User = Depends(needs(perms.USERS_MANAGE))):
    """Turn a staff member's MFA off from the admin side, for a lost device.

    This is a real reduction in that account's security - anyone who then
    signs in with just the password is in, with no second factor until they
    re-enroll - so it is logged as an administrative act with the admin's own
    id, the same way an admin-issued password reset is, rather than folded
    into the ordinary self-service disable flow that logs against the account
    holder.
    """
    target = get_or_404(db, User, user_id)
    if not target.mfa_enabled:
        return RedirectResponse("/users", status_code=303)
    mfa.disable(db, target, by_admin=True)
    log(db, f"MFA reset by {actor.name}", "user", target.id, user_id=actor.id)
    db.commit()
    request.session["user_note"] = (
        f"Two-factor authentication turned off for {target.name}. "
        f"They can set it up again from their Account page.")
    return RedirectResponse("/users", status_code=303)


@app.get("/permissions", response_class=HTMLResponse)
def permissions_screen(request: Request, db: Session = Depends(get_db)):
    """Visible to every signed-in user. Knowing the policy is not a privilege -
    people need to see why a button is missing before they ask for it."""
    return render("permissions.html", ctx(
        request, db, nav="permissions",
        rows=perms.matrix(),
        roles=[r.value for r in perms.ROLE_PERMISSIONS],
        staff=db.query(User).order_by(User.id).all()
        if perms.can(auth.current_user(request, db), perms.USERS_MANAGE) else [],
    ))


@app.get("/pending", response_class=HTMLResponse)
def pending_screen(request: Request, db: Session = Depends(get_db)):
    """Where a self-registered account lands until somebody gives it a role."""
    return render("pending.html", ctx(request, db, nav=""))


@app.get("/account", response_class=HTMLResponse)
def account(request: Request, mustchange: int = 0, db: Session = Depends(get_db)):
    return render("account.html", ctx(request, db, nav="account", message="", error="",
                                      mustchange=bool(mustchange)))


@app.post("/account/password")
def change_password(request: Request, current: str = F(""), new: str = F(""),
                    confirm: str = F(""), db: Session = Depends(get_db)):
    user = auth.current_user(request, db)
    error = message = ""
    if not auth.verify_password(current, user.password_hash):
        error = "Your current password is incorrect."
    elif len(new) < auth.MIN_PASSWORD_LENGTH:
        error = f"New password must be at least {auth.MIN_PASSWORD_LENGTH} characters."
    elif new != confirm:
        error = "The two new passwords do not match."
    else:
        user.password_hash = auth.hash_password(new)
        user.must_change_password = False
        log(db, "Password Changed", "user", user.id, user_id=user.id)
        db.commit()
        message = "Password changed."
    return render("account.html", ctx(request, db, nav="account", message=message,
                                      error=error, mustchange=user.must_change_password))


@app.get("/account/mfa/setup", response_class=HTMLResponse)
def mfa_setup(request: Request, db: Session = Depends(get_db)):
    """Start enrollment: generate a secret, show it once, wait for a code back.

    Re-running this while already enrolled starts over with a fresh secret -
    the old one stops working the moment a new one is stored, which is the
    correct behaviour for "I want to switch to a different phone" and for
    "the QR code did not scan the first time" alike.
    """
    user = auth.current_user(request, db)
    secret = mfa.begin_enrollment(db, user)
    db.commit()
    uri = mfa.provisioning_uri(secret, email=user.email,
                               issuer=config.load().get("name", "Meridian"))
    return render("mfa_setup.html", ctx(
        request, db, nav="account", secret=secret, uri=uri, error=""))


@app.post("/account/mfa/confirm")
def mfa_confirm(request: Request, code: str = F(""),
                db: Session = Depends(get_db)):
    user = auth.current_user(request, db)
    codes = mfa.confirm_enrollment(db, user, code)
    if codes is None:
        # confirm_enrollment writes nothing when the code fails to verify -
        # the stored secret from setup is untouched, so the retry below shows
        # the same manual-entry key rather than a fresh one.
        secret = mfa.pending_secret(user)
        uri = (mfa.provisioning_uri(secret, email=user.email,
                                    issuer=config.load().get("name", "Meridian"))
              if secret else "")
        return render("mfa_setup.html", ctx(
            request, db, nav="account", secret=secret, uri=uri,
            error="That code did not verify. Check the time on your phone "
                 "matches your computer's, and try the newest code shown."))
    log(db, "MFA turned on", "user", user.id, user_id=user.id, ip=client_ip(request))
    db.commit()
    return render("mfa_backup_codes.html", ctx(
        request, db, nav="account", codes=codes, first_time=True))


@app.post("/account/mfa/disable")
def mfa_disable(request: Request, current: str = F(""),
                db: Session = Depends(get_db)):
    """Turn MFA off. Requires the current password, same bar as changing one.

    A stolen, already-signed-in laptop is exactly the situation this password
    re-check defends: the session cookie alone is not enough to remove the
    second factor protecting it.
    """
    user = auth.current_user(request, db)
    if not auth.verify_password(current, user.password_hash):
        return render("account.html", ctx(
            request, db, nav="account", message="",
            error="Your current password is incorrect.",
            mustchange=user.must_change_password))
    mfa.disable(db, user)
    db.commit()
    return render("account.html", ctx(
        request, db, nav="account", message="Two-factor authentication is now off.",
        error="", mustchange=user.must_change_password))


@app.post("/account/mfa/backup-codes")
def mfa_new_backup_codes(request: Request, current: str = F(""),
                        db: Session = Depends(get_db)):
    user = auth.current_user(request, db)
    if not auth.verify_password(current, user.password_hash):
        return render("account.html", ctx(
            request, db, nav="account", message="",
            error="Your current password is incorrect.",
            mustchange=user.must_change_password))
    codes = mfa.regenerate_backup_codes(db, user)
    log(db, "MFA backup codes regenerated", "user", user.id, user_id=user.id,
        ip=client_ip(request))
    db.commit()
    return render("mfa_backup_codes.html", ctx(
        request, db, nav="account", codes=codes, first_time=False))


@app.get("/forms", response_class=HTMLResponse)
def forms_list(request: Request, archived: int = 0, db: Session = Depends(get_db),
               _=Depends(needs(perms.FORMS_VIEW))):
    """The form library. Archived forms are hidden, not gone.

    A form with submissions attached can never simply be deleted - the answers
    hang off it, and a records system that loses answers when somebody tidies the
    form list is not a records system. Archiving takes it out of the way and
    leaves every submission readable.
    """
    q = db.query(Form).filter(Form.folder_id.is_(None))
    shown = q.filter(Form.is_active.is_(True) if not archived
                     else Form.is_active.is_(False)).order_by(Form.id).all()
    n_archived = q.filter(Form.is_active.is_(False)).count()

    # Two sections, as IntakeQ has them: questionnaires go to patients, note
    # templates stay with the clinician. Split here rather than in the template
    # so the counts and the empty states can differ.
    return render("forms_list.html", ctx(
        request, db, nav="forms",
        forms=[f for f in shown if f.kind != "note_template"],
        notes=[f for f in shown if f.kind == "note_template"],
        folders=db.query(Folder).order_by(Folder.position).all(),
        showing_archived=bool(archived), n_archived=n_archived))


@app.post("/forms/{form_id}/archive")
def form_archive(form_id: int, db: Session = Depends(get_db),
                 _=Depends(needs(perms.FORMS_EDIT))):
    """Hide a form from the library, or bring it back. Never touches answers."""
    form = get_or_404(db, Form, form_id)
    form.is_active = not form.is_active
    log(db, f"Form {'restored' if form.is_active else 'archived'}", "form", form_id)
    db.commit()
    return RedirectResponse("/forms" if form.is_active else "/forms?archived=1",
                            status_code=303)


SPECIALTIES = [
    "Accounting", "Acupuncture", "Acute Care", "Addiction/Rehab", "Allergy",
    "Allergy/Immunology", "Anesthesiology", "Applied Behavior Analysis",
    "Athletic Training", "Audiology", "Cardiology", "Chiropractic", "Coaching",
    "Counseling", "Dentistry", "Dermatology", "Dietetics", "Endocrinology",
    "Family Medicine", "Gastroenterology", "Internal Medicine", "Massage Therapy",
    "Mental Health", "Naturopathic", "Neurology", "Nutrition", "Obstetrics",
    "Occupational Therapy", "Oncology", "Ophthalmology", "Optometry",
    "Orthopedics", "Pain Management", "Pediatrics", "Physical Therapy",
    "Podiatry", "Psychiatry", "Psychology", "Pulmonology", "Rheumatology",
    "Sleep Medicine", "Speech Therapy", "Urgent Care", "Urology", "Other",
]

FORM_TYPES = {
    "Psychiatry": ["New Patient Intake", "Adult Packet", "Minor Packet",
                   "Medication History", "Symptom Screener", "Consent Forms",
                   "Release of Information", "Treatment Completion",
                   "Financial Policy", "Telehealth Consent"],
    "default": ["Client Information", "New Patient Intake", "Medical History",
                "Consent Forms", "Screening Questionnaire", "Release of Information",
                "Financial Policy", "Follow-up Survey", "Service Agreements"],
}

# Starter questions per type. A template, not a model - the screen says so.
STARTERS = {
    "New Patient Intake": [
        ("Please enter patient's information.", QuestionType.address, True),
        ("Please state the patient's presenting problem(s).", QuestionType.long_text, True),
        ("Who referred you to our practice?", QuestionType.short_text, False),
    ],
    "Medication History": [
        ("List all medications you are currently taking, with dose.", QuestionType.long_text, True),
        ("List medications you have stopped in the last 12 months.", QuestionType.long_text, False),
        ("Any medication allergies?", QuestionType.long_text, False),
    ],
    "Symptom Screener": [
        ("Over the last 2 weeks, how often have you been bothered by the following?",
         QuestionType.matrix, True),
    ],
    "Telehealth Consent": [
        ("I consent to receiving care by video or telephone.", QuestionType.attestation, True),
        ("Type your full name to sign.", QuestionType.signature, True),
    ],
}

MATRIX_ROWS = "Depressed mood\nAnxiety\nSleep disturbance\nAppetite change\nConcentration"
MATRIX_OPTS = "Not at all\nSeveral days\nMore than half the days\nNearly every day"


@app.get("/forms/new", response_class=HTMLResponse)
def form_new_choose(request: Request, kind: str = "questionnaire",
                    db: Session = Depends(get_db),
                    _=Depends(needs(perms.FORMS_CREATE))):
    return render("form_new.html", ctx(request, db, nav="forms", kind=kind))


@app.get("/forms/new/blank")
def form_new_blank(kind: str = "questionnaire", db: Session = Depends(get_db),
                   _=Depends(needs(perms.FORMS_CREATE))):
    f = Form(name="Untitled note template" if kind == "note_template"
                  else "Untitled form",
             kind="note_template" if kind == "note_template" else "questionnaire")
    db.add(f)
    db.flush()
    log(db, "Form Created", "form", f.id)
    db.commit()
    return RedirectResponse(f"/forms/{f.id}", status_code=303)


@app.get("/forms/new/guided", response_class=HTMLResponse)
def form_new_guided(request: Request, db: Session = Depends(get_db),
                    _=Depends(needs(perms.FORMS_CREATE))):
    return render("form_specialty.html",
                  ctx(request, db, nav="forms", specialties=SPECIALTIES))


@app.get("/forms/new/guided/type", response_class=HTMLResponse)
def form_new_type(request: Request, specialty: str = "Psychiatry",
                  db: Session = Depends(get_db),
                  _=Depends(needs(perms.FORMS_CREATE))):
    return render("form_type.html", ctx(
        request, db, nav="forms", specialty=specialty,
        types=FORM_TYPES.get(specialty, FORM_TYPES["default"])))


@app.post("/forms/new/guided/create")
def form_new_create(specialty: str = F("Psychiatry"), kind: str = F(""),
                    note: str = F(""), db: Session = Depends(get_db),
                    _=Depends(needs(perms.FORMS_CREATE))):
    name = kind.strip() or note.strip()[:80] or "Untitled form"
    f = Form(name=name, description=f"{specialty}. {note.strip()}".strip())
    db.add(f)
    db.flush()
    starter = STARTERS.get(kind, STARTERS["New Patient Intake"])
    for pos, (text, qtype, required) in enumerate(starter):
        is_matrix = qtype == QuestionType.matrix
        db.add(Question(form_id=f.id, text=text, qtype=qtype, required=required,
                        position=pos, page=1,
                        rows_raw=MATRIX_ROWS if is_matrix else "",
                        options_raw=MATRIX_OPTS if is_matrix else ""))
    log(db, "Form Created (guided)", "form", f.id)
    db.commit()
    return RedirectResponse(f"/forms/{f.id}", status_code=303)


@app.get("/forms/new/upload", response_class=HTMLResponse)
def form_new_upload(request: Request, db: Session = Depends(get_db),
                    _=Depends(needs(perms.FORMS_CREATE))):
    return render("form_upload.html", ctx(request, db, nav="forms"))


@app.post("/forms/{form_id}/duplicate")
def form_duplicate(form_id: int, db: Session = Depends(get_db),
                   _=Depends(needs(perms.FORMS_CREATE))):
    src = get_or_404(db, Form, form_id)
    copy = Form(name=f"{src.name} (copy)", description=src.description,
                colour=src.colour, is_active=False, folder_id=src.folder_id)
    db.add(copy)
    db.flush()
    for q in src.questions:
        db.add(Question(form_id=copy.id, text=q.text, help_text=q.help_text,
                        qtype=q.qtype, required=q.required, position=q.position,
                        page=q.page, options_raw=q.options_raw, rows_raw=q.rows_raw,
                        office_use_only=q.office_use_only))
    for fc in src.consents:
        db.add(FormConsent(form_id=copy.id, consent_id=fc.consent_id))
    log(db, "Form Duplicated", "form", copy.id)
    db.commit()
    return RedirectResponse(f"/forms/{copy.id}", status_code=303)


@app.post("/forms/{form_id}/delete")
def form_delete(form_id: int, db: Session = Depends(get_db),
                _=Depends(needs(perms.FORMS_DELETE))):
    """Deactivate, never destroy. Submissions point at this form, and an audit
    trail that dead-ends is worse than a form nobody uses."""
    f = get_or_404(db, Form, form_id)
    f.is_active = False
    if not f.name.endswith("(deleted)"):
        f.name = f"{f.name} (deleted)"
    log(db, "Form Deleted", "form", form_id)
    db.commit()
    return RedirectResponse("/forms", status_code=303)


@app.get("/forms/{form_id}/url", response_class=HTMLResponse)
def form_url(form_id: int, request: Request, db: Session = Depends(get_db),
             _=Depends(needs(perms.FORMS_VIEW))):
    form = get_or_404(db, Form, form_id)
    return render("form_url.html", ctx(request, db, nav="forms", form=form,
                                       base=str(request.base_url).rstrip("/")))


@app.get("/forms/{form_id}/print", response_class=HTMLResponse)
def form_print(form_id: int, request: Request, db: Session = Depends(get_db),
               _=Depends(needs(perms.FORMS_VIEW))):
    form = get_or_404(db, Form, form_id)
    return render("form_print.html", {"request": request, "practice": config.load(),
                                      "form": form})


@app.get("/forms/{form_id}/settings", response_class=HTMLResponse)
def form_settings(form_id: int, request: Request, db: Session = Depends(get_db),
                  _=Depends(needs(perms.FORMS_VIEW))):
    form = get_or_404(db, Form, form_id)
    colours = [("#9aa5b1", "Grey"), ("#3d9ad1", "Blue"), ("#7cb342", "Green"),
               ("#e0605a", "Red"), ("#8e6bb5", "Purple"), ("#e8b93d", "Yellow")]
    return render("form_settings.html",
                  ctx(request, db, nav="forms", form=form, colours=colours))


@app.post("/forms/{form_id}/settings")
def save_form_settings(form_id: int, name: str = F(""), colour: str = F("#9aa5b1"),
                       active: str = F(""), db: Session = Depends(get_db),
                       _=Depends(needs(perms.FORMS_EDIT))):
    form = get_or_404(db, Form, form_id)
    if name.strip():
        form.name = name.strip()
    form.colour = colour
    form.is_active = bool(active)
    log(db, "Form Settings Changed", "form", form_id)
    db.commit()
    return RedirectResponse(f"/forms/{form_id}", status_code=303)


@app.get("/forms/{form_id}/rules", response_class=HTMLResponse)
def form_rules(form_id: int, request: Request, db: Session = Depends(get_db),
               _=Depends(needs(perms.FORMS_VIEW))):
    form = get_or_404(db, Form, form_id)
    return render("form_rules.html", ctx(request, db, nav="forms", form=form))


@app.post("/folders/new")
def folder_new(name: str = F("New folder"), db: Session = Depends(get_db),
               _=Depends(needs(perms.FORMS_CREATE))):
    db.add(Folder(name=name, position=db.query(Folder).count()))
    db.commit()
    return RedirectResponse("/forms", status_code=303)


@app.get("/forms/{form_id}", response_class=HTMLResponse)
def form_editor(form_id: int, request: Request, q: int | None = None,
                db: Session = Depends(get_db),
                _=Depends(needs(perms.FORMS_VIEW))):
    form = get_or_404(db, Form, form_id)
    attached = {fc.consent_id for fc in form.consents}
    consents = db.query(ConsentForm).order_by(ConsentForm.name).all()
    selected = db.get(Question, q) if q else None
    if selected and selected.form_id != form.id:
        selected = None            # never edit one form's question from another
    return render(
        "form_editor.html",
        ctx(request, db, form=form, consents=consents, attached=attached,
            qtypes=list(QuestionType), selected=selected, nav="forms"),
    )


@app.post("/questions/{qid}/text")
def question_text(qid: int, text: str = F(""), db: Session = Depends(get_db),
                  _=Depends(needs(perms.FORMS_EDIT))):
    question = get_or_404(db, Question, qid)
    if text.strip():
        question.text = text.strip()
        log(db, "Question Edited", "form", question.form_id)
        db.commit()
    return RedirectResponse(f"/forms/{question.form_id}?q={qid}", status_code=303)


@app.post("/questions/{qid}/type")
def question_type(qid: int, qtype: str = F("short_text"),
                  db: Session = Depends(get_db),
                  _=Depends(needs(perms.FORMS_EDIT))):
    question = get_or_404(db, Question, qid)
    try:
        question.qtype = QuestionType(qtype)
        log(db, "Question Type Changed", "form", question.form_id)
        db.commit()
    except ValueError:
        pass
    return RedirectResponse(f"/forms/{question.form_id}?q={qid}", status_code=303)


@app.post("/questions/{qid}/options")
def question_options(qid: int, options: str = F(""),
                     db: Session = Depends(get_db),
                     _=Depends(needs(perms.FORMS_EDIT))):
    question = get_or_404(db, Question, qid)
    question.options_raw = options
    log(db, "Question Options Changed", "form", question.form_id)
    db.commit()
    return RedirectResponse(f"/forms/{question.form_id}?q={qid}", status_code=303)


@app.post("/questions/{qid}/matrix")
def question_matrix(qid: int, rows: str = F(""), options: str = F(""),
                    db: Session = Depends(get_db),
                    _=Depends(needs(perms.FORMS_EDIT))):
    question = get_or_404(db, Question, qid)
    question.rows_raw, question.options_raw = rows, options
    log(db, "Matrix Changed", "form", question.form_id)
    db.commit()
    return RedirectResponse(f"/forms/{question.form_id}?q={qid}", status_code=303)


@app.post("/questions/{qid}/items/new")
def item_new(qid: int, label: str = F(""), kind: str = F("text"),
             options: str = F(""), db: Session = Depends(get_db),
             _=Depends(needs(perms.FORMS_EDIT))):
    question = get_or_404(db, Question, qid)
    if label.strip():
        db.add(QuestionItem(question_id=question.id, label=label.strip(),
                            kind=ItemKind(kind), options_raw=options,
                            position=len(question.items)))
        log(db, "Item Added", "form", question.form_id)
        db.commit()
    return RedirectResponse(f"/forms/{question.form_id}?q={qid}", status_code=303)


@app.post("/questions/{qid}/duplicate")
def question_duplicate(qid: int, db: Session = Depends(get_db),
                       _=Depends(needs(perms.FORMS_EDIT))):
    src = get_or_404(db, Question, qid)
    copy = Question(form_id=src.form_id, text=f"{src.text} (copy)",
                    help_text=src.help_text, qtype=src.qtype, required=src.required,
                    position=src.position + 1, page=src.page,
                    options_raw=src.options_raw, rows_raw=src.rows_raw)
    db.add(copy)
    db.flush()
    for it in src.items:
        db.add(QuestionItem(question_id=copy.id, label=it.label, kind=it.kind,
                            options_raw=it.options_raw, position=it.position,
                            width=it.width))
    log(db, "Question Duplicated", "form", src.form_id)
    db.commit()
    return RedirectResponse(f"/forms/{src.form_id}?q={copy.id}", status_code=303)


@app.post("/forms/{form_id}/questions/new")
def question_new(form_id: int, text: str = F("New question"),
                 qtype: str = F("short_text"), page: int = F(1),
                 required: str = F(""), options: str = F(""), rows: str = F(""),
                 db: Session = Depends(get_db),
                 _=Depends(needs(perms.FORMS_EDIT))):
    pos = db.query(Question).filter_by(form_id=form_id).count()
    db.add(Question(form_id=form_id, text=text, qtype=QuestionType(qtype), page=page,
                    required=bool(required), position=pos, options_raw=options,
                    rows_raw=rows))
    log(db, "Question Added", "form", form_id)
    db.commit()
    return RedirectResponse(f"/forms/{form_id}", status_code=303)


@app.post("/questions/{qid}/move")
def question_move(qid: int, direction: str = F("up"), db: Session = Depends(get_db),
                  _=Depends(needs(perms.FORMS_EDIT))):
    q = get_or_404(db, Question, qid)
    siblings = db.query(Question).filter_by(form_id=q.form_id).order_by(Question.position).all()
    i = siblings.index(q)
    j = i - 1 if direction == "up" else i + 1
    if 0 <= j < len(siblings):
        siblings[i].position, siblings[j].position = siblings[j].position, siblings[i].position
        db.commit()
    return RedirectResponse(f"/forms/{q.form_id}", status_code=303)


@app.post("/questions/{qid}/delete")
def question_delete(qid: int, db: Session = Depends(get_db),
                    _=Depends(needs(perms.FORMS_DELETE))):
    q = get_or_404(db, Question, qid)
    form_id = q.form_id
    db.delete(q)
    log(db, "Question Deleted", "form", form_id)
    db.commit()
    return RedirectResponse(f"/forms/{form_id}", status_code=303)


@app.post("/forms/{form_id}/consents/{consent_id}/toggle")
def consent_toggle(form_id: int, consent_id: int, db: Session = Depends(get_db),
                   _=Depends(needs(perms.FORMS_EDIT))):
    existing = db.query(FormConsent).filter_by(form_id=form_id, consent_id=consent_id).first()
    if existing:
        db.delete(existing)
    else:
        db.add(FormConsent(form_id=form_id, consent_id=consent_id))
    log(db, "Consent Forms Changed", "form", form_id)
    db.commit()
    return RedirectResponse(f"/forms/{form_id}?tab=consents", status_code=303)


@app.get("/consents", response_class=HTMLResponse)
def consents_list(request: Request, db: Session = Depends(get_db),
                  _=Depends(needs(perms.FORMS_VIEW))):
    """Every consent document, and whether it says anything.

    The count of documents with no wording is the number on this screen that
    matters. Until it is zero the practice is attaching titles to forms and
    calling the result consent.
    """
    rows = db.query(ConsentForm).order_by(ConsentForm.name).all()
    signed = dict(db.query(ConsentSignature.consent_id,
                           func.count(ConsentSignature.id))
                  .group_by(ConsentSignature.consent_id).all())
    used = dict(db.query(FormConsent.consent_id, func.count(FormConsent.form_id))
                .group_by(FormConsent.consent_id).all())
    return render("consents.html", ctx(
        request, db, nav="consents", consents=rows, signed=signed, used=used,
        empty=sum(1 for c in rows if not (c.body or "").strip()),
        note=request.session.pop("consent_note", "")))


@app.get("/consents/{consent_id}", response_class=HTMLResponse)
def consent_edit(consent_id: int, request: Request, db: Session = Depends(get_db),
                 _=Depends(needs(perms.FORMS_VIEW))):
    consent = get_or_404(db, ConsentForm, consent_id)
    return render("consent_edit.html", ctx(
        request, db, nav="consents", consent=consent,
        signed=db.query(ConsentSignature).filter_by(consent_id=consent_id).count(),
        used=[fc.form for fc in
              db.query(FormConsent).filter_by(consent_id=consent_id).all()]))


@app.post("/consents/{consent_id}")
def consent_save(consent_id: int, request: Request, name: str = F(""),
                 body: str = F(""), db: Session = Depends(get_db),
                 _=Depends(needs(perms.FORMS_EDIT))):
    """Save a consent's wording.

    Editing a document that has already been signed is allowed and does not
    rewrite anything: each signature stored the hash of the text as it stood at
    that moment, so an old signature keeps pointing at the old wording whatever
    happens here. That is the whole reason the hash is on the signature rather
    than computed from the document on demand.
    """
    consent = get_or_404(db, ConsentForm, consent_id)
    if name.strip():
        consent.name = name.strip()[:255]
    was_empty = not (consent.body or "").strip()
    consent.body = body

    signed = db.query(ConsentSignature).filter_by(consent_id=consent_id).count()
    log(db, f"Consent wording {'written' if was_empty else 'edited'}: "
            f"{consent.name}" + (f" ({signed} existing signatures keep the "
                                 f"wording they signed)" if signed else ""),
        "consent", consent_id)
    db.commit()
    request.session["consent_note"] = (
        f"Saved. {consent.name} " + ("can now be signed."
                                     if (consent.body or "").strip()
                                     else "still has no wording, so it stays hidden."))
    return RedirectResponse("/consents", status_code=303)


@app.post("/consents/new")
def consent_new(request: Request, name: str = F(""),
                db: Session = Depends(get_db),
                _=Depends(needs(perms.FORMS_EDIT))):
    if not name.strip():
        return RedirectResponse("/consents", status_code=303)
    consent = ConsentForm(name=name.strip()[:255], body="")
    db.add(consent)
    db.flush()
    log(db, f"Consent document created: {consent.name}", "consent", consent.id)
    db.commit()
    return RedirectResponse(f"/consents/{consent.id}", status_code=303)


@app.get("/clients", response_class=HTMLResponse)
def clients(request: Request, db: Session = Depends(get_db),
            _=Depends(needs(perms.CLIENTS_VIEW))):
    rows = db.query(Client).filter_by(archived=False).order_by(Client.last_name).all()
    log(db, "Client List Viewed", "client")
    db.commit()
    return render("clients.html", ctx(
        request, db, clients=rows, nav="clients",
        note=request.session.pop("caseload_note", ""),
        error=request.session.pop("client_error", "")))


@app.post("/clients/new")
def client_new(request: Request, first_name: str = F(""), last_name: str = F(""),
               email: str = F(""), phone: str = F(""), dob: str = F(""),
               hospital_id: str = F(""),
               city: str = F(""), state: str = F(""), postal_code: str = F(""),
               allow_email: str = F(""), allow_sms: str = F(""),
               db: Session = Depends(get_db),
               _=Depends(needs(perms.CLIENTS_EDIT))):
    """Add a patient. Consent to be contacted defaults to off on purpose -
    absent consent is not implied consent."""
    if not (first_name.strip() or last_name.strip()):
        request.session["client_error"] = "Enter at least a first or last name."
        return RedirectResponse("/clients", status_code=303)

    # Refuse an obvious duplicate. Two rows for one person is worse than a
    # rejected form: their answers land on one record, their chart on the other,
    # and the export shows them twice with half the data each.
    existing = _same_person(db, first_name, last_name, email, dob)
    if existing:
        request.session["client_error"] = (
            f"{existing.name} is already on the list as RD-{existing.id:04d}"
            + (f" ({existing.email})" if existing.email else "")
            + ". Open their chart to send a form or change their details, or "
              "use a different name or email if this really is somebody else.")
        return RedirectResponse("/clients", status_code=303)

    born = None
    if dob:
        try:
            born = datetime.strptime(dob, "%Y-%m-%d").date()
        except ValueError:
            born = None

    client = Client(first_name=first_name.strip(), last_name=last_name.strip(),
                    email=email.strip().lower(), phone=phone.strip(), dob=born,
                    hospital_id=_hospital_id(hospital_id),
                    city=city.strip(), state=state.strip().upper()[:2],
                    postal_code=postal_code.strip(),
                    allow_email=bool(allow_email), allow_sms=bool(allow_sms),
                    contact_preference="email" if allow_email else
                                       ("sms" if allow_sms else "none"))
    db.add(client)
    db.flush()
    log(db, "Client Created", "client", client.id)
    db.commit()
    return RedirectResponse(f"/clients/{client.id}", status_code=303)


def _same_person(db: Session, first: str, last: str, email: str,
                 dob: str) -> Client | None:
    """An existing patient this new one is probably a second copy of.

    Two signals, either one enough. An email address is meant to be unique to a
    person, and a full name with a matching date of birth is how every registry
    in the world identifies somebody. Neither is proof - families share inboxes,
    and names repeat - which is why this reports the clash and lets a human
    decide, instead of merging anything by itself.
    """
    live = db.query(Client).filter_by(archived=False)

    address = (email or "").strip().lower()
    if address:
        hit = live.filter(Client.email == address).first()
        if hit:
            return hit

    first, last = first.strip().lower(), last.strip().lower()
    if first and last and dob:
        for c in live.all():
            if (c.first_name.strip().lower() == first
                    and c.last_name.strip().lower() == last
                    and c.dob and c.dob.isoformat() == dob.strip()):
                return c
    return None


def _hospital_id(raw: str) -> int | None:
    """The hospital's patient number, or nothing.

    Anything that is not a plain number is discarded rather than stored as
    typed. A number that is wrong in a way the app cannot see is worse than a
    number that is absent: it matches a real person in the archive who is not
    this patient.
    """
    raw = (raw or "").strip()
    if not raw.isdigit():
        return None
    value = int(raw)
    return value if 0 < value < 2_000_000_000 else None


@app.post("/clients/{client_id}/hospital-id")
def set_hospital_id(client_id: int, request: Request, hospital_id: str = F(""),
                    db: Session = Depends(get_db),
                    _=Depends(needs(perms.CLIENTS_EDIT))):
    """Link a patient to their record in the hospital archive, or unlink them."""
    client = get_or_404(db, Client, client_id)
    before = client.hospital_id
    client.hospital_id = _hospital_id(hospital_id)

    # The link is saved either way, and then checked. Warning after the fact is
    # deliberate: staff know things the archive does not, so the app's job is to
    # make a questionable link visible, not to refuse it.
    notes = []
    if client.hospital_id:
        notes = matching.check_link(client, records_for(db, client.hospital_id))
    request.session["link_notes"] = notes

    if client.hospital_id != before:
        log(db, f"Hospital ID {'set' if client.hospital_id else 'cleared'}"
               + (" (with warnings)" if notes else ""), "client", client_id)
    db.commit()
    return RedirectResponse(f"/clients/{client_id}#archive", status_code=303)


@app.post("/clients/{client_id}/archive/add")
def archive_add_patient(client_id: int, request: Request,
                        condition: str = F(""), procedure: str = F(""),
                        outcome: str = F("Ongoing"), db: Session = Depends(get_db),
                        _=Depends(needs(perms.CLINICAL_EDIT))):
    """First visit for somebody the hospital has never seen.

    Issues a number from the practice's own range and writes their first visit
    under it. The two happen together on purpose: a number with no visit behind
    it is an identity nobody can check, and this screen exists precisely because
    there was nothing to check against.
    """
    client = get_or_404(db, Client, client_id)
    if client.hospital_id:
        request.session["chart_error"] = (
            f"{client.first_name} already has number {client.hospital_id}. "
            f"Clear it first if it is wrong.")
        return RedirectResponse(f"/clients/{client_id}#archive", status_code=303)
    if not condition.strip():
        request.session["chart_error"] = (
            "What were they seen for? A visit needs a condition - that is the "
            "thing their answers will later be checked against.")
        return RedirectResponse(f"/clients/{client_id}#archive", status_code=303)

    number = identity.next_practice_number(db)
    client.hospital_id = number
    db.add(HospitalRecord(
        patient_id=number,
        name=client.name,
        dob=client.dob,
        age=_age_of(client),
        gender=_sex_of(client),
        condition=condition.strip(),
        procedure=procedure.strip(),
        outcome=outcome.strip(),
        source=ENTERED_HERE,
    ))
    log(db, f"Patient added to the archive as number {number}", "client", client_id)
    db.commit()
    request.session["chart_note"] = (
        f"{client.first_name} is now in the archive as patient number {number}.")
    return RedirectResponse(f"/clients/{client_id}#archive", status_code=303)


@app.get("/clients/{client_id}", response_class=HTMLResponse)
def client_chart(client_id: int, request: Request, db: Session = Depends(get_db),
                 _=Depends(needs(perms.CLIENTS_VIEW))):
    """The facesheet: medications, problems, vitals, allergies, history, forms."""
    client = get_or_404(db, Client, client_id)
    log(db, "Chart Viewed", "client", client_id)

    #  Opening the chart is when a patient's messages count as read by staff -
    #  the mirror of what opening /portal/messages already does on their side.
    thread = (db.query(portal.PatientMessage).filter_by(client_id=client.id)
              .order_by(portal.PatientMessage.id).all())
    now = datetime.utcnow()
    for m in thread:
        if m.from_patient and m.read_by_staff_at is None:
            m.read_by_staff_at = now
    if any(m.from_patient and m.read_by_staff_at == now for m in thread):
        cache.drop("nav:unread_messages")
    db.commit()
    archive = records_for(db, client.hospital_id)
    return render("client_chart.html", ctx(
        request, db, nav="clients", client=client,
        archive=archive, match=matching.compare(client, archive),
        suggestions=identity.candidates(db, client),
        appointments=db.query(schedule.Appointment)
                       .filter_by(client_id=client.id)
                       .order_by(schedule.Appointment.on_day.desc()).all(),
        chart_error=request.session.pop("chart_error", ""),
        chart_note=request.session.pop("chart_note", ""),
        flash=request.session.pop("flash", None),
        message_thread=thread,
        #  Paperwork this patient completed in IntakeQ before they were ever a
        #  row here. Listed beside the forms sent from this app on purpose: to
        #  the person reading the chart they are the same thing, and which
        #  system happened to collect one is not a distinction worth a click.
        encounters=(db.query(ehr.Encounter).filter_by(client_id=client.id)
                    .order_by(ehr.Encounter.seen_on.desc(),
                              ehr.Encounter.id.desc()).all()),
        prescriptions=(db.query(ehr.Prescription).filter_by(client_id=client.id)
                       .order_by(ehr.Prescription.id.desc()).all()),
        orders=(db.query(ehr.Order).filter_by(client_id=client.id)
                .order_by(ehr.Order.id.desc()).all()),
        charges=(db.query(ehr.Charge).filter_by(client_id=client.id)
                 .options(selectinload(ehr.Charge.payments),
                          selectinload(ehr.Charge.claims))
                 .order_by(ehr.Charge.id.desc()).all()),
        payments=(db.query(ehr.Payment).filter_by(client_id=client.id)
                  .order_by(ehr.Payment.id.desc()).all()),
        pay_methods=ehr.METHODS,
        documents=(db.query(documents.Document).filter_by(client_id=client.id)
                   .order_by(documents.Document.uploaded_at.desc()).all()),
        doc_labels=documents.LABELS,
        imported_intakes=(db.query(intakeq.ImportedIntake)
                          .filter_by(client_id=client.id)
                          .order_by(intakeq.ImportedIntake.submitted_at.desc())
                          .all()),
        link_notes=request.session.pop("link_notes", None)
                   or (matching.check_link(client, archive)
                       if client.hospital_id else [])))


@app.post("/clients/{client_id}/medications")
def add_medication(client_id: int, name: str = F(""), dose: str = F(""),
                   form_: str = F("", alias="form"), prescribed: str = F(""),
                   db: Session = Depends(get_db),
                   _=Depends(needs(perms.CLINICAL_EDIT))):
    client = get_or_404(db, Client, client_id)
    if name.strip():
        started = None
        if prescribed:
            try:
                started = datetime.strptime(prescribed, "%Y-%m-%d").date()
            except ValueError:
                started = None
        db.add(Medication(client_id=client.id, name=name.strip(), dose=dose.strip(),
                          form=form_.strip(), prescribed_on=started))
        log(db, "Medication Added", "client", client_id)
        db.commit()
    return RedirectResponse(f"/clients/{client_id}", status_code=303)


@app.post("/clients/{client_id}/medications/{med_id}/stop")
def stop_medication(client_id: int, med_id: int, db: Session = Depends(get_db),
                    _=Depends(needs(perms.CLINICAL_EDIT))):
    med = get_or_404(db, Medication, med_id)
    med.is_active = False
    med.stopped_on = date.today()
    log(db, "Medication Stopped", "client", client_id)
    db.commit()
    return RedirectResponse(f"/clients/{client_id}", status_code=303)


@app.post("/clients/{client_id}/problems")
def add_problem(client_id: int, description: str = F(""), icd10: str = F(""),
                db: Session = Depends(get_db),
                _=Depends(needs(perms.CLINICAL_EDIT))):
    client = get_or_404(db, Client, client_id)
    if description.strip():
        db.add(Problem(client_id=client.id, description=description.strip(),
                       icd10=icd10.strip()))
        log(db, "Problem Added", "client", client_id)
        db.commit()
    return RedirectResponse(f"/clients/{client_id}", status_code=303)


def _date(raw: str) -> date | None:
    """A date from a form field, or nothing. Never today as a fallback -
    an invented date on a clinical row is worse than an absent one."""
    try:
        return datetime.strptime((raw or "").strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


@app.post("/clients/{client_id}/allergies")
def add_allergy(client_id: int, substance: str = F(""), reaction: str = F(""),
                severity: str = F(""), db: Session = Depends(get_db),
                _=Depends(needs(perms.CLINICAL_EDIT))):
    client = get_or_404(db, Client, client_id)
    if substance.strip():
        db.add(Allergy(client_id=client.id, substance=substance.strip(),
                       reaction=reaction.strip(), severity=severity.strip()))
        log(db, "Allergy Added", "client", client_id)
        db.commit()
    return RedirectResponse(f"/clients/{client_id}#allergies", status_code=303)


@app.post("/clients/{client_id}/allergies/{allergy_id}/delete")
def remove_allergy(client_id: int, allergy_id: int, db: Session = Depends(get_db),
                   _=Depends(needs(perms.CLINICAL_EDIT))):
    """Allergies are the one clinical list that is deleted rather than ended.

    A medication that stops is history worth keeping. An allergy recorded in
    error is not history - it is wrong, and leaving it visible means somebody
    withholds a drug the patient can safely take.
    """
    allergy = get_or_404(db, Allergy, allergy_id)
    db.delete(allergy)
    log(db, "Allergy Removed", "client", client_id)
    db.commit()
    return RedirectResponse(f"/clients/{client_id}#allergies", status_code=303)


@app.post("/clients/{client_id}/vitals")
def add_vitals(client_id: int, taken_on: str = F(""), bp: str = F(""),
               hr: str = F(""), temp: str = F(""), height: str = F(""),
               weight: str = F(""), bmi: str = F(""), spo2: str = F(""),
               db: Session = Depends(get_db),
               _=Depends(needs(perms.CLINICAL_EDIT))):
    """A new set of readings, never an edit of the last one. Vitals are a series
    - overwriting yesterday's blood pressure with today's destroys the trend
    that made them worth recording."""
    client = get_or_404(db, Client, client_id)
    values = [bp, hr, temp, height, weight, bmi, spo2]
    if any(v.strip() for v in values):
        db.add(VitalSigns(client_id=client.id, taken_on=_date(taken_on) or date.today(),
                          bp=bp.strip(), hr=hr.strip(), temp=temp.strip(),
                          height=height.strip(), weight=weight.strip(),
                          bmi=bmi.strip(), spo2=spo2.strip()))
        log(db, "Vitals Recorded", "client", client_id)
        db.commit()
    return RedirectResponse(f"/clients/{client_id}#vitals", status_code=303)


@app.post("/clients/{client_id}/notes")
def add_note(client_id: int, kind: str = F("PMHx"), body: str = F(""),
             db: Session = Depends(get_db),
             _=Depends(needs(perms.CLINICAL_EDIT))):
    client = get_or_404(db, Client, client_id)
    if body.strip():
        db.add(ClinicalNote(client_id=client.id, kind=kind.strip() or "PMHx",
                            body=body.strip()))
        log(db, "Clinical Note Added", "client", client_id)
        db.commit()
    return RedirectResponse(f"/clients/{client_id}#history", status_code=303)


@app.post("/clients/{client_id}/labs")
def add_lab(client_id: int, name: str = F(""), ordered_on: str = F(""),
            status: str = F("Needs Results"), db: Session = Depends(get_db),
            _=Depends(needs(perms.CLINICAL_EDIT))):
    client = get_or_404(db, Client, client_id)
    if name.strip():
        db.add(LabOrder(client_id=client.id, name=name.strip(),
                        ordered_on=_date(ordered_on),
                        status=status.strip() or "Needs Results"))
        log(db, "Lab Order Added", "client", client_id)
        db.commit()
    return RedirectResponse(f"/clients/{client_id}#labs", status_code=303)


@app.post("/clients/{client_id}/visit")
def add_visit(client_id: int, condition: str = F(""), procedure: str = F(""),
              outcome: str = F(""), length_of_stay: str = F(""),
              readmission: str = F(""), db: Session = Depends(get_db),
              _=Depends(needs(perms.CLINICAL_EDIT))):
    """Record a visit into the hospital archive, under the patient's own number.

    This is the write-back the practice asked for: what happens here joins the
    same history the hospital already holds, keyed by the same patient number,
    instead of living in a separate list nobody reading the archive would see.

    It refuses when the patient has no number. There would be nowhere to put the
    row - and inventing a number here is how two people end up sharing one.
    """
    client = get_or_404(db, Client, client_id)
    if not client.hospital_id:
        request_error = "Link this patient to a hospital number first."
        raise HTTPException(status_code=400, detail=request_error)
    if condition.strip():
        db.add(HospitalRecord(
            patient_id=client.hospital_id,
            age=_age_of(client),
            gender=_sex_of(client),         # from the patient's own form answer
            condition=condition.strip(),
            procedure=procedure.strip(),
            outcome=outcome.strip(),
            length_of_stay=_int_or_none(length_of_stay),
            readmission=bool(readmission),
            source=ENTERED_HERE,
        ))
        log(db, f"Visit recorded in archive (patient no. {client.hospital_id})",
            "client", client_id)
        db.commit()
    return RedirectResponse(f"/clients/{client_id}#archive", status_code=303)


def _sex_of(client) -> str:
    """The patient's sex as they stated it on a form.

    The client record does not hold one - staff enter four fields and sex is not
    among them, because the patient answers it themselves. Reading it back from
    their form keeps an archive row the practice writes as complete as one the
    hospital sent, instead of leaving a blank column that looks like a fault.
    """
    return matching._recorded_sex(client)


def _age_of(client) -> int | None:
    if not client.dob:
        return None
    today = date.today()
    return (today.year - client.dob.year -
            ((today.month, today.day) < (client.dob.month, client.dob.day)))


def _int_or_none(raw: str) -> int | None:
    raw = (raw or "").strip()
    return int(raw) if raw.isdigit() else None


@app.get("/integrations", response_class=HTMLResponse)
def integrations_view(request: Request, db: Session = Depends(get_db),
                      _=Depends(needs(perms.USERS_MANAGE))):
    """What this app connects to, and whether it can reach any of it.

    Results are held in the session rather than the database: a connection test
    is true at the moment it ran and says nothing about five minutes later, so
    storing it would create a stale green light - exactly the thing this screen
    exists to avoid.
    """
    return render("integrations.html", ctx(
        request, db, nav="integrations",
        connections=integrations.CONNECTIONS,
        can_store=credentials.available(),
        saved=request.session.pop("credential_note", ""),
        save_error=request.session.pop("credential_error", ""),
        results=request.session.get("integration_results", {}),
        run=intakeq.latest_run(db), day_limit=intakeq.PER_DAY,
        demo=simulation.enabled(db)))


@app.post("/integrations/{key}/save")
async def integrations_save(key: str, request: Request,
                            db: Session = Depends(get_db),
                            _=Depends(needs(perms.USERS_MANAGE))):
    """Store the credentials typed into one connection's panel.

    Reads the form generically rather than naming each field, so a variable
    added to a Connection is editable without a second edit here - two places
    to change is how the two drift apart.

    A blank box is not an instruction to clear the value. Most saves change one
    field and leave the rest untouched, and if blank meant "delete" then every
    such save would quietly wipe everything the administrator did not retype.
    Removing one is a separate, explicit button.
    """
    connection = integrations.BY_KEY.get(key)
    if not connection:
        raise HTTPException(status_code=404, detail="No such connection")
    if not connection.editable:
        request.session["credential_error"] = (
            f"{connection.name} is configured on the server, not here.")
        return RedirectResponse("/integrations", status_code=303)
    if not credentials.available():
        request.session["credential_error"] = (
            "Credentials cannot be stored on this copy: SESSION_SECRET is not "
            "set, and it is what encrypts them.")
        return RedirectResponse("/integrations", status_code=303)

    form = await request.form()
    user = auth.current_user(request, db)
    changed, cleared = [], []
    for var, _what, _secret in connection.settings:
        if f"clear__{var}" in form:
            if credentials.drop(db, var):
                cleared.append(var)
            continue
        typed = (form.get(var) or "").strip()
        if not typed:
            continue
        # Quotes pasted in from a .env line are the fault that cost a day of
        # this project. Strip them here rather than store a value that will
        # fail with an error naming neither the quote nor the variable.
        typed = typed.strip('"').strip("'").strip()
        if typed:
            credentials.put(db, var, typed, who=user.name if user else "")
            changed.append(var)

    if changed or cleared:
        log(db, f"Credentials changed for {connection.name}: "
                + ", ".join(changed + [f"{c} removed" for c in cleared]),
            "integration", key)
        db.commit()
        request.session["credential_note"] = (
            f"{connection.name}: "
            + ", ".join([f"{c} saved" for c in changed]
                        + [f"{c} removed" for c in cleared])
            + ". Press Test connection to prove it works.")
    else:
        request.session["credential_error"] = "Nothing was typed, so nothing changed."
    return RedirectResponse("/integrations", status_code=303)


@app.post("/integrations/{key}/test")
def integrations_test(key: str, request: Request, db: Session = Depends(get_db),
                      _=Depends(needs(perms.USERS_MANAGE))):
    connection = integrations.BY_KEY.get(key)
    if not connection:
        raise HTTPException(status_code=404, detail="No such connection")

    result = integrations.run(key, db)
    held = dict(request.session.get("integration_results", {}))
    # The session is stored as JSON in a cookie, so everything put in it has to
    # be JSON already. A datetime here fails at the moment the response is
    # written, long after the code that looks wrong.
    held[key] = {"ok": result.ok, "summary": result.summary,
                 "detail": result.detail,
                 "at": result.at.strftime("%d %b %H:%M")}
    request.session["integration_results"] = held

    # Worth an audit line: a failed credential test is how somebody finds out a
    # key was rotated, and a successful one records who proved it worked.
    log(db, f"Connection tested: {connection.name} - "
            f"{'ok' if result.ok else 'failed'}", "integration", key)
    db.commit()
    return RedirectResponse("/integrations", status_code=303)


@app.post("/integrations/intakeq/import")
def intakeq_import(request: Request, mode: str = F("resume"),
                   db: Session = Depends(get_db),
                   _=Depends(needs(perms.INTEGRATIONS_IMPORT))):
    """Do one slice of the IntakeQ import, then come back.

    A backfill is longer than a web request is allowed to be, so the button is
    pressed repeatedly rather than once - by a person, or by the page's own
    refresh while a run is active. Each press advances the checkpoint and
    returns; nothing is held open, and nothing is lost if the tab is closed.
    """
    run = intakeq.latest_run(db)
    if mode == "full" or run is None:
        run = intakeq.begin(db, full=True)
    elif mode == "changes":
        run = intakeq.begin(db, full=False)
    elif run.status in ("paused", "failed"):
        #  Resuming a stopped run keeps its counters and its place. A new row
        #  here would report "0 imported" over work that was already done.
        run.status = "running"
        run.message = ""

    intakeq.run_slice(db, run)
    log(db, f"IntakeQ import {run.status}: +{run.clients_new} patients, "
            f"+{run.intakes_new} intakes", "integration", "intakeq")
    db.commit()
    return RedirectResponse("/integrations#intakeq-import", status_code=303)


@app.post("/integrations/demo")
def integrations_demo(request: Request, on: str = F(""),
                      db: Session = Depends(get_db),
                      _=Depends(needs(perms.USERS_MANAGE))):
    """Turn demonstration mode on or off.

    Audited like a credential change, because that is what it is: it decides
    whether the numbers on every integration screen came from a vendor or from
    this machine, and somebody reading the log later deserves to know which
    the practice was looking at.
    """
    user = auth.current_user(request, db)
    wanted = on.strip() in ("1", "on", "true")
    simulation.set_enabled(db, wanted, who=user.name if user else "")
    log(db, f"Demonstration mode turned {'on' if wanted else 'off'}",
        "integration", "demo")
    db.commit()
    request.session["credential_note"] = (
        "Demonstration mode is ON. IntakeQ and Tebra are answered by this "
        "machine with invented data, and every screen says so."
        if wanted else
        "Demonstration mode is OFF. The integrations will use real "
        "credentials, and refuse until they have them.")
    return RedirectResponse("/integrations", status_code=303)


@app.post("/integrations/demo/clear")
def integrations_demo_clear(request: Request, db: Session = Depends(get_db),
                            _=Depends(needs(perms.USERS_MANAGE))):
    """Remove every row demonstration mode created, and nothing else.

    Scoped by `source`, which is why that column was worth having. A
    demonstration that cannot be undone is one nobody dares run against a
    database they care about - and the first thing anybody does after a demo is
    ask whether the invented patients are still there.
    """
    doomed = db.query(Client).filter_by(source=intakeq.INTAKEQ).all()
    ids = [c.id for c in doomed]
    intakes = 0
    if ids:
        intakes = (db.query(intakeq.ImportedIntake)
                   .filter(intakeq.ImportedIntake.client_id.in_(ids))
                   .delete(synchronize_session=False))
    #  Intakes whose patient was never matched are demonstration rows too, and
    #  leaving orphans behind would make the count wrong next time.
    intakes += (db.query(intakeq.ImportedIntake)
                .filter(intakeq.ImportedIntake.client_id.is_(None))
                .delete(synchronize_session=False))
    for client in doomed:
        db.delete(client)
    db.query(intakeq.ImportRun).delete(synchronize_session=False)
    log(db, f"Demo data removed: {len(ids)} patients, {intakes} intakes",
        "integration", "demo")
    db.commit()
    request.session["credential_note"] = (
        f"Removed {len(ids)} simulated patient(s) and {intakes} intake(s). "
        f"Nothing else was touched.")
    return RedirectResponse("/integrations", status_code=303)


@app.post("/clients/{client_id}/tebra")
def push_client_to_tebra(client_id: int, request: Request,
                         db: Session = Depends(get_db),
                         _=Depends(needs(perms.CLINICAL_EDIT))):
    """Create this patient's chart in Tebra.

    Behind CLINICAL_EDIT rather than CLIENTS_EDIT: creating a medical record in
    another system is a clinical act, and front desk moving a patient into the
    practice's EHR is not a decision reception should be making alone.
    """
    client = get_or_404(db, Client, client_id)
    user = auth.current_user(request, db)
    try:
        new_id = tebra.push_patient(db, client, user=user, ip=client_ip(request))
        db.commit()
        request.session["chart_note"] = (
            f"{client.first_name} has a Tebra chart: {new_id}."
            + (" (simulated)" if simulation.enabled(db) else ""))
    except tebra.TebraError as exc:
        db.rollback()
        request.session["chart_error"] = str(exc)
    return RedirectResponse(f"/clients/{client_id}#tebra", status_code=303)


@app.post("/charges/{charge_id}/tebra")
def push_charge_to_tebra(charge_id: int, request: Request,
                         db: Session = Depends(get_db),
                         _=Depends(needs(perms.CLINICAL_EDIT))):
    """Post this charge to Tebra as an encounter.

    Same permission as pushing the chart itself - billing a visit into
    another system's ledger is a clinical-financial act, not a front-desk one.
    """
    charge = get_or_404(db, ehr.Charge, charge_id)
    client = get_or_404(db, Client, charge.client_id)
    user = auth.current_user(request, db)
    try:
        new_id = tebra.push_charge(db, client, charge, user=user, ip=client_ip(request))
        db.commit()
        request.session["chart_note"] = (
            f"Charge {charge.cpt} posted to Tebra: {new_id}."
            + (" (simulated)" if simulation.enabled(db) else ""))
    except tebra.TebraError as exc:
        db.rollback()
        request.session["chart_error"] = str(exc)
    return RedirectResponse(f"/clients/{client.id}#billing", status_code=303)


@app.post("/payments/{payment_id}/tebra")
def push_payment_to_tebra(payment_id: int, request: Request,
                          db: Session = Depends(get_db),
                          _=Depends(needs(perms.BILLING_EDIT))):
    """Post this patient-sourced payment to Tebra."""
    payment = get_or_404(db, ehr.Payment, payment_id)
    client = get_or_404(db, Client, payment.client_id)
    user = auth.current_user(request, db)
    try:
        new_id = tebra.push_payment(db, client, payment, user=user, ip=client_ip(request))
        db.commit()
        request.session["chart_note"] = (
            f"Payment of {payment.amount} posted to Tebra: {new_id}."
            + (" (simulated)" if simulation.enabled(db) else ""))
    except tebra.TebraError as exc:
        db.rollback()
        request.session["chart_error"] = str(exc)
    return RedirectResponse(f"/clients/{client.id}#billing", status_code=303)


@app.post("/integrations/intakeq/preview")
def intakeq_preview(request: Request, db: Session = Depends(get_db),
                    _=Depends(needs(perms.INTEGRATIONS_IMPORT))):
    """Two requests, no writes: what IntakeQ sends and what we make of it.

    Worth its own button because the field mapping is the one part of the
    import that cannot be proved without a real key, and a wrong field name
    fails silently - it yields a blank column rather than an error. Seeing that
    on two records costs nothing; discovering it after a two thousand patient
    backfill costs the backfill.
    """
    result = intakeq.preview(db)
    log(db, "IntakeQ preview run (no data written)", "integration", "intakeq")
    db.commit()
    #  Rendered here rather than stashed and redirected to. The session is a
    #  signed JSON cookie with a four-kilobyte ceiling, and a couple of raw
    #  IntakeQ records go straight past it - the write succeeds, the browser
    #  drops the oversized cookie, and the user is silently signed out.
    return render("intakeq_preview.html", ctx(
        request, db, nav="integrations", result=result))


@app.post("/integrations/intakeq/import/stop")
def intakeq_import_stop(request: Request, db: Session = Depends(get_db),
                        _=Depends(needs(perms.INTEGRATIONS_IMPORT))):
    run = intakeq.latest_run(db)
    if run and run.status == "running":
        run.status = "paused"
        run.message = "Stopped by hand. Resume picks up at the same page."
        log(db, "IntakeQ import stopped by hand", "integration", "intakeq")
        db.commit()
    return RedirectResponse("/integrations#intakeq-import", status_code=303)


@app.get("/intakes/{intake_id}/pdf")
def intakeq_intake_pdf(intake_id: int, request: Request,
                       db: Session = Depends(get_db),
                       _=Depends(needs(perms.SUBMISSIONS_VIEW))):
    """The signed intake document, fetched from IntakeQ the first time.

    Reading a patient's completed paperwork is an event worth recording, the
    same as opening a submission - the audit log should not be able to tell you
    who opened a form filled in here but not one filled in at IntakeQ.
    """
    intake = get_or_404(db, intakeq.ImportedIntake, intake_id)
    body = intakeq.fetch_pdf(db, intake)
    log(db, f"IntakeQ intake opened: {intake.questionnaire_name or intake_id}",
        "intake", intake_id)
    db.commit()

    if not body:
        raise HTTPException(
            status_code=502,
            detail=intake.pdf_error or "IntakeQ did not return the document.")
    safe = "".join(c for c in (intake.questionnaire_name or "intake")
                   if c.isalnum() or c in " -_").strip() or "intake"
    return Response(
        content=body, media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{safe}.pdf"'})


@app.get("/calendar", response_class=HTMLResponse)
def calendar_view(request: Request, year: int = 0, month: int = 0,
                  provider: int = 0, db: Session = Depends(get_db),
                  _=Depends(needs(perms.CLIENTS_VIEW))):
    today = date.today()
    year = year or today.year
    month = month or today.month
    if not 1 <= month <= 12:
        year, month = today.year, today.month

    prev_y, prev_m = (year, month - 1) if month > 1 else (year - 1, 12)
    next_y, next_m = (year, month + 1) if month < 12 else (year + 1, 1)

    return render("calendar.html", ctx(
        request, db, nav="calendar",
        weeks=schedule.month_grid(db, year, month, provider or None),
        year=year, month=month, today=today,
        month_name=date(year, month, 1).strftime("%B %Y"),
        prev_y=prev_y, prev_m=prev_m, next_y=next_y, next_m=next_m,
        provider=provider,
        providers=db.query(User).filter(User.is_active.is_(True))
                    .order_by(User.name).all(),
        clients=db.query(Client).filter_by(archived=False)
                  .order_by(Client.last_name).all(),
        kinds=schedule.KINDS,
        reminders=schedule.reminders(db),
        error=request.session.pop("calendar_error", "")))


@app.post("/calendar/book")
def calendar_book(request: Request, client_id: int = F(...), on_day: str = F(""),
                  at_time: str = F(""), minutes: str = F("30"),
                  kind: str = F("Screening visit"), provider_id: str = F(""),
                  location: str = F(""), notes: str = F(""),
                  db: Session = Depends(get_db),
                  _=Depends(needs(perms.SCHEDULE_EDIT))):
    day = _date(on_day)
    if not day:
        request.session["calendar_error"] = "Pick a date for the appointment."
        return RedirectResponse("/calendar", status_code=303)
    client = get_or_404(db, Client, client_id)
    booking = schedule.Appointment(
        client_id=client.id, on_day=day, at_time=at_time.strip()[:5],
        minutes=_int_or_none(minutes) or 30, kind=kind.strip() or "Screening visit",
        provider_id=_int_or_none(provider_id), location=location.strip(),
        notes=notes.strip())
    db.add(booking)
    cache.drop_nav()
    log(db, f"Appointment booked ({day:%d %b})", "client", client.id)
    db.commit()
    return RedirectResponse(f"/calendar?year={day.year}&month={day.month}",
                            status_code=303)


@app.post("/calendar/{appt_id}/status")
def calendar_status(appt_id: int, status: str = F("attended"),
                    db: Session = Depends(get_db),
                    _=Depends(needs(perms.SCHEDULE_EDIT))):
    appt = get_or_404(db, schedule.Appointment, appt_id)
    try:
        appt.status = schedule.Attendance(status)
    except ValueError:
        raise HTTPException(status_code=400, detail="Unknown attendance value")
    log(db, f"Appointment marked {appt.status.value}", "client", appt.client_id)
    db.commit()
    return RedirectResponse(f"/calendar?year={appt.on_day.year}&month={appt.on_day.month}",
                            status_code=303)


@app.get("/trials", response_class=HTMLResponse)
def trials_list(request: Request, db: Session = Depends(get_db),
                _=Depends(needs(perms.SUBMISSIONS_VIEW))):
    rows = db.query(Trial).order_by(Trial.is_active.desc(), Trial.id).all()
    return render("trials.html", ctx(request, db, nav="trials", trials=rows))


@app.get("/trials/{trial_id}", response_class=HTMLResponse)
def trial_detail(trial_id: int, request: Request, db: Session = Depends(get_db),
                 _=Depends(needs(perms.SUBMISSIONS_VIEW))):
    """The protocol, and every patient screened against it."""
    trial = get_or_404(db, Trial, trial_id)
    clients = (db.query(Client).filter_by(archived=False)
               .options(selectinload(Client.medications),
                        selectinload(Client.submissions_list)
                        .selectinload(Submission.answers)
                        .joinedload(Answer.question))
               .order_by(Client.last_name).all())

    #  Same two fixes as the caseload screen: eager-load what the evaluation
    #  reads, and fetch the whole archive in one query rather than one per row.
    archives = records_for_many(db, (c.hospital_id for c in clients))
    screened = [(c, trials.evaluate(c, trial, archives.get(c.hospital_id or 0, [])))
                for c in clients]
    # Most promising first: the point of the screen is to find who to read next.
    rank = {"Looks eligible": 0, "Needs review": 1, "No criteria": 2, "Not eligible": 3}
    screened.sort(key=lambda pair: (rank.get(pair[1].verdict, 9),
                                    -len([x for x in pair[1].checks
                                          if x.result == trials.MET])))

    log(db, f"Trial screening viewed ({trial.name})", "trial", trial_id)
    db.commit()
    return render("trial_detail.html", ctx(
        request, db, nav="trials", trial=trial, screened=screened,
        MET=trials.MET, FAILED=trials.FAILED, UNKNOWN=trials.UNKNOWN,
        errors=request.session.pop("criterion_error", []),
        draft=request.session.pop("criterion_draft", {}),
        added=request.session.pop("criterion_added", "")))


@app.post("/trials/new")
def trial_new(request: Request, name: str = F(""), nct_id: str = F(""),
              condition: str = F(""), site: str = F(""),
              db: Session = Depends(get_db),
              _=Depends(needs(perms.FORMS_CREATE))):
    if not name.strip():
        return RedirectResponse("/trials", status_code=303)
    trial = Trial(name=name.strip(), nct_id=nct_id.strip().upper(),
                  condition=condition.strip(), site=site.strip())
    db.add(trial)
    db.flush()
    log(db, "Trial created", "trial", trial.id)
    db.commit()
    return RedirectResponse(f"/trials/{trial.id}", status_code=303)


@app.post("/trials/{trial_id}/criteria")
def criterion_add(trial_id: int, request: Request,
                  kind: str = F("inclusion"), rule: str = F("manual"),
                  target: str = F(""), min_value: str = F(""), max_value: str = F(""),
                  text: str = F(""), db: Session = Depends(get_db),
                  _=Depends(needs(perms.FORMS_EDIT))):
    trial = get_or_404(db, Trial, trial_id)
    try:
        as_kind, as_rule = trials.CriterionKind(kind), trials.Rule(rule)
    except ValueError:
        request.session["criterion_error"] = ["That is not a criterion type this "
                                              "app knows. Pick one from the lists."]
        return RedirectResponse(f"/trials/{trial_id}#add", status_code=303)

    low, high = _int_or_none(min_value), _int_or_none(max_value)
    faults = trials.problems_with(as_rule, target, low, high)
    if faults:
        # Hand back what they typed along with what is wrong with it. Losing a
        # half-filled form to an error message is how somebody gives up.
        request.session["criterion_error"] = faults
        request.session["criterion_draft"] = {
            "kind": kind, "rule": rule, "target": target,
            "min_value": min_value, "max_value": max_value, "text": text}
        return RedirectResponse(f"/trials/{trial_id}#add", status_code=303)

    db.add(trials.Criterion(
        trial_id=trial.id, kind=as_kind, rule=as_rule,
        target=trials.tidy_target(target),
        min_value=low, max_value=high,
        text=text.strip(), position=len(trial.criteria)))
    log(db, "Trial criterion added", "trial", trial_id)
    db.commit()
    request.session["criterion_added"] = (text.strip()
                                          or trials.RULE_LABEL.get(as_rule, rule))
    return RedirectResponse(f"/trials/{trial_id}#add", status_code=303)


@app.post("/trials/{trial_id}/criteria/{criterion_id}/delete")
def criterion_remove(trial_id: int, criterion_id: int, db: Session = Depends(get_db),
                     _=Depends(needs(perms.FORMS_EDIT))):
    db.delete(get_or_404(db, trials.Criterion, criterion_id))
    log(db, "Trial criterion removed", "trial", trial_id)
    db.commit()
    return RedirectResponse(f"/trials/{trial_id}", status_code=303)


def _as_date(term: str):
    """A date of birth typed any of the ways people type one, or nothing."""
    for pattern in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%m/%d/%Y", "%d %b %Y"):
        try:
            return datetime.strptime(term.strip(), pattern).date()
        except ValueError:
            continue
    return None


@app.get("/archive", response_class=HTMLResponse)
def archive(request: Request, q: str = "", page: int = 1,
            db: Session = Depends(get_db),
            _=Depends(needs(perms.CLIENTS_VIEW))):
    """The hospital archive as it was imported - read-only, on purpose.

    Nothing on this screen can be edited or added. The CSV on disk is the
    authority and the table is a copy of it; an edit made here would be silently
    undone by the next import, which is worse than not being able to edit at all.

    A patient the hospital has never seen is recorded from their own chart
    instead. That keeps every row in this table answerable to something outside
    it - either the hospital's file or a named patient - rather than typed
    directly into a dataset that is supposed to mirror another system.
    """
    rows = db.query(HospitalRecord)
    term = q.strip()
    matched_by_name = []
    if term:
        as_date = _as_date(term)
        if as_date:
            # A date can only sensibly mean a date of birth here, and it is the
            # thing that tells two people with the same name apart.
            rows = rows.filter(HospitalRecord.dob == as_date)
        elif term.isdigit():
            rows = rows.filter(HospitalRecord.patient_id == int(term))
        else:
            # The archive carries no names - the hospital's file has ten columns
            # and none of them is one. But somebody searching "Thomas Bell" is
            # asking a reasonable question, so resolve the name through the
            # patients who have been linked to a number and search on that.
            like = f"%{term}%"
            # The archive now carries its own names, so a name search hits it
            # directly. Registered patients are still resolved through their
            # hospital number as well, in case the practice spells a name
            # differently from the way the hospital recorded it.
            matched_by_name = (
                db.query(Client)
                .filter(Client.hospital_id.isnot(None), Client.archived.is_(False))
                .filter((Client.first_name + " " + Client.last_name).ilike(like)
                        | Client.first_name.ilike(like)
                        | Client.last_name.ilike(like))
                .all())
            numbers = [c.hospital_id for c in matched_by_name]
            condition = (HospitalRecord.name.ilike(like)
                         | HospitalRecord.condition.ilike(like)
                         | HospitalRecord.procedure.ilike(like)
                         | HospitalRecord.outcome.ilike(like))
            if numbers:
                condition = condition | HospitalRecord.patient_id.in_(numbers)
            rows = rows.filter(condition)

    # One line per person, not per visit. A patient who attended three times was
    # showing as three near-identical rows with the same name and date of birth,
    # which reads as duplicated data rather than as a history.
    #
    # The visits themselves are kept, and stay on the row: a person's second
    # admission is a different condition on a different date, and collapsing
    # that into "Diabetes" would throw away the thing the archive is for.
    every = rows.order_by(HospitalRecord.patient_id, HospitalRecord.id).all()
    people: dict[int, list] = {}
    for r in every:
        people.setdefault(r.patient_id, []).append(r)

    numbers_in_order = sorted(people)
    total = len(numbers_in_order)
    per_page = 50
    page = max(1, page)
    found = [(pid, people[pid]) for pid in
             numbers_in_order[(page - 1) * per_page: page * per_page]]
    visits_shown = sum(len(v) for _pid, v in found)

    # Which of these numbers belong to somebody registered here. Fetched in one
    # query rather than one per row - 984 lookups to draw a table is not a table.
    linked = {c.hospital_id: c for c in
              db.query(Client).filter(Client.hospital_id.isnot(None)).all()}

    sources = [s for (s,) in db.query(HospitalRecord.source).distinct().all() if s]
    log(db, f"Hospital Archive Viewed ({total} patients)", "archive", total)
    db.commit()
    return render("archive.html", ctx(
        request, db, nav="archive", people=found, linked=linked, q=term,
        matched_by_name=matched_by_name, visits_shown=visits_shown,
        total=total, page=page, pages=max(1, -(-total // per_page)),
        per_page=per_page, sources=sources,
        archive_error=request.session.pop("archive_error", ""),
        archive_note=request.session.pop("archive_note", ""),
        visits=len(every),
        conditions=db.query(HospitalRecord.condition).distinct().count(),
        patients=db.query(HospitalRecord.patient_id).distinct().count()))


ARCHIVE_SECTIONS = [
    ("facesheet", "Facesheet"), ("history", "History"), ("problems", "Problems"),
    ("medications", "Medications"), ("immunizations", "Immunizations"),
    ("allergies", "Allergies"), ("vitals", "Vitals"), ("notes", "Notes"),
    ("labs", "Labs/Studies"), ("demographics", "Demographics"),
]


@app.get("/archive/{patient_id}", response_class=HTMLResponse)
@app.get("/archive/{patient_id}/{section}", response_class=HTMLResponse)
def archive_record(patient_id: int, request: Request, section: str = "facesheet",
                   db: Session = Depends(get_db),
                   _=Depends(needs(perms.CLIENTS_VIEW))):
    """One archive patient, laid out the way the hospital system lays it out.

    Staff who use Tebra all day already know where medications sit relative to
    problems, and which tab holds the vitals. Reproducing that arrangement is
    not imitation for its own sake - it means nobody has to learn a second
    place for the same eight things, and a screen that reads the same way is a
    screen they trust the numbers on.

    What it shows comes from two different places, and they are not merged.
    The visits come from the archive, which is the hospital's file. Everything
    clinical - medications, problems, allergies, vitals, notes - lives against
    the practice's own patient record, and exists only where somebody has
    linked the two. An unlinked archive number therefore shows a real header
    and honestly empty sections rather than invented content.
    """
    if section not in dict(ARCHIVE_SECTIONS):
        raise HTTPException(status_code=404, detail="No such section")

    visits = records_for(db, patient_id)
    if not visits:
        raise HTTPException(status_code=404, detail="No such patient number")
    client = (db.query(Client).filter_by(hospital_id=patient_id)
              .filter(Client.archived.is_(False)).first())

    return render("archive_record.html", ctx(
        request, db, nav="archive", section=section, sections=ARCHIVE_SECTIONS,
        patient_id=patient_id, visits=visits, first=visits[0], client=client,
        immunisations=(db.query(clinical.Immunization)
                       .filter_by(client_id=client.id)
                       .order_by(clinical.Immunization.name).all()
                       if client else []),
        default_vaccines=clinical.ADULT_VACCINES,
        note=request.session.pop("record_note", "")))


@app.get("/my-patients", response_class=HTMLResponse)
def my_patients(request: Request, provider: int = -1, q: str = "",
                db: Session = Depends(get_db),
                _=Depends(needs(perms.CLIENTS_VIEW))):
    """A clinician's own caseload, screened against the open trial.

    The default is deliberately different per role rather than the same screen
    for everyone. A practitioner opening this wants their own list and nothing
    else - filtering to themselves every time would be a chore the app should
    have done. Anyone who supervises opens it on the whole practice, because
    their question is "who is unassigned" and a pre-filtered view hides exactly
    that.

    Nobody is prevented from looking at another clinician's list. This is a
    small practice where cover is routine, and a permission wall between
    colleagues who share patients produces workarounds, not security.
    """
    me = auth.current_user(request, db)
    doctors = (db.query(User)
               .filter(User.is_active.is_(True),
                       User.role == UserRole.practitioner)
               .order_by(User.name).all())

    if provider == -1:
        provider = (me.id if me and me.role == UserRole.practitioner else 0)

    rows = db.query(Client).filter_by(archived=False)
    if provider > 0:
        rows = rows.filter(Client.provider_id == provider)
    elif provider == -2:                       # the "unassigned" filter
        rows = rows.filter(Client.provider_id.is_(None))
    term = q.strip()
    if term:
        like = f"%{term}%"
        rows = rows.filter((Client.first_name + " " + Client.last_name).ilike(like)
                           | Client.first_name.ilike(like)
                           | Client.last_name.ilike(like))
    #  Everything the loop below touches, fetched up front. Without these three
    #  the screen costs a query per patient per relationship - fine at seven
    #  patients, roughly fifteen thousand queries at two thousand, which is not
    #  slow so much as dead.
    clients = (rows.options(selectinload(Client.medications),
                            selectinload(Client.submissions_list)
                            .selectinload(Submission.answers)
                            .joinedload(Answer.question),
                            joinedload(Client.provider))
               .order_by(Client.last_name, Client.first_name).all())

    trial = db.query(Trial).filter_by(is_active=True).first()
    archives = records_for_many(db, (c.hospital_id for c in clients))
    cards = []
    for c in clients:
        screening = (trials.evaluate(c, trial, archives.get(c.hospital_id or 0, []))
                     if trial else None)
        cards.append({
            "client": c,
            "screening": screening,
            "inclusion": screening.tally(trials.CriterionKind.inclusion)
                         if screening else (0, 0),
            "exclusion": screening.tally(trials.CriterionKind.exclusion)
                         if screening else (0, 0),
            "open_form": next((s for s in c.submissions_list if s.is_open), None),
        })

    log(db, f"Caseload viewed ({len(cards)} patients)", "clients", len(cards))
    db.commit()
    return render("my_patients.html", ctx(
        request, db, nav="mypatients", cards=cards, doctors=doctors,
        provider=provider, q=term, trial=trial,
        unassigned=db.query(Client).filter_by(archived=False,
                                              provider_id=None).count(),
        note=request.session.pop("caseload_note", "")))


@app.post("/clients/{client_id}/provider")
def set_provider(client_id: int, request: Request, provider_id: str = F(""),
                 back: str = F(""), db: Session = Depends(get_db),
                 _=Depends(needs(perms.CLIENTS_EDIT))):
    """Assign a patient to a clinician, or unassign them."""
    client = get_or_404(db, Client, client_id)
    chosen = _int_or_none(provider_id)
    client.provider_id = chosen
    who = db.get(User, chosen).name if chosen else "nobody"
    log(db, f"Patient assigned to {who}", "client", client_id)
    db.commit()
    request.session["caseload_note"] = f"{client.name} is now assigned to {who}."
    return RedirectResponse(back or f"/clients/{client_id}", status_code=303)


@app.get("/records", response_class=HTMLResponse)
def records(request: Request, db: Session = Depends(get_db),
            _=Depends(needs(perms.SUBMISSIONS_VIEW))):
    """One row per patient - form answers and chart data in the same table."""
    clients = db.query(Client).filter_by(archived=False).order_by(Client.last_name).all()
    log(db, "Combined Records Viewed", "records", len(clients))
    db.commit()
    return render("records.html", ctx(
        request, db, nav="records", clients=clients,
        headings=rec.headings(), rows=[(c, rec.row_for(c)) for c in clients]))


def _build_workbook(request: Request, db: Session) -> tuple[bytes, str, int]:
    clients = db.query(Client).filter_by(archived=False).order_by(Client.last_name).all()
    data = rec.to_excel(clients)
    user = auth.current_user(request, db)
    log(db, f"Records Downloaded ({len(clients)} patients)", "records",
        len(clients), user_id=user.id if user else None, ip=client_ip(request))
    db.commit()
    stamp = datetime.utcnow().strftime("%Y-%m-%d-%H%M")
    return data, f"patient-records-{stamp}.xlsx", len(clients)


@app.get("/records/download")
def records_download(request: Request, db: Session = Depends(get_db),
                     _=Depends(needs(perms.SUBMISSIONS_VIEW))):
    """Browser download. Works in Chrome; does nothing in the desktop window,
    which has no download manager - use /records/save from there."""
    data, filename, _n = _build_workbook(request, db)
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/records/save", response_class=HTMLResponse)
def records_save(request: Request, db: Session = Depends(get_db),
                 _=Depends(needs(perms.SUBMISSIONS_VIEW))):
    """Write the spreadsheet to the Downloads folder and say where it went.

    The app runs inside a pywebview window, which embeds the OS webview but no
    download manager. A Content-Disposition response there is simply dropped -
    the click appears to do nothing. Writing the file ourselves and reporting the
    path is the only thing that reliably works in both the desktop window and a
    browser.
    """
    if HOSTED:
        # Nothing on this machine belongs to the person clicking. Let the browser
        # do what browsers are for.
        return RedirectResponse("/records/download", status_code=303)

    data, filename, count = _build_workbook(request, db)

    downloads = Path.home() / "Downloads"
    if not downloads.is_dir():
        downloads = Path.home()
    target = downloads / filename

    n = 1
    while target.exists():                     # never overwrite a previous export
        target = downloads / f"{target.stem.rsplit('(', 1)[0]}({n}).xlsx"
        n += 1

    target.write_bytes(data)
    return render("records_saved.html", ctx(request, db, nav="records",
                                            path=str(target), filename=target.name,
                                            folder=str(downloads),
                                            count=count, size=len(data)))


def _exports_dir() -> Path:
    d = Path.home() / "Downloads"
    return d if d.is_dir() else Path.home()


def _safe_export(name: str) -> Path:
    """Resolve an export filename to a real path, or refuse.

    The open/reveal routes launch things through the OS, so they must never accept
    an arbitrary path from the page. Only a plain filename, only in the exports
    folder, only one this app writes.
    """
    if "/" in name or "\\" in name or not name.startswith("patient-records-")             or not name.endswith(".xlsx"):
        raise HTTPException(status_code=400, detail="Not an export file")
    target = (_exports_dir() / name).resolve()
    if target.parent != _exports_dir().resolve() or not target.is_file():
        raise HTTPException(status_code=404, detail=f"{name} not found")
    return target


@app.post("/records/open")
def records_open(request: Request, filename: str = F(""),
                 db: Session = Depends(get_db),
                 _=Depends(needs(perms.SUBMISSIONS_VIEW))):
    """Open the saved spreadsheet in Excel.

    The app runs in a pywebview window with no download manager, so the usual
    click-to-download does nothing. The server and the person clicking are on the
    same machine here, so launching the file locally is the honest equivalent.
    """
    target = _safe_export(filename)
    user = auth.current_user(request, db)
    log(db, "Export Opened", "records", filename, user_id=user.id if user else None)
    db.commit()
    try:
        os.startfile(target)                               # noqa: S606 - Windows
    except AttributeError:                                 # macOS / Linux
        subprocess.Popen(["xdg-open", str(target)])
    return RedirectResponse(f"/records/saved?f={filename}", status_code=303)


@app.post("/records/folder")
def records_folder(filename: str = F(""),
                   _=Depends(needs(perms.SUBMISSIONS_VIEW))):
    """Open File Explorer with the export selected."""
    target = _safe_export(filename)
    try:
        subprocess.Popen(["explorer", "/select,", str(target)])
    except FileNotFoundError:
        subprocess.Popen(["xdg-open", str(target.parent)])
    return RedirectResponse(f"/records/saved?f={filename}", status_code=303)


@app.get("/records/saved", response_class=HTMLResponse)
def records_saved(request: Request, f: str = "", db: Session = Depends(get_db),
                  _=Depends(needs(perms.SUBMISSIONS_VIEW))):
    """The confirmation page, reachable again after opening the file."""
    target = _safe_export(f)
    return render("records_saved.html", ctx(
        request, db, nav="records", path=str(target), filename=f,
        folder=str(_exports_dir()), count=db.query(Client).filter_by(archived=False).count(),
        size=target.stat().st_size))


@app.get("/send", response_class=HTMLResponse)
def send_page(request: Request, client: int | None = None,
              db: Session = Depends(get_db),
              _=Depends(needs(perms.SUBMISSIONS_SEND))):
    return render(
        "send.html",
        ctx(request, db, forms=db.query(Form).filter_by(is_active=True).all(),
            clients=db.query(Client).filter_by(archived=False).all(),
            preselect=client, backends=messaging.describe(db), nav="send"),
    )


def deliver(db: Session, request: Request, sub: Submission, channels: list[str]) -> None:
    """Attempt each requested channel, recording the outcome either way.

    A refusal to send is recorded as loudly as a send. "We texted them" and "we
    could not text them because they never consented" must both be answerable
    from the log months later.
    """
    client = sub.client
    base = str(request.base_url).rstrip("/")
    url = f"{base}/f/{sub.token}"

    for channel in channels:
        recipient = (client.email if channel == "email" else client.phone) or ""
        consented = client.allow_email if channel == "email" else client.allow_sms

        if not recipient:
            entry = ("skipped", f"No {channel} address on the client record")
        elif not consented:
            entry = ("skipped", f"No consent recorded for {channel} contact")
        else:
            msg = messaging.compose(
                channel, first_name=client.first_name, practice=config.load(),
                url=url, expires=sub.expires_at.date() if sub.expires_at else None,
                form_name=sub.form.name)
            msg.to = recipient
            backend = messaging.backend_for(channel, db)
            try:
                backend.send(msg)
                if isinstance(backend, messaging.FileBackend):
                    # Saying "sent" here would be false: the file backend writes to
                    # outbox.log and transmits nothing. Staff then wait for a message
                    # that is never coming, which is worse than an obvious failure.
                    entry = ("not sent", "No mail server configured - written to "
                                         "outbox.log. Copy the patient link instead.")
                else:
                    entry = ("sent", "")
            except messaging.SendFailed as exc:
                entry = ("failed", str(exc))

        status, detail = entry
        db.add(MessageLog(submission_id=sub.id, client_id=client.id, channel=channel,
                          recipient=recipient, status=status, detail=detail,
                          backend=messaging.backend_for(channel, db).name))
        log(db, f"Form Link {status.title()} ({channel})", "submission", sub.id)


@app.post("/send")
def do_send(request: Request, form_id: int = F(...), client_id: int = F(...),
            channels: list[str] = F([]), db: Session = Depends(get_db),
            _=Depends(needs(perms.SUBMISSIONS_SEND))):
    form = get_or_404(db, Form, form_id)

    # Sending the same form to the same patient again is usually a chase, not a
    # second questionnaire. Reuse the outstanding one rather than stacking a
    # third empty row on the list - three rows where one is expected reads as a
    # bug, and it is genuinely unclear which link the patient should use.
    #
    # An submission that already has answers is never reused. Re-sending a form
    # somebody completed is a real thing (an annual re-screen), and overwriting
    # their answers to save a row would be indefensible.
    sub = (db.query(Submission)
           .filter_by(form_id=form_id, client_id=client_id)
           .filter(Submission.status.in_([SubmissionStatus.sent,
                                          SubmissionStatus.opened,
                                          SubmissionStatus.partial]))
           .order_by(Submission.id.desc()).first())
    if sub and sub.answers:
        sub = None

    if sub:
        sub.token = new_token()                 # the old link stops working
        sub.form_version = form.version
        sub.sent_at = datetime.utcnow()
        sub.expires_at = datetime.utcnow() + LINK_LIFETIME
        sub.status = SubmissionStatus.sent
        log(db, "Form Re-sent (same outstanding request)", "submission", sub.id)
    else:
        sub = Submission(form_id=form_id, client_id=client_id, token=new_token(),
                         form_version=form.version,
                         expires_at=datetime.utcnow() + LINK_LIFETIME)
        db.add(sub)
        db.flush()
        log(db, "Form Sent", "submission", sub.id)
    if channels:
        deliver(db, request, sub, [c for c in channels if c in ("email", "sms")])
    db.commit()
    return RedirectResponse(f"/submissions/{sub.id}", status_code=303)


@app.post("/submissions/{sub_id}/resend")
def resend(sub_id: int, request: Request, channel: str = F("email"),
           db: Session = Depends(get_db), _=Depends(needs(perms.SUBMISSIONS_SEND))):
    sub = get_or_404(db, Submission, sub_id)
    if sub.is_open:
        deliver(db, request, sub, [channel])
        db.commit()
    return RedirectResponse(f"/submissions/{sub_id}", status_code=303)


@app.get("/outbox", response_class=HTMLResponse)
def outbox(request: Request, db: Session = Depends(get_db),
           _=Depends(needs(perms.SUBMISSIONS_VIEW))):
    return render("outbox.html", ctx(
        request, db, nav="outbox",
        rows=db.query(MessageLog).order_by(MessageLog.at.desc()).limit(200).all(),
        backends=messaging.describe(db)))


# ------------------------------------------------------------------- surveys
#
# Post-visit feedback, kept deliberately separate from the clinical record -
# see app/surveys.py. Sending one is SUBMISSIONS_SEND (the same permission that
# sends a form link); reading the results is SUBMISSIONS_VIEW, same as the rest
# of what a patient has sent back.

@app.get("/surveys", response_class=HTMLResponse)
def surveys_page(request: Request, status: str = "", client: int | None = None,
                 db: Session = Depends(get_db),
                 _=Depends(needs(perms.SUBMISSIONS_VIEW))):
    rows_q = (db.query(ExperienceSurvey)
             .options(joinedload(ExperienceSurvey.client))
             .order_by(ExperienceSurvey.sent_at.desc()))
    if status:
        try:
            rows_q = rows_q.filter(ExperienceSurvey.status == SurveyStatus(status))
        except ValueError:
            pass
    rows = rows_q.limit(500).all()

    rated = [s.rating_overall for s in rows if s.rating_overall]
    avg = round(sum(rated) / len(rated), 1) if rated else None

    return render("surveys.html", ctx(
        request, db, nav="surveys", rows=rows, status=status, avg=avg,
        rated_count=len(rated),
        clients=db.query(Client).filter_by(archived=False).order_by(Client.last_name).all(),
        preselect=client, backends=messaging.describe(db)))


@app.post("/surveys/send")
def surveys_send(request: Request, client_id: int = F(...),
                 channels: list[str] = F([]), db: Session = Depends(get_db),
                 _=Depends(needs(perms.SUBMISSIONS_SEND))):
    client_obj = get_or_404(db, Client, client_id)
    survey = ExperienceSurvey(client_id=client_obj.id, token=new_token(),
                              expires_at=datetime.utcnow() + surveys.LINK_LIFETIME)
    db.add(survey)
    db.flush()
    log(db, "Experience Survey Sent", "experience_survey", survey.id)
    if channels:
        surveys.deliver(db, request, survey, [c for c in channels if c in ("email", "sms")])
    db.commit()
    return RedirectResponse("/surveys", status_code=303)


@app.get("/submissions/{sub_id}", response_class=HTMLResponse)
def submission_detail(sub_id: int, request: Request, db: Session = Depends(get_db),
                      _=Depends(needs(perms.SUBMISSIONS_VIEW))):
    sub = get_or_404(db, Submission, sub_id)
    if sub.status == SubmissionStatus.submitted and not sub.read_by_staff:
        sub.read_by_staff = True
        cache.drop_nav()          # the badge must not still show this one
    log(db, "Submission Viewed", "submission", sub_id)
    db.commit()
    answered = {a.question_id: a for a in sub.answers}
    messages = (db.query(MessageLog).filter_by(submission_id=sub.id)
                .order_by(MessageLog.at.desc()).all())
    return render("submission_detail.html",
                  ctx(request, db, sub=sub, answered=answered, messages=messages,
                      base=str(request.base_url).rstrip("/"), nav="submissions"))


@app.get("/submissions", response_class=HTMLResponse)
def submissions(request: Request, db: Session = Depends(get_db),
                _=Depends(needs(perms.SUBMISSIONS_VIEW))):
    rows = db.query(Submission).order_by(Submission.sent_at.desc()).all()
    return render(
        "submissions.html", ctx(request, db, rows=rows, nav="submissions"))


@app.get("/events", response_class=HTMLResponse)
def events(request: Request, db: Session = Depends(get_db),
           _=Depends(needs(perms.EVENTS_VIEW))):
    rows = db.query(AuditEvent).order_by(AuditEvent.at.desc()).limit(200).all()
    return render(
        "events.html", ctx(request, db, rows=rows, nav="events"))


# -------------------------------------------------------------- patient screens

@app.get("/preview/{form_id}", response_class=HTMLResponse)
def preview(form_id: int, request: Request, page: int = 0,
            db: Session = Depends(get_db)):
    """Staff-facing preview. Same template as the real thing, nothing saved."""
    from .patient import pages_of, questions_on

    form = get_or_404(db, Form, form_id)
    pages = pages_of(form)
    page = max(0, min(page, len(pages)))
    return render("patient_form.html", {
        "request": request, "practice": config.load(), "sub": None, "form": form,
        "page": page, "pages": pages, "total": len(pages),
        "questions": questions_on(form, pages[page - 1]) if page >= 1 else [],
        "answers": {}, "percent": int(page / (len(pages) + 1) * 100),
        "consents": signable(form),
        "client_first_name": "[ClientFirstName]", "preview": True, "errors": [],
    })


app.include_router(patient_router)
app.include_router(recovery_router)
app.include_router(surveys.router)

# Seed at import so the app is populated however it's launched - desktop.py,
# uvicorn directly, or a test client (which never fires startup events).
@app.middleware("http")
async def restore_path(request, call_next):
    """Put back the URL the browser asked for.

    Vercel routes every request to this app through a rewrite, and that rewrite
    *replaces* the path: a browser asking for /setup arrives here as
    /api/index, with the original path in no header at all. Every route then
    misses, the auth middleware sees a path that is not on its public list and
    redirects to /setup, which arrives mangled the same way - and the browser
    stops with ERR_TOO_MANY_REDIRECTS.

    So vercel.json carries the real path through the rewrite as __vpath, and
    this puts it back. Registered last, which makes it the outermost middleware,
    so routing and the public-path list both read the corrected value. A local
    run never sets __vpath, so this does nothing there.
    """
    carried = request.query_params.get("__vpath")
    if carried and carried.startswith("/"):
        rest = [(k, v) for k, v in request.query_params.multi_items()
                if k != "__vpath"]
        request.scope["path"] = carried
        request.scope["raw_path"] = carried.encode()
        request.scope["query_string"] = urlencode(rest).encode()
    return await call_next(request)


seed()


# ---------------------------------------------------------------- the record
#
# Encounters, prescriptions, orders and charges. Everything here records, signs
# and prints; nothing transmits. See app/ehr.py for why that line is where it
# is, and why the screens repeat it.


@app.get("/clients/{client_id}/encounters/new", response_class=HTMLResponse)
def encounter_new(client_id: int, request: Request, template: str = "SOAP",
                  db: Session = Depends(get_db),
                  _=Depends(needs(perms.CLINICAL_EDIT))):
    client = get_or_404(db, Client, client_id)
    return render("encounter_edit.html", ctx(
        request, db, nav="clients", client=client, encounter=None,
        template=template if template in ehr.TEMPLATES else "SOAP",
        templates_available=list(ehr.TEMPLATES)))


@app.get("/encounters/{encounter_id}", response_class=HTMLResponse)
def encounter_view(encounter_id: int, request: Request,
                   db: Session = Depends(get_db),
                   _=Depends(needs(perms.CLIENTS_VIEW))):
    enc = get_or_404(db, ehr.Encounter, encounter_id)
    log(db, "Clinical note viewed", "encounter", encounter_id,
        user_id=getattr(auth.current_user(request, db), "id", None))
    db.commit()
    return render("encounter_view.html", ctx(
        request, db, nav="clients", encounter=enc, client=enc.client,
        note=request.session.pop("encounter_note", ""),
        error=request.session.pop("encounter_error", "")))


@app.get("/encounters/{encounter_id}/edit", response_class=HTMLResponse)
def encounter_edit(encounter_id: int, request: Request,
                   db: Session = Depends(get_db),
                   _=Depends(needs(perms.CLINICAL_EDIT))):
    enc = get_or_404(db, ehr.Encounter, encounter_id)
    if enc.is_signed:
        #  A signed note is not editable, and the route says so rather than
        #  rendering a form whose save would be refused. Offering an edit box
        #  for a locked record teaches people the lock is advisory.
        request.session["encounter_error"] = (
            "This note is signed. Signed notes cannot be edited - add an "
            "addendum instead, which is recorded after the original and names "
            "who wrote it.")
        return RedirectResponse(f"/encounters/{encounter_id}", status_code=303)
    return render("encounter_edit.html", ctx(
        request, db, nav="clients", client=enc.client, encounter=enc,
        template=enc.template, templates_available=list(ehr.TEMPLATES)))


@app.post("/clients/{client_id}/encounters")
async def encounter_save(client_id: int, request: Request,
                         db: Session = Depends(get_db),
                         _=Depends(needs(perms.CLINICAL_EDIT))):
    """Create or update a draft note.

    Reads the section fields generically so a template can gain a section
    without a second edit here - and refuses outright on a signed note, because
    the form is the last place a lock should be enforced, not the only one.
    """
    client = get_or_404(db, Client, client_id)
    form = await request.form()
    me = auth.current_user(request, db)

    enc_id = _int_or_none(form.get("encounter_id") or "")
    enc = db.get(ehr.Encounter, enc_id) if enc_id else None
    if enc and enc.is_signed:
        request.session["encounter_error"] = "That note is signed and cannot change."
        return RedirectResponse(f"/encounters/{enc.id}", status_code=303)

    template = form.get("template") or "SOAP"
    if template not in ehr.TEMPLATES:
        template = "SOAP"
    sections = {key: (form.get(f"s_{key}") or "").strip()
                for key, _label in ehr.TEMPLATES[template]}

    seen = _date_or_none(form.get("seen_on") or "") or date.today()
    if enc is None:
        enc = ehr.Encounter(client_id=client.id,
                            provider_id=getattr(me, "id", None))
        db.add(enc)
    enc.seen_on = seen
    enc.reason = (form.get("reason") or "").strip()[:255]
    enc.template = template
    enc.sections = json.dumps(sections)
    enc.updated_at = datetime.utcnow()
    db.flush()

    log(db, f"Clinical note saved (draft)", "encounter", enc.id,
        user_id=getattr(me, "id", None), ip=client_ip(request))
    db.commit()
    request.session["encounter_note"] = "Saved as a draft. Nothing is locked until you sign it."
    return RedirectResponse(f"/encounters/{enc.id}", status_code=303)


@app.post("/encounters/{encounter_id}/sign")
def encounter_sign(encounter_id: int, request: Request,
                   db: Session = Depends(get_db),
                   _=Depends(needs(perms.CLINICAL_EDIT))):
    """Sign a note, which locks it for good.

    The signature is the clinician's own name and the moment, stored on the row.
    There is deliberately no unsign: a record that can be unsigned, edited and
    re-signed is a record with no fixed point, and the fixed point is the entire
    reason a signature exists.
    """
    enc = get_or_404(db, ehr.Encounter, encounter_id)
    me = auth.current_user(request, db)
    if enc.is_signed:
        request.session["encounter_error"] = "Already signed."
        return RedirectResponse(f"/encounters/{encounter_id}", status_code=303)
    if not enc.filled:
        request.session["encounter_error"] = (
            "Nothing has been written yet. An empty signed note is a record "
            "that somebody attested to nothing.")
        return RedirectResponse(f"/encounters/{encounter_id}", status_code=303)

    enc.status = ehr.NoteStatus.signed
    enc.signed_at = datetime.utcnow()
    enc.signed_by = (me.name if me else "")[:160]
    log(db, "Clinical note signed", "encounter", enc.id,
        user_id=getattr(me, "id", None), ip=client_ip(request))
    db.commit()
    request.session["encounter_note"] = (
        f"Signed by {enc.signed_by}. It is now locked; corrections go in an addendum.")
    return RedirectResponse(f"/encounters/{encounter_id}", status_code=303)


@app.post("/encounters/{encounter_id}/addendum")
def encounter_addendum(encounter_id: int, request: Request, body: str = F(""),
                       db: Session = Depends(get_db),
                       _=Depends(needs(perms.CLINICAL_EDIT))):
    enc = get_or_404(db, ehr.Encounter, encounter_id)
    me = auth.current_user(request, db)
    if not body.strip():
        return RedirectResponse(f"/encounters/{encounter_id}", status_code=303)
    db.add(ehr.Addendum(encounter_id=enc.id, body=body.strip(),
                        written_by=(me.name if me else "")[:160]))
    log(db, "Note addendum added", "encounter", enc.id,
        user_id=getattr(me, "id", None), ip=client_ip(request))
    db.commit()
    request.session["encounter_note"] = "Addendum added after the original."
    return RedirectResponse(f"/encounters/{encounter_id}", status_code=303)


# --- prescriptions --------------------------------------------------------


@app.post("/clients/{client_id}/prescriptions")
def prescription_new(client_id: int, request: Request, drug: str = F(""),
                     strength: str = F(""), form_: str = F("", alias="form"),
                     sig: str = F(""), quantity: str = F(""),
                     refills: str = F("0"), days_supply: str = F(""),
                     pharmacy: str = F(""), controlled: str = F(""),
                     encounter_id: str = F(""), db: Session = Depends(get_db),
                     _=Depends(needs(perms.PRESCRIBE))):
    """Write a prescription. Behind PRESCRIBE, which an administrator does not have."""
    client = get_or_404(db, Client, client_id)
    me = auth.current_user(request, db)
    if not drug.strip():
        request.session["chart_error"] = "A prescription needs a drug name."
        return RedirectResponse(f"/clients/{client_id}#rx", status_code=303)

    rx = ehr.Prescription(
        client_id=client.id, encounter_id=_int_or_none(encounter_id),
        prescriber_id=getattr(me, "id", None),
        drug=drug.strip()[:255], strength=strength.strip()[:64],
        form=form_.strip()[:64], sig=sig.strip()[:500],
        quantity=quantity.strip()[:64], refills=_int_or_none(refills) or 0,
        days_supply=_int_or_none(days_supply), pharmacy=pharmacy.strip()[:255],
        is_controlled=bool(controlled))
    db.add(rx)
    db.flush()
    log(db, f"Prescription written: {rx.drug[:28]}", "client", client.id,
        user_id=getattr(me, "id", None), ip=client_ip(request))
    db.commit()
    request.session["chart_note"] = (
        f"{rx.display} saved as a draft. Sign it, then print it - this app "
        f"does not send prescriptions to a pharmacy.")
    return RedirectResponse(f"/clients/{client_id}#rx", status_code=303)


@app.post("/prescriptions/{rx_id}/sign")
def prescription_sign(rx_id: int, request: Request,
                      db: Session = Depends(get_db),
                      _=Depends(needs(perms.PRESCRIBE))):
    rx = get_or_404(db, ehr.Prescription, rx_id)
    me = auth.current_user(request, db)
    if rx.is_signed:
        return RedirectResponse(f"/clients/{rx.client_id}#rx", status_code=303)
    rx.status = ehr.RxStatus.signed
    rx.signed_at = datetime.utcnow()
    rx.signed_by = (me.name if me else "")[:160]
    log(db, f"Prescription signed: {rx.drug[:30]}", "client", rx.client_id,
        user_id=getattr(me, "id", None), ip=client_ip(request))
    db.commit()
    request.session["chart_note"] = (
        f"{rx.display} signed by {rx.signed_by}. Print it to hand to the patient.")
    return RedirectResponse(f"/clients/{rx.client_id}#rx", status_code=303)


@app.get("/prescriptions/{rx_id}/print", response_class=HTMLResponse)
def prescription_print(rx_id: int, request: Request,
                      db: Session = Depends(get_db),
                      _=Depends(needs(perms.PRESCRIBE))):
    """The printable prescription, and a record that it was printed.

    Printing is the closest thing to a transmission this app has, so it is
    recorded: a prescriber asking "did this actually go out?" deserves an
    answer, and the honest answer here is "a piece of paper was produced at
    this time".
    """
    rx = get_or_404(db, ehr.Prescription, rx_id)
    me = auth.current_user(request, db)
    if not rx.is_signed:
        request.session["chart_error"] = "Sign the prescription before printing it."
        return RedirectResponse(f"/clients/{rx.client_id}#rx", status_code=303)
    rx.printed_at = datetime.utcnow()
    log(db, f"Prescription printed: {rx.drug[:29]}", "client", rx.client_id,
        user_id=getattr(me, "id", None), ip=client_ip(request))
    db.commit()
    return render("prescription_print.html", ctx(
        request, db, rx=rx, client=rx.client))


@app.post("/prescriptions/{rx_id}/cancel")
def prescription_cancel(rx_id: int, request: Request,
                        db: Session = Depends(get_db),
                        _=Depends(needs(perms.PRESCRIBE))):
    rx = get_or_404(db, ehr.Prescription, rx_id)
    rx.status = ehr.RxStatus.cancelled
    log(db, f"Prescription cancelled: {rx.drug[:26]}", "client", rx.client_id,
        user_id=getattr(auth.current_user(request, db), "id", None))
    db.commit()
    return RedirectResponse(f"/clients/{rx.client_id}#rx", status_code=303)


# --- orders ---------------------------------------------------------------


@app.post("/clients/{client_id}/orders")
def order_new(client_id: int, request: Request, kind: str = F("lab"),
              name: str = F(""), reason: str = F(""), priority: str = F("Routine"),
              facility: str = F(""), db: Session = Depends(get_db),
              _=Depends(needs(perms.CLINICAL_EDIT))):
    client = get_or_404(db, Client, client_id)
    me = auth.current_user(request, db)
    if not name.strip():
        request.session["chart_error"] = "An order needs a test or study name."
        return RedirectResponse(f"/clients/{client_id}#orders", status_code=303)
    order = ehr.Order(client_id=client.id, ordered_by_id=getattr(me, "id", None),
                      kind=kind if kind in ("lab", "imaging") else "lab",
                      name=name.strip()[:255], reason=reason.strip()[:255],
                      priority=priority.strip()[:32] or "Routine",
                      facility=facility.strip()[:255],
                      status=ehr.OrderStatus.placed,
                      placed_at=datetime.utcnow())
    db.add(order)
    db.flush()
    log(db, f"Order placed: {order.name[:33]}", "client", client.id,
        user_id=getattr(me, "id", None), ip=client_ip(request))
    db.commit()
    request.session["chart_note"] = (
        f"{order.name} recorded. Print or hand the requisition over - this app "
        f"does not transmit orders to a laboratory.")
    return RedirectResponse(f"/clients/{client_id}#orders", status_code=303)


@app.post("/orders/{order_id}/result")
def order_result(order_id: int, request: Request, result: str = F(""),
                 abnormal: str = F(""), db: Session = Depends(get_db),
                 _=Depends(needs(perms.CLINICAL_EDIT))):
    order = get_or_404(db, ehr.Order, order_id)
    me = auth.current_user(request, db)
    order.result_text = result.strip()
    order.result_abnormal = bool(abnormal)
    order.result_at = datetime.utcnow()
    order.status = ehr.OrderStatus.resulted
    #  Recording a result does not mark it reviewed. They are different acts by
    #  possibly different people, and collapsing them is how a result is filed
    #  as seen by nobody - which is the failure orders modules get sued over.
    cache.drop("nav:unreviewed")
    log(db, f"Result recorded: {order.name[:31]}", "client", order.client_id,
        user_id=getattr(me, "id", None), ip=client_ip(request))
    db.commit()
    return RedirectResponse(f"/clients/{order.client_id}#orders", status_code=303)


@app.post("/orders/{order_id}/reviewed")
def order_reviewed(order_id: int, request: Request,
                   db: Session = Depends(get_db),
                   _=Depends(needs(perms.CLINICAL_EDIT))):
    order = get_or_404(db, ehr.Order, order_id)
    me = auth.current_user(request, db)
    order.reviewed_by = (me.name if me else "")[:160]
    order.reviewed_at = datetime.utcnow()
    cache.drop("nav:unreviewed")
    log(db, f"Result reviewed: {order.name[:31]}", "client", order.client_id,
        user_id=getattr(me, "id", None), ip=client_ip(request))
    db.commit()
    return RedirectResponse(f"/clients/{order.client_id}#orders", status_code=303)


@app.get("/results", response_class=HTMLResponse)
def results_inbox(request: Request, db: Session = Depends(get_db),
                  _=Depends(needs(perms.CLINICAL_EDIT))):
    """Results that arrived and nobody has said they have seen.

    The one screen an orders module exists for. A result sitting unread is the
    failure mode with real consequences, so it gets its own list rather than
    living as a badge on somebody's chart.
    """
    waiting = (db.query(ehr.Order)
               .filter(ehr.Order.status == ehr.OrderStatus.resulted,
                       ehr.Order.reviewed_at.is_(None))
               .order_by(ehr.Order.result_at).all())
    return render("results.html", ctx(request, db, nav="results", orders=waiting))


# --- charges --------------------------------------------------------------


@app.post("/clients/{client_id}/charges")
def charge_new(client_id: int, request: Request, cpt: str = F(""),
               description: str = F(""), icd10: str = F(""), units: str = F("1"),
               amount: str = F("0"), payer: str = F(""),
               encounter_id: str = F(""), db: Session = Depends(get_db),
               _=Depends(needs(perms.BILLING_EDIT))):
    client = get_or_404(db, Client, client_id)
    me = auth.current_user(request, db)
    if not cpt.strip():
        request.session["chart_error"] = "A charge needs a CPT code."
        return RedirectResponse(f"/clients/{client_id}#billing", status_code=303)
    try:
        money = Decimal((amount or "0").strip() or "0")
    except (InvalidOperation, ValueError):
        money = Decimal("0")
    charge = ehr.Charge(
        client_id=client.id, encounter_id=_int_or_none(encounter_id),
        cpt=cpt.strip()[:16], description=description.strip()[:255],
        icd10=icd10.strip()[:120], units=_int_or_none(units) or 1,
        amount=money, payer=payer.strip()[:160])
    db.add(charge)
    db.flush()
    log(db, f"Charge coded: {charge.cpt}", "client", client.id,
        user_id=getattr(me, "id", None), ip=client_ip(request))
    db.commit()
    request.session["chart_note"] = f"Charge {charge.cpt} added."
    return RedirectResponse(f"/clients/{client_id}#billing", status_code=303)


@app.post("/charges/{charge_id}/status")
def charge_status(charge_id: int, request: Request, status: str = F(""),
                  paid: str = F(""), db: Session = Depends(get_db),
                  _=Depends(needs(perms.BILLING_EDIT))):
    charge = get_or_404(db, ehr.Charge, charge_id)
    me = auth.current_user(request, db)
    try:
        charge.status = ehr.ChargeStatus(status)
    except ValueError:
        return RedirectResponse(f"/clients/{charge.client_id}#billing",
                                status_code=303)
    if charge.status == ehr.ChargeStatus.submitted:
        charge.submitted_at = datetime.utcnow()
    if charge.status == ehr.ChargeStatus.paid:
        try:
            charge.paid_amount = Decimal((paid or "0").strip() or "0")
        except (InvalidOperation, ValueError):
            charge.paid_amount = charge.amount
        charge.paid_at = datetime.utcnow()
    log(db, f"Charge {charge.cpt} -> {charge.status.value}", "client",
        charge.client_id, user_id=getattr(me, "id", None))
    db.commit()
    return RedirectResponse(f"/clients/{charge.client_id}#billing", status_code=303)


def _date_or_none(raw: str):
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        return None


# ------------------------------------------------------------------ documents
#
# Scans, faxes and outside records. Bytes are stored in the database rather than
# on disk - the hosted copy has a read-only filesystem, and a row pointing at a
# file nobody can open is worse than no row at all.


@app.get("/documents", response_class=HTMLResponse)
def documents_list(request: Request, show: str = "unfiled",
                   db: Session = Depends(get_db),
                   _=Depends(needs(perms.DOCUMENTS_VIEW))):
    """The document queue, unfiled first.

    Unfiled is the default view because it is the only one that represents
    work. A list of everything sorted by date is an archive; the question
    somebody opens this screen to answer is "what has come in that nobody has
    dealt with".
    """
    rows = db.query(documents.Document)
    if show == "unfiled":
        rows = rows.filter(documents.Document.client_id.is_(None))
    elif show == "unprocessed":
        rows = rows.filter(documents.Document.processed.is_(False))
    rows = rows.order_by(documents.Document.uploaded_at.desc()).limit(300).all()

    return render("documents.html", ctx(
        request, db, nav="documents", docs=rows, show=show,
        labels=documents.LABELS,
        clients=db.query(Client).filter_by(archived=False)
                 .order_by(Client.last_name).all(),
        unfiled=db.query(documents.Document)
                  .filter(documents.Document.client_id.is_(None)).count(),
        note=request.session.pop("document_note", ""),
        error=request.session.pop("document_error", "")))


@app.post("/documents/upload")
async def document_upload(request: Request, db: Session = Depends(get_db),
                          _=Depends(needs(perms.DOCUMENTS_VIEW))):
    """Take a file in, having actually looked at it.

    Three checks, in this order and for different reasons: a size cap so one
    upload cannot exhaust memory, a magic-number check because the declared
    media type comes from the client and a browser will say whatever it is told,
    and a non-empty check because an empty file is a failed scan and filing it
    silently means somebody believes the card is on record.
    """
    form = await request.form()
    upload = form.get("file")
    me = auth.current_user(request, db)
    back = (form.get("back") or "/documents").strip() or "/documents"

    if upload is None or not getattr(upload, "filename", ""):
        request.session["document_error"] = "No file was chosen."
        return RedirectResponse(back, status_code=303)

    #  Read with a cap rather than reading then measuring: reading first is how
    #  a large upload becomes a memory problem before anybody has checked it.
    data = await upload.read(documents.MAX_BYTES + 1)
    if len(data) > documents.MAX_BYTES:
        request.session["document_error"] = (
            f"That file is larger than "
            f"{documents.MAX_BYTES // (1024 * 1024)} MB. Scan at a lower "
            f"resolution, or split it.")
        return RedirectResponse(back, status_code=303)
    if not data:
        request.session["document_error"] = (
            "That file is empty - usually a scan that did not complete. "
            "Nothing was saved, because a blank record of an insurance card "
            "is worse than none.")
        return RedirectResponse(back, status_code=303)

    media = documents.sniff(data[:64], (upload.content_type or "").split(";")[0])
    if media is None:
        request.session["document_error"] = (
            "That file type is not accepted. PDFs, images and plain text only "
            "- and the check is on the file's own contents, not its name.")
        return RedirectResponse(back, status_code=303)

    client_id = _int_or_none(form.get("client_id") or "")
    if client_id and db.get(Client, client_id) is None:
        client_id = None

    doc = documents.Document(
        client_id=client_id,
        name=(form.get("name") or "").strip()[:255]
             or documents.safe_name(upload.filename),
        file_name=documents.safe_name(upload.filename),
        media_type=media, size_bytes=len(data), sha256=documents.digest(data),
        content=data,
        label=(form.get("label") or "Other").strip()[:64],
        received_from=(form.get("received_from") or "").strip()[:255],
        notes=(form.get("notes") or "").strip(),
        uploaded_by=(me.name if me else "")[:160])
    db.add(doc)
    db.flush()

    #  A document arriving is a PHI event. Naming the label rather than the
    #  filename keeps the audit line useful without putting a patient's name
    #  into it twice over.
    cache.drop("nav:unfiled")
    log(db, f"Document received: {doc.label[:30]}", "document", doc.id,
        user_id=getattr(me, "id", None), ip=client_ip(request))
    db.commit()

    same = (db.query(documents.Document)
            .filter(documents.Document.sha256 == doc.sha256,
                    documents.Document.id != doc.id).count())
    request.session["document_note"] = (
        f"{doc.name} stored ({doc.size_human})."
        + (f" Note: {same} other document(s) have identical contents."
           if same else "")
        + ("" if client_id else " It is unfiled - attach it to a patient when "
                                "you know whose it is."))
    return RedirectResponse(back, status_code=303)


@app.get("/documents/{doc_id}/file")
def document_file(doc_id: int, request: Request, download: int = 0,
                  db: Session = Depends(get_db),
                  _=Depends(needs(perms.DOCUMENTS_VIEW))):
    """Serve the stored bytes.

    Opening a document is a read of protected information and is logged as one.
    The Content-Disposition filename is rebuilt from a sanitised name rather
    than echoed, because a filename is attacker-controlled input and a header is
    a bad place to discover that.
    """
    doc = get_or_404(db, documents.Document, doc_id)
    me = auth.current_user(request, db)
    log(db, f"Document opened: {doc.label[:32]}", "document", doc.id,
        user_id=getattr(me, "id", None), ip=client_ip(request))
    db.commit()

    disposition = "attachment" if download else "inline"
    name = documents.download_name(doc)
    return Response(
        content=doc.content, media_type=doc.media_type or "application/octet-stream",
        headers={"Content-Disposition": f'{disposition}; filename="{name}"',
                 #  Served from our own origin, so a stored SVG or HTML would
                 #  run as us. Neither is in the allowed list, and this is the
                 #  second lock on the same door.
                 "X-Content-Type-Options": "nosniff",
                 "Content-Security-Policy": "default-src 'none'; img-src 'self'"})


@app.post("/documents/{doc_id}/file-to")
def document_file_to(doc_id: int, request: Request, client_id: str = F(""),
                     db: Session = Depends(get_db),
                     _=Depends(needs(perms.DOCUMENTS_VIEW))):
    """Attach an unfiled document to a patient, or detach it again."""
    doc = get_or_404(db, documents.Document, doc_id)
    me = auth.current_user(request, db)
    chosen = _int_or_none(client_id)
    target = db.get(Client, chosen) if chosen else None
    doc.client_id = target.id if target else None
    cache.drop("nav:unfiled")
    log(db, f"Document {'filed' if target else 'unfiled'}: {doc.label[:26]}",
        "document", doc.id, user_id=getattr(me, "id", None), ip=client_ip(request))
    db.commit()
    request.session["document_note"] = (
        f"{doc.name} filed to {target.name}." if target
        else f"{doc.name} is unfiled again.")
    return RedirectResponse(request.headers.get("referer") or "/documents",
                            status_code=303)


@app.post("/documents/{doc_id}/processed")
def document_processed(doc_id: int, request: Request,
                       db: Session = Depends(get_db),
                       _=Depends(needs(perms.DOCUMENTS_VIEW))):
    doc = get_or_404(db, documents.Document, doc_id)
    me = auth.current_user(request, db)
    doc.processed = not doc.processed
    doc.processed_by = (me.name if me else "")[:160] if doc.processed else ""
    doc.processed_at = datetime.utcnow() if doc.processed else None
    log(db, f"Document {'processed' if doc.processed else 'reopened'}: "
            f"{doc.label[:20]}", "document", doc.id,
        user_id=getattr(me, "id", None))
    db.commit()
    return RedirectResponse(request.headers.get("referer") or "/documents",
                            status_code=303)


@app.post("/documents/{doc_id}/delete")
def document_delete(doc_id: int, request: Request,
                    db: Session = Depends(get_db),
                    _=Depends(needs(perms.USERS_MANAGE))):
    """Delete a document. Deliberately the narrowest permission here.

    Everything else in this module is front-desk work; destroying a received
    record is not. A misfiled document should be re-filed, not deleted, and the
    only legitimate deletions are duplicates and things scanned in error.
    """
    doc = get_or_404(db, documents.Document, doc_id)
    me = auth.current_user(request, db)
    name = doc.name
    log(db, f"Document deleted: {doc.label[:31]}", "document", doc.id,
        user_id=getattr(me, "id", None), ip=client_ip(request))
    db.delete(doc)
    db.commit()
    request.session["document_note"] = f"{name} was deleted."
    return RedirectResponse("/documents", status_code=303)


# -------------------------------------------------------------------- billing
#
# Claims and payments. Nothing transmits: a claim reaches a payer through a
# clearinghouse contract, which is a procurement step. What this does is track
# what was sent, what came back, and what is still owed - which is the part a
# biller reconciles against a bank statement.


def _money(raw: str) -> Decimal:
    try:
        return Decimal((raw or "0").strip() or "0")
    except (InvalidOperation, ValueError):
        return Decimal("0")


@app.post("/charges/{charge_id}/claims")
def claim_new(charge_id: int, request: Request, payer: str = F(""),
              claim_number: str = F(""), replaces_id: str = F(""),
              db: Session = Depends(get_db),
              _=Depends(needs(perms.BILLING_EDIT))):
    """Prepare a claim for this charge.

    `replaces` carries the correction chain: a rejected claim is fixed and
    resent, and the replacement points back at the original so the history
    reads as one story rather than three unrelated rows.
    """
    charge = get_or_404(db, ehr.Charge, charge_id)
    me = auth.current_user(request, db)
    previous = db.get(ehr.Claim, _int_or_none(replaces_id) or 0)

    claim = ehr.Claim(
        charge_id=charge.id, client_id=charge.client_id,
        payer=(payer.strip() or charge.payer or "")[:160],
        claim_number=claim_number.strip()[:64],
        billed=charge.amount or 0,
        replaces_id=previous.id if previous else None,
        created_by=(me.name if me else "")[:160])
    db.add(claim)
    db.flush()
    log(db, f"Claim prepared for charge {charge.cpt}", "claim", claim.id,
        user_id=getattr(me, "id", None), ip=client_ip(request))
    db.commit()
    request.session["chart_note"] = (
        f"Claim prepared for {charge.cpt}."
        + (" It replaces the earlier one." if previous else "")
        + " Nothing has been sent - hand it to whoever files claims.")
    return RedirectResponse(f"/clients/{charge.client_id}#billing", status_code=303)


@app.post("/claims/{claim_id}/status")
def claim_status(claim_id: int, request: Request, status: str = F(""),
                 allowed: str = F(""), paid: str = F(""),
                 patient_responsibility: str = F(""), adjustment: str = F(""),
                 denial_code: str = F(""), denial_reason: str = F(""),
                 db: Session = Depends(get_db),
                 _=Depends(needs(perms.BILLING_EDIT))):
    """Record what the payer said.

    Rejected and denied are kept apart deliberately - one is a format problem
    to correct and resend, the other a coverage decision to appeal or write
    off. A practice that treats them alike appeals format errors and resends
    coverage decisions, and does neither well.
    """
    claim = get_or_404(db, ehr.Claim, claim_id)
    me = auth.current_user(request, db)
    try:
        claim.status = ehr.ClaimStatus(status)
    except ValueError:
        return RedirectResponse(f"/clients/{claim.client_id}#billing",
                                status_code=303)

    if claim.status == ehr.ClaimStatus.submitted and not claim.submitted_on:
        claim.submitted_on = date.today()
    if claim.status in (ehr.ClaimStatus.paid, ehr.ClaimStatus.denied,
                        ehr.ClaimStatus.rejected, ehr.ClaimStatus.accepted):
        claim.responded_on = date.today()

    for field, raw in (("allowed", allowed), ("paid", paid),
                       ("patient_responsibility", patient_responsibility),
                       ("adjustment", adjustment)):
        if (raw or "").strip():
            setattr(claim, field, _money(raw))
    if denial_code.strip():
        claim.denial_code = denial_code.strip()[:32]
    if denial_reason.strip():
        claim.denial_reason = denial_reason.strip()[:255]

    #  A paid claim posts its payment automatically, once. Asking a biller to
    #  record the same figure twice is how the claim and the ledger drift
    #  apart, and the drift is only ever found at reconciliation.
    if claim.status == ehr.ClaimStatus.paid and float(claim.paid or 0) > 0:
        already = (db.query(ehr.Payment)
                   .filter_by(claim_id=claim.id,
                              source=ehr.PaymentSource.payer).first())
        if already is None:
            db.add(ehr.Payment(
                client_id=claim.client_id, charge_id=claim.charge_id,
                claim_id=claim.id, amount=claim.paid,
                source=ehr.PaymentSource.payer, method="Insurance payment",
                reference=claim.claim_number,
                note=f"Posted automatically from claim {claim.id}",
                posted_by=(me.name if me else "")[:160]))
        if float(claim.adjustment or 0) > 0:
            seen = (db.query(ehr.Payment)
                    .filter_by(claim_id=claim.id,
                               source=ehr.PaymentSource.adjustment).first())
            if seen is None:
                db.add(ehr.Payment(
                    client_id=claim.client_id, charge_id=claim.charge_id,
                    claim_id=claim.id, amount=claim.adjustment,
                    source=ehr.PaymentSource.adjustment,
                    method="Contractual adjustment",
                    note="Contractual - not billable to the patient",
                    posted_by=(me.name if me else "")[:160]))

    log(db, f"Claim {claim.id} -> {claim.status.value}", "claim", claim.id,
        user_id=getattr(me, "id", None), ip=client_ip(request))
    db.commit()
    return RedirectResponse(f"/clients/{claim.client_id}#billing", status_code=303)


@app.post("/clients/{client_id}/payments")
def payment_new(client_id: int, request: Request, amount: str = F(""),
                source: str = F("patient"), method: str = F(""),
                reference: str = F(""), received_on: str = F(""),
                charge_id: str = F(""), note: str = F(""),
                db: Session = Depends(get_db),
                _=Depends(needs(perms.BILLING_EDIT))):
    """Record money received, or an adjustment.

    The charge is optional: somebody pays at the desk before the visit is
    coded, and refusing to record that until there is a charge to attach it to
    means it does not get recorded at all.
    """
    client = get_or_404(db, Client, client_id)
    me = auth.current_user(request, db)
    value = _money(amount)
    if value == 0:
        request.session["chart_error"] = "A payment needs an amount."
        return RedirectResponse(f"/clients/{client_id}#billing", status_code=303)
    try:
        kind = ehr.PaymentSource(source)
    except ValueError:
        kind = ehr.PaymentSource.patient

    payment = ehr.Payment(
        client_id=client.id, charge_id=_int_or_none(charge_id),
        amount=value, source=kind, method=method.strip()[:32],
        reference=reference.strip()[:120],
        received_on=_date_or_none(received_on) or date.today(),
        note=note.strip()[:255], posted_by=(me.name if me else "")[:160])
    db.add(payment)
    db.flush()
    log(db, f"Payment posted: {value} ({kind.value})", "client", client.id,
        user_id=getattr(me, "id", None), ip=client_ip(request))
    db.commit()
    request.session["chart_note"] = f"{value} recorded."
    return RedirectResponse(f"/clients/{client_id}#billing", status_code=303)


@app.post("/payments/{payment_id}/delete")
def payment_delete(payment_id: int, request: Request,
                   db: Session = Depends(get_db),
                   _=Depends(needs(perms.BILLING_EDIT))):
    """Remove a mis-posted payment.

    Deleting rather than reversing, because this is a small practice ledger
    and a reversal entry nobody can explain is worse than a corrected one - but
    the audit log keeps the amount, so the removal itself is not invisible.
    """
    payment = get_or_404(db, ehr.Payment, payment_id)
    me = auth.current_user(request, db)
    log(db, f"Payment removed: {payment.amount}", "client", payment.client_id,
        user_id=getattr(me, "id", None), ip=client_ip(request))
    client_id = payment.client_id
    db.delete(payment)
    db.commit()
    return RedirectResponse(f"/clients/{client_id}#billing", status_code=303)


@app.get("/billing", response_class=HTMLResponse)
def billing_dashboard(request: Request, db: Session = Depends(get_db),
                      _=Depends(needs(perms.BILLING_EDIT))):
    """What is owed, and what needs a human.

    Two lists rather than one dashboard of totals. A number on its own is a
    number; the questions a biller actually has are "which claims came back
    badly" and "what is still outstanding and how old is it".
    """
    charges = (db.query(ehr.Charge)
               .options(selectinload(ehr.Charge.payments),
                        selectinload(ehr.Charge.claims),
                        joinedload(ehr.Charge.client))
               .order_by(ehr.Charge.service_on.desc()).all())

    outstanding = [c for c in charges if c.outstanding > 0.005]
    problems = [c for c in db.query(ehr.Claim)
                .options(joinedload(ehr.Claim.client))
                .filter(ehr.Claim.status.in_(
                    [ehr.ClaimStatus.rejected, ehr.ClaimStatus.denied]))
                .order_by(ehr.Claim.responded_on.desc()).all()]

    #  Ageing buckets, because "how much is outstanding" is a much less useful
    #  question than "how much has been outstanding for more than ninety days".
    today = date.today()
    buckets = {"0-30": 0.0, "31-60": 0.0, "61-90": 0.0, "90+": 0.0}
    for charge in outstanding:
        days = (today - charge.service_on).days
        key = ("0-30" if days <= 30 else "31-60" if days <= 60
               else "61-90" if days <= 90 else "90+")
        buckets[key] += charge.outstanding

    return render("billing.html", ctx(
        request, db, nav="billing", outstanding=outstanding, problems=problems,
        buckets=buckets,
        billed=sum(float(c.amount or 0) for c in charges),
        received=sum(c.received for c in charges),
        owed=sum(c.outstanding for c in outstanding)))


# --- patient collections and statements -----------------------------------


def _patient_balances(db):
    """Every patient with money outstanding, and how old the oldest charge is.

    Built from charges and their payments rather than a stored balance. A
    balance column would be one more thing to keep in step, and the day it
    drifts is the day somebody is chased for money they have already paid.
    """
    charges = (db.query(ehr.Charge)
               .options(selectinload(ehr.Charge.payments),
                        joinedload(ehr.Charge.client))
               .all())
    today = date.today()
    rows: dict[int, dict] = {}
    for charge in charges:
        owed = charge.outstanding
        if owed <= 0.005 or charge.client is None:
            continue
        row = rows.setdefault(charge.client_id, {
            "client": charge.client, "owed": 0.0, "charges": 0,
            "oldest": charge.service_on})
        row["owed"] += owed
        row["charges"] += 1
        row["oldest"] = min(row["oldest"], charge.service_on)
    for row in rows.values():
        row["days"] = (today - row["oldest"]).days
    return sorted(rows.values(), key=lambda r: -r["days"])


@app.get("/billing/patients", response_class=HTMLResponse)
def patient_collections(request: Request, db: Session = Depends(get_db),
                        _=Depends(needs(perms.BILLING_EDIT))):
    """Who owes money, oldest first.

    Ordered by age rather than amount on purpose: a small balance outstanding
    for four months is a collections problem, and a large one from last week is
    simply a claim that has not come back yet.
    """
    balances = _patient_balances(db)
    recent = (db.query(ehr.Statement)
              .options(joinedload(ehr.Statement.client))
              .order_by(ehr.Statement.id.desc()).limit(50).all())
    return render("collections.html", ctx(
        request, db, nav="billing", balances=balances, statements=recent,
        methods=ehr.STATEMENT_METHODS,
        total=sum(r["owed"] for r in balances),
        note=request.session.pop("billing_note", ""),
        error=request.session.pop("billing_error", "")))


@app.post("/clients/{client_id}/statements")
def statement_new(client_id: int, request: Request, kind: str = F("initial"),
                  amount: str = F(""), method: str = F("Post"),
                  due_on: str = F(""), detail: str = F(""),
                  db: Session = Depends(get_db),
                  _=Depends(needs(perms.BILLING_EDIT))):
    """Produce a statement for what this patient owes.

    The amount defaults to the current balance but is editable, because a
    practice legitimately bills part of it - a payment plan, or holding back a
    line that is still with the insurer. Forcing the full balance would mean
    the statement and the arrangement disagree.
    """
    client = get_or_404(db, Client, client_id)
    me = auth.current_user(request, db)

    owed = sum(r["owed"] for r in _patient_balances(db)
               if r["client"].id == client.id)
    value = _money(amount) if (amount or "").strip() else Decimal(str(round(owed, 2)))
    if value <= 0:
        request.session["billing_error"] = (
            f"{client.first_name} owes nothing, so there is nothing to bill. "
            f"A statement for zero is a letter that confuses the patient and "
            f"generates a phone call.")
        return RedirectResponse("/billing/patients", status_code=303)

    try:
        which = ehr.StatementKind(kind)
    except ValueError:
        which = ehr.StatementKind.initial

    statement = ehr.Statement(
        client_id=client.id, kind=which, amount=value,
        balance_at_send=Decimal(str(round(owed, 2))),
        method=method.strip()[:32] or "Post",
        due_on=_date_or_none(due_on), detail=detail.strip()[:255],
        created_by=(me.name if me else "")[:160])
    db.add(statement)
    db.flush()
    log(db, f"{statement.label} prepared: {value}", "client", client.id,
        user_id=getattr(me, "id", None), ip=client_ip(request))
    db.commit()
    request.session["billing_note"] = (
        f"{statement.label} for {value} prepared. Nothing has been sent - "
        f"mark it sent once it actually goes out.")
    return RedirectResponse("/billing/patients", status_code=303)


@app.post("/statements/{statement_id}/status")
def statement_status(statement_id: int, request: Request, status: str = F(""),
                     db: Session = Depends(get_db),
                     _=Depends(needs(perms.BILLING_EDIT))):
    """Record what happened to a statement.

    Marked by a person, never inferred. This app does not post letters or send
    email on its own, so a status it set itself would be a claim nobody
    verified - and "delivered" is exactly the claim that matters when a patient
    says they never received the bill.
    """
    statement = get_or_404(db, ehr.Statement, statement_id)
    me = auth.current_user(request, db)
    try:
        statement.status = ehr.Delivery(status)
    except ValueError:
        return RedirectResponse("/billing/patients", status_code=303)
    if statement.is_out and not statement.sent_on:
        statement.sent_on = date.today()
    log(db, f"Statement {statement.id} -> {statement.status.value}", "client",
        statement.client_id, user_id=getattr(me, "id", None))
    db.commit()
    return RedirectResponse("/billing/patients", status_code=303)


# --- the billing worklists ------------------------------------------------


@app.get("/billing/charges", response_class=HTMLResponse)
def billing_charges(request: Request, q: str = "", status: str = "",
                    db: Session = Depends(get_db),
                    _=Depends(needs(perms.BILLING_EDIT))):
    """Every charge, with the transitions its current status actually allows.

    The buttons come from CHARGE_NEXT rather than from the template deciding
    what to draw. One place defines the workflow, so a route can refuse a
    transition that was never offered, and nobody has to keep a template and a
    state machine in step by hand.
    """
    rows = (db.query(ehr.Charge)
            .options(joinedload(ehr.Charge.client),
                     selectinload(ehr.Charge.payments)))
    if status:
        try:
            rows = rows.filter(ehr.Charge.status == ehr.ChargeStatus(status))
        except ValueError:
            pass
    charges = rows.order_by(ehr.Charge.service_on.desc(),
                            ehr.Charge.id.desc()).all()
    term = q.strip().lower()
    if term:
        charges = [c for c in charges
                   if term in (c.client.name if c.client else "").lower()
                   or term in (c.cpt or "").lower()
                   or term in (c.description or "").lower()]

    return render("billing_charges.html", ctx(
        request, db, nav="billing", charges=charges, q=q, status=status,
        next_steps=ehr.CHARGE_NEXT, statuses=list(ehr.ChargeStatus),
        clients=db.query(Client).filter_by(archived=False)
                 .order_by(Client.last_name).all(),
        cpts=ehr.COMMON_CPT,
        note=request.session.pop("billing_note", ""),
        error=request.session.pop("billing_error", "")))


@app.post("/charges/{charge_id}/advance")
def charge_advance(charge_id: int, request: Request, to: str = F(""),
                   db: Session = Depends(get_db),
                   _=Depends(needs(perms.BILLING_EDIT))):
    """Move a charge along its workflow, refusing anything not allowed.

    Checked against CHARGE_NEXT rather than accepting whatever was posted. A
    status field that takes any value it is given is how an unapproved charge
    reaches a payer - the button was never on screen, but the form still is.
    """
    charge = get_or_404(db, ehr.Charge, charge_id)
    me = auth.current_user(request, db)
    try:
        wanted = ehr.ChargeStatus(to)
    except ValueError:
        request.session["billing_error"] = "That is not a charge status."
        return RedirectResponse("/billing/charges", status_code=303)

    allowed = [s for s, _label in ehr.CHARGE_NEXT.get(charge.status, [])]
    if wanted not in allowed:
        request.session["billing_error"] = (
            f"A {charge.status.value.replace('_', ' ')} charge cannot go "
            f"straight to {wanted.value.replace('_', ' ')}.")
        return RedirectResponse("/billing/charges", status_code=303)

    charge.status = wanted
    if wanted == ehr.ChargeStatus.submitted:
        charge.submitted_at = datetime.utcnow()
    log(db, f"Charge {charge.cpt} -> {wanted.value}", "client",
        charge.client_id, user_id=getattr(me, "id", None), ip=client_ip(request))
    db.commit()
    request.session["billing_note"] = (
        f"{charge.cpt} is now {wanted.value.replace('_', ' ')}.")
    return RedirectResponse("/billing/charges", status_code=303)


@app.get("/billing/insurance", response_class=HTMLResponse)
def insurance_collections(request: Request, show: str = "all", q: str = "",
                          db: Session = Depends(get_db),
                          _=Depends(needs(perms.BILLING_EDIT))):
    """Claims by what the payer did with them."""
    groups = {
        "all": None,
        "rejected": [ehr.ClaimStatus.rejected],
        "denied": [ehr.ClaimStatus.denied],
        "waiting": [ehr.ClaimStatus.submitted, ehr.ClaimStatus.accepted,
                    ehr.ClaimStatus.waiting_adjudication],
        "investigate": [ehr.ClaimStatus.needs_investigation],
        "paid": [ehr.ClaimStatus.paid],
    }
    rows = (db.query(ehr.Claim)
            .options(joinedload(ehr.Claim.client), joinedload(ehr.Claim.charge)))
    wanted = groups.get(show)
    if wanted:
        rows = rows.filter(ehr.Claim.status.in_(wanted))
    claims = rows.order_by(ehr.Claim.id.desc()).all()
    term = q.strip().lower()
    if term:
        claims = [c for c in claims
                  if term in (c.client.name if c.client else "").lower()
                  or term in (c.payer or "").lower()]

    counts = {}
    for key, statuses in groups.items():
        query = db.query(func.count(ehr.Claim.id))
        if statuses:
            query = query.filter(ehr.Claim.status.in_(statuses))
        counts[key] = query.scalar() or 0

    return render("billing_insurance.html", ctx(
        request, db, nav="billing", claims=claims, show=show, q=q,
        counts=counts))


@app.get("/billing/analytics", response_class=HTMLResponse)
def billing_analytics(request: Request, db: Session = Depends(get_db),
                      _=Depends(needs(perms.BILLING_EDIT))):
    """Gross charges against what was actually collected, by month.

    Two figures side by side rather than one, because gross charges on their
    own flatter a practice: billing three hundred and collecting a hundred and
    twenty is a different business from billing three hundred and collecting
    two ninety, and a single "revenue" line cannot tell them apart.
    """
    charges = (db.query(ehr.Charge)
               .options(selectinload(ehr.Charge.payments)).all())

    months: dict[str, dict] = {}
    for charge in charges:
        key = charge.service_on.strftime("%Y-%m")
        row = months.setdefault(key, {"gross": 0.0, "net": 0.0})
        row["gross"] += float(charge.amount or 0)
        row["net"] += charge.received
    series = [{"month": k, **v} for k, v in sorted(months.items())]
    peak = max([max(r["gross"], r["net"]) for r in series], default=0) or 1

    visits = db.query(func.count(ehr.Encounter.id)).scalar() or 0
    gross = sum(float(c.amount or 0) for c in charges)
    net = sum(c.received for c in charges)
    return render("billing_analytics.html", ctx(
        request, db, nav="billing", series=series, peak=peak,
        visits=visits, gross=gross, net=net,
        collection_rate=(net / gross * 100) if gross else 0))


@app.get("/billing/pay", response_class=HTMLResponse)
def billing_pay(request: Request, db: Session = Depends(get_db),
                _=Depends(needs(perms.BILLING_EDIT))):
    return render("billing_pay.html", ctx(
        request, db, nav="billing",
        clients=db.query(Client).filter_by(archived=False)
                 .order_by(Client.last_name).all(),
        result=request.session.pop("pay_result", None),
        note=request.session.pop("billing_note", ""),
        error=request.session.pop("billing_error", "")))


def _luhn(number: str) -> bool:
    digits = [int(c) for c in number if c.isdigit()]
    if len(digits) < 12:
        return False
    total, parity = 0, len(digits) % 2
    for i, digit in enumerate(digits):
        if i % 2 == parity:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


@app.post("/billing/pay")
def billing_pay_run(request: Request, client_id: str = F(""),
                    amount: str = F(""), card: str = F(""),
                    exp_month: str = F(""), exp_year: str = F(""),
                    cvc: str = F(""), db: Session = Depends(get_db),
                    _=Depends(needs(perms.BILLING_EDIT))):
    """A demonstration card payment. No processor is contacted, ever.

    The outcome is decided by a Luhn check on the number typed in, so the
    screen can show both a success and a failure on demand. Nothing about the
    card is stored beyond the last four digits - not the number, not the CVC,
    not even hashed. A demo that keeps a full PAN is a demo that has put the
    practice inside PCI scope for no benefit at all.
    """
    client = db.get(Client, _int_or_none(client_id) or 0)
    value = _money(amount)
    if client is None or value <= 0:
        request.session["billing_error"] = "Choose a patient and an amount."
        return RedirectResponse("/billing/pay", status_code=303)

    digits = "".join(c for c in card if c.isdigit())
    ok = _luhn(digits)
    last4 = digits[-4:] if len(digits) >= 4 else ""
    me = auth.current_user(request, db)

    if ok:
        db.add(ehr.Payment(
            client_id=client.id, amount=value,
            source=ehr.PaymentSource.patient, method="Card",
            reference=f"demo ****{last4}",
            note="DEMONSTRATION - no real transaction was processed",
            posted_by=(me.name if me else "")[:160]))
        log(db, f"Demo card payment {value} (no real transaction)", "client",
            client.id, user_id=getattr(me, "id", None), ip=client_ip(request))
        db.commit()

    request.session["pay_result"] = {
        "ok": ok, "amount": f"{value:.2f}", "last4": last4,
        "patient": client.name,
        "detail": ("Recorded as a patient payment. No processor was contacted."
                   if ok else
                   "The card number failed its checksum, so this demonstration "
                   "reports a decline. Nothing was recorded."),
    }
    return RedirectResponse("/billing/pay", status_code=303)


# ------------------------------------------------------------------ reports
#
# Read-only cross-cutting views. Every one of these is a query that already
# exists somewhere else in the app, reassembled as a list rather than a chart -
# the point of a report is that it is the same facts, seen across everybody
# instead of one patient at a time.


@app.get("/reports", response_class=HTMLResponse)
def reports_home(request: Request, _=Depends(needs(perms.SUBMISSIONS_VIEW))):
    return RedirectResponse("/reports/patients", status_code=303)


def _last_contact_for_all(db) -> dict[int, datetime]:
    """The most recent contact date for every patient, in three queries total.

    A per-patient version of this - one query each for submissions, messages
    and appointments - is the exact N+1 shape the caseload screen had before
    records_for_many fixed it: seven patients hides it, a real practice's worth
    does not. So this reads each table once, grouped by client, and folds the
    three maxima together in Python.

    Not a stored field either way - a stored "last contact" drifts the moment
    somebody adds a new kind of contact and forgets to update it there too.
    """
    latest: dict[int, datetime] = {}

    def fold(client_id, when):
        if client_id is not None and when is not None:
            if client_id not in latest or when > latest[client_id]:
                latest[client_id] = when

    for client_id, when in (db.query(Submission.client_id,
                                     func.max(Submission.sent_at))
                            .group_by(Submission.client_id).all()):
        fold(client_id, when)
    for client_id, when in (db.query(MessageLog.client_id, func.max(MessageLog.at))
                            .group_by(MessageLog.client_id).all()):
        fold(client_id, when)
    for client_id, when in (db.query(schedule.Appointment.client_id,
                                     func.max(schedule.Appointment.on_day))
                            .group_by(schedule.Appointment.client_id).all()):
        if when is not None:
            fold(client_id, datetime.combine(when, datetime.min.time()))
    return latest


@app.get("/reports/patients", response_class=HTMLResponse)
def report_patients(request: Request, trial_id: str = "", verdict: str = "",
                    db: Session = Depends(get_db),
                    _=Depends(needs(perms.SUBMISSIONS_VIEW))):
    """Every patient, their trial screening verdict, and who is looking after them.

    The same batching as My Patients and the trial detail screen - eager-load
    what the screening reads and fetch the whole archive in one query - because
    this is the same N+1 shape at the same scale, and the fix that mattered
    there matters here for the same reason.
    """
    clients = (db.query(Client).filter_by(archived=False)
               .options(selectinload(Client.medications),
                        selectinload(Client.submissions_list)
                        .selectinload(Submission.answers)
                        .joinedload(Answer.question),
                        joinedload(Client.provider))
               .order_by(Client.last_name).all())
    trials_all = db.query(Trial).order_by(Trial.is_active.desc(), Trial.name).all()
    chosen = db.get(Trial, _int_or_none(trial_id)) if trial_id else (
        next((t for t in trials_all if t.is_active), trials_all[0] if trials_all else None))

    archives = records_for_many(db, (c.hospital_id for c in clients))
    contacts = _last_contact_for_all(db)
    rows = []
    for c in clients:
        screening = (trials.evaluate(c, chosen, archives.get(c.hospital_id or 0, []))
                     if chosen else None)
        v = screening.verdict if screening else "No trial selected"
        if verdict and v != verdict:
            continue
        rows.append({"client": c, "verdict": v, "screening": screening,
                    "last_contact": contacts.get(c.id)})

    return render("report_patients.html", ctx(
        request, db, nav="reports", report="patients", rows=rows,
        trials_all=trials_all, chosen=chosen, verdict=verdict))


@app.get("/reports/appointments", response_class=HTMLResponse)
def report_appointments(request: Request, status: str = "",
                        db: Session = Depends(get_db),
                        _=Depends(needs(perms.SUBMISSIONS_VIEW))):
    rows = (db.query(schedule.Appointment)
            .options(joinedload(schedule.Appointment.client),
                     joinedload(schedule.Appointment.provider))
            .order_by(schedule.Appointment.on_day.desc()))
    if status:
        try:
            rows = rows.filter(schedule.Appointment.status == schedule.Attendance(status))
        except ValueError:
            pass
    return render("report_appointments.html", ctx(
        request, db, nav="reports", report="appointments",
        appointments=rows.limit(500).all(), status=status,
        statuses=list(schedule.Attendance)))


@app.get("/reports/notes", response_class=HTMLResponse)
def report_unsigned_notes(request: Request, db: Session = Depends(get_db),
                          _=Depends(needs(perms.CLINICAL_EDIT))):
    """Every clinical note still in draft.

    This is a genuine clinical-risk list, not a housekeeping one: a note left
    unsigned is a visit with no record a court, a payer or a colleague covering
    for the prescriber can rely on. Oldest first, because the ones that matter
    are the ones somebody has been meaning to get back to for weeks.
    """
    rows = (db.query(ehr.Encounter)
            .filter(ehr.Encounter.status == ehr.NoteStatus.draft)
            .options(joinedload(ehr.Encounter.client),
                     joinedload(ehr.Encounter.provider))
            .order_by(ehr.Encounter.seen_on).all())
    return render("report_notes.html", ctx(
        request, db, nav="reports", report="notes", encounters=rows))


@app.get("/reports/encounters", response_class=HTMLResponse)
def report_encounters(request: Request, db: Session = Depends(get_db),
                      _=Depends(needs(perms.SUBMISSIONS_VIEW))):
    rows = (db.query(ehr.Encounter)
            .options(joinedload(ehr.Encounter.client),
                     joinedload(ehr.Encounter.provider))
            .order_by(ehr.Encounter.seen_on.desc()).limit(500).all())
    return render("report_encounters.html", ctx(
        request, db, nav="reports", report="encounters", encounters=rows))


@app.get("/reports/pipeline", response_class=HTMLResponse)
def report_pipeline(request: Request, db: Session = Depends(get_db),
                    _=Depends(needs(perms.SUBMISSIONS_VIEW))):
    """The funnel: forms out, forms back, who's eligible, who's asked for feedback.

    Every number here is a plain count against a real table - nothing modelled
    or forecast. A pipeline dashboard that estimates is a pipeline dashboard
    nobody can use to answer "how many, right now".
    """
    total_patients = db.query(Client).filter_by(archived=False).count()
    intake_counts = {status.value: n for status, n in
                     db.query(Submission.status, func.count(Submission.id))
                     .group_by(Submission.status).all()}

    trial_rows: list[tuple] = []
    trials_all = db.query(Trial).filter_by(is_active=True).order_by(Trial.name).all()
    if trials_all:
        clients = (db.query(Client).filter_by(archived=False)
                   .options(selectinload(Client.medications),
                            selectinload(Client.submissions_list)
                            .selectinload(Submission.answers)
                            .joinedload(Answer.question))
                   .all())
        archives = records_for_many(db, (c.hospital_id for c in clients))
        for trial in trials_all:
            verdicts = {"Looks eligible": 0, "Needs review": 0,
                       "Not eligible": 0, "No criteria": 0}
            for c in clients:
                v = trials.evaluate(c, trial, archives.get(c.hospital_id or 0, [])).verdict
                verdicts[v] = verdicts.get(v, 0) + 1
            trial_rows.append((trial, verdicts))

    survey_total = db.query(ExperienceSurvey).count()
    survey_completed = db.query(ExperienceSurvey).filter_by(
        status=SurveyStatus.completed).count()
    ratings = [r for (r,) in db.query(ExperienceSurvey.rating_overall)
              .filter(ExperienceSurvey.rating_overall.isnot(None)).all()]
    avg_rating = round(sum(ratings) / len(ratings), 1) if ratings else None

    return render("report_pipeline.html", ctx(
        request, db, nav="reports", report="pipeline",
        total_patients=total_patients, intake_counts=intake_counts,
        trial_rows=trial_rows, survey_total=survey_total,
        survey_completed=survey_completed, avg_rating=avg_rating))


# ------------------------------------------------------------------ broadcasts
#
# A message to more than one patient at once. See app/broadcasts.py for why
# the safeguards here are entirely about the audience, not the wording - the
# filter is resolved fresh at send time, never trusted from a hidden field,
# and every recipient goes through the same consent gate a single-patient send
# already uses.


@app.get("/broadcasts", response_class=HTMLResponse)
def broadcasts_new(request: Request, db: Session = Depends(get_db),
                   _=Depends(needs(perms.SUBMISSIONS_SEND))):
    return render("broadcast_new.html", ctx(
        request, db, nav="broadcasts",
        trials_all=db.query(Trial).order_by(Trial.is_active.desc(), Trial.name).all(),
        verdicts=broadcasts.VERDICTS, form_statuses=broadcasts.FORM_STATUS,
        max_body=broadcasts.MAX_BODY, preview=None,
        error=request.session.pop("broadcast_error", "")))


def _broadcast_form(form) -> dict:
    return {
        "subject": (form.get("subject") or "").strip()[:200],
        "body": (form.get("body") or "").strip()[:broadcasts.MAX_BODY],
        "channel": form.get("channel") or "email",
        "trial_id": form.get("trial_id") or "",
        "verdict": form.get("verdict") or "",
        "form_status": form.get("form_status") or "",
    }


@app.post("/broadcasts/preview")
async def broadcasts_preview(request: Request, db: Session = Depends(get_db),
                             _=Depends(needs(perms.SUBMISSIONS_SEND))):
    """Resolve the filter and show who it matches. Nothing is sent."""
    form = await request.form()
    values = _broadcast_form(form)
    if not values["body"]:
        request.session["broadcast_error"] = "Write a message first."
        return RedirectResponse("/broadcasts", status_code=303)

    matched = broadcasts.matching_clients(
        db, trial_id=_int_or_none(values["trial_id"]),
        verdict=values["verdict"], form_status=values["form_status"])

    return render("broadcast_new.html", ctx(
        request, db, nav="broadcasts", values=values,
        trials_all=db.query(Trial).order_by(Trial.is_active.desc(), Trial.name).all(),
        verdicts=broadcasts.VERDICTS, form_statuses=broadcasts.FORM_STATUS,
        max_body=broadcasts.MAX_BODY, preview=matched,
        error=""))


@app.post("/broadcasts/send")
async def broadcasts_send(request: Request, db: Session = Depends(get_db),
                          _=Depends(needs(perms.SUBMISSIONS_SEND))):
    """Resolve the filter AGAIN and send for real.

    Never trusts the count a preview showed: that count was a string in HTML
    by the time this button was pressed, and re-running the same filter is the
    only way the fifty people who receive a message are the fifty who were
    actually reviewed.
    """
    form = await request.form()
    values = _broadcast_form(form)
    me = auth.current_user(request, db)
    if not values["body"]:
        request.session["broadcast_error"] = "Write a message first."
        return RedirectResponse("/broadcasts", status_code=303)

    try:
        channel = broadcasts.Channel(values["channel"])
    except ValueError:
        channel = broadcasts.Channel.email

    matched = broadcasts.matching_clients(
        db, trial_id=_int_or_none(values["trial_id"]),
        verdict=values["verdict"], form_status=values["form_status"])

    record = broadcasts.Broadcast(
        subject=values["subject"], body=values["body"], channel=channel,
        filter_trial_id=_int_or_none(values["trial_id"]),
        filter_verdict=values["verdict"], filter_form_status=values["form_status"],
        recipient_count=len(matched), sent_by=(me.name if me else "")[:160])
    db.add(record)
    db.flush()

    broadcasts.send(db, record, matched, request=request, config=config.load())
    log(db, f"Broadcast prepared ({len(matched)} matched)", "broadcast", record.id,
        user_id=getattr(me, "id", None), ip=client_ip(request))
    db.commit()

    request.session["broadcast_note"] = (
        f"{record.sent_count} sent, {record.skipped_count} skipped "
        f"(no address or no consent), {record.failed_count} failed, "
        f"of {record.recipient_count} matched.")
    return RedirectResponse(f"/broadcasts/{record.id}", status_code=303)


@app.get("/broadcasts/history", response_class=HTMLResponse)
def broadcasts_history(request: Request, db: Session = Depends(get_db),
                       _=Depends(needs(perms.SUBMISSIONS_SEND))):
    rows = (db.query(broadcasts.Broadcast)
            .options(joinedload(broadcasts.Broadcast.filter_trial))
            .order_by(broadcasts.Broadcast.id.desc()).all())
    return render("broadcast_history.html", ctx(
        request, db, nav="broadcasts", rows=rows,
        note=request.session.pop("broadcast_note", "")))


@app.get("/broadcasts/{broadcast_id}", response_class=HTMLResponse)
def broadcast_detail(broadcast_id: int, request: Request,
                     db: Session = Depends(get_db),
                     _=Depends(needs(perms.SUBMISSIONS_SEND))):
    record = (db.query(broadcasts.Broadcast)
              .options(selectinload(broadcasts.Broadcast.entries)
                       .joinedload(MessageLog.client))
              .filter_by(id=broadcast_id).first())
    if record is None:
        raise HTTPException(status_code=404, detail="Broadcast not found")
    return render("broadcast_detail.html", ctx(
        request, db, nav="broadcasts", record=record,
        note=request.session.pop("broadcast_note", "")))


# ------------------------------------------------------------------- portal
#
# The patient's own dashboard. Entirely separate auth from staff - see
# app/portal.py's module docstring for why that is structural, not a
# convention somebody has to remember to follow.


@app.get("/portal/login", response_class=HTMLResponse)
def portal_login(request: Request, timeout: int = 0):
    return render("portal_login.html", {
        "request": request, "practice": config.load(), "error": "",
        "notice": "You were signed out after 15 minutes of inactivity."
                  if timeout else "", "email": ""})


@app.post("/portal/login")
def portal_login_submit(request: Request, email: str = F(""),
                        password: str = F(""), db: Session = Depends(get_db)):
    def fail(message: str):
        return render("portal_login.html", {
            "request": request, "practice": config.load(), "error": message,
            "notice": "", "email": email})

    lock_key = portal.LOCKOUT_PREFIX + email.strip().lower()
    remaining = auth.is_locked(lock_key)
    if remaining:
        mins = max(1, int(remaining.total_seconds() // 60))
        return fail(f"Too many failed attempts. Try again in {mins} minute(s).")

    client = (db.query(Client)
              .filter(Client.email == email.strip().lower(),
                      Client.archived.is_(False)).first())
    ok = (client and client.portal_password_hash
         and auth.verify_password(password, client.portal_password_hash))
    if not ok:
        auth.record_failure(lock_key)
        log(db, "Portal login failed", "client",
            client.id if client else email, ip=client_ip(request))
        db.commit()
        # Identical message whether the email is unknown or the password is
        # wrong, the same reasoning as the staff login - this cannot be used
        # to learn who has portal access.
        return fail("Email or password is incorrect.")

    auth.clear_failures(lock_key)
    portal.sign_in(request, client)
    client.portal_last_login = datetime.utcnow()
    log(db, "Portal login", "client", client.id, ip=client_ip(request))
    db.commit()
    return RedirectResponse("/portal", status_code=303)


@app.get("/portal/logout")
def portal_logout(request: Request):
    portal.sign_out(request)
    return RedirectResponse("/portal/login", status_code=303)


def _portal_client(request: Request, db: Session) -> Client:
    """The signed-in patient, or a 401.

    A 401 rather than a redirect: every route below is already behind
    portal_auth_middleware, which redirects when there is no session at all.
    Reaching here with no patient found means the session named somebody
    whose row is gone (an admin deleted the client mid-session) - a real, if
    rare, edge case that deserves a clear failure rather than presenting one
    patient's dashboard as another's by silently falling through.
    """
    client = portal.current_patient(request, db)
    if client is None:
        raise HTTPException(status_code=401, detail="Not signed in")
    return client


@app.get("/portal", response_class=HTMLResponse)
def portal_home(request: Request, db: Session = Depends(get_db)):
    client = _portal_client(request, db)
    appts = (db.query(schedule.Appointment).filter_by(client_id=client.id)
             .order_by(schedule.Appointment.on_day.desc()).all())
    return render("portal_home.html", {
        "request": request, "practice": config.load(), "client": client, "nav": "home",
        "next_appt": portal.next_appointment(appts),
        "active_meds": client.active_medications,
        "unread": portal.unread_message_count(db, client.id),
        "open_forms": [s for s in client.submissions_list if s.is_open],
    })


@app.get("/portal/medications", response_class=HTMLResponse)
def portal_medications(request: Request, db: Session = Depends(get_db)):
    client = _portal_client(request, db)
    return render("portal_medications.html", {
        "request": request, "practice": config.load(), "client": client, "nav": "medications",
        "unread": portal.unread_message_count(db, client.id),
        "active": client.active_medications,
        "past": [m for m in client.medications if not m.is_active],
    })


@app.get("/portal/appointments", response_class=HTMLResponse)
def portal_appointments(request: Request, db: Session = Depends(get_db)):
    client = _portal_client(request, db)
    today = date.today()
    appts = (db.query(schedule.Appointment).filter_by(client_id=client.id)
             .order_by(schedule.Appointment.on_day.desc()).all())
    return render("portal_appointments.html", {
        "request": request, "practice": config.load(), "client": client, "nav": "appointments",
        "unread": portal.unread_message_count(db, client.id),
        "upcoming": [a for a in appts if a.on_day >= today],
        "past": [a for a in appts if a.on_day < today],
    })


@app.get("/portal/forms", response_class=HTMLResponse)
def portal_forms(request: Request, db: Session = Depends(get_db)):
    client = _portal_client(request, db)
    return render("portal_forms.html", {
        "request": request, "practice": config.load(), "client": client, "nav": "forms",
        "unread": portal.unread_message_count(db, client.id),
        "submissions": sorted(client.submissions_list,
                              key=lambda s: s.sent_at, reverse=True),
    })


@app.get("/portal/messages", response_class=HTMLResponse)
def portal_messages(request: Request, db: Session = Depends(get_db)):
    client = _portal_client(request, db)
    thread = (db.query(portal.PatientMessage).filter_by(client_id=client.id)
              .order_by(portal.PatientMessage.id).all())
    #  Opening the thread is when staff messages count as read - the same
    #  moment a phone call would count as having been answered.
    now = datetime.utcnow()
    for m in thread:
        if not m.from_patient and m.read_by_patient_at is None:
            m.read_by_patient_at = now
    db.commit()
    return render("portal_messages.html", {
        "request": request, "practice": config.load(), "client": client, "nav": "messages",
        "unread": 0, "thread": thread, "max_len": portal.MAX_MESSAGE_LENGTH})


@app.post("/portal/messages")
def portal_messages_send(request: Request, body: str = F(""),
                         db: Session = Depends(get_db)):
    client = _portal_client(request, db)
    text = body.strip()[:portal.MAX_MESSAGE_LENGTH]
    if text:
        db.add(portal.PatientMessage(client_id=client.id, sender_role="patient",
                                     body=text))
        #  Never an attempt to answer what they asked - see the module
        #  docstring on send_acknowledgment_if_due for why that boundary does
        #  not move, including for a question that looks simple.
        portal.send_acknowledgment_if_due(db, client.id)
        cache.drop("nav:unread_messages")
        log(db, "Portal message sent", "client", client.id, ip=client_ip(request))
        db.commit()
    return RedirectResponse("/portal/messages", status_code=303)


# --------------------------------------------------------- staff side of the portal


@app.post("/clients/{client_id}/portal/issue")
def portal_issue(client_id: int, request: Request, db: Session = Depends(get_db),
                 actor: User = Depends(needs(perms.CLIENTS_EDIT))):
    """Issue portal access, or reissue a fresh password over the old one.

    Requires an email on file - there is nothing else a patient could type
    into the login box - and the generated password is shown exactly once,
    the same flash-and-forget pattern already used for a new staff account.
    """
    client = get_or_404(db, Client, client_id)
    if not (client.email or "").strip():
        request.session["chart_error"] = (
            f"{client.first_name} has no email on file. The portal is "
            f"reached by email and password, so one is needed before access "
            f"can be issued.")
        return RedirectResponse(f"/clients/{client_id}#portal", status_code=303)

    password = um.temp_password()
    client.portal_password_hash = auth.hash_password(password)
    client.portal_issued_at = datetime.utcnow()
    log(db, "Portal access issued", "client", client.id,
        user_id=actor.id, ip=client_ip(request))
    db.commit()
    request.session["flash"] = {
        "title": f"Portal access for {client.name}",
        "email": client.email, "password": password,
    }
    return RedirectResponse(f"/clients/{client_id}#portal", status_code=303)


@app.post("/clients/{client_id}/portal/revoke")
def portal_revoke(client_id: int, request: Request, db: Session = Depends(get_db),
                  actor: User = Depends(needs(perms.CLIENTS_EDIT))):
    client = get_or_404(db, Client, client_id)
    client.portal_password_hash = None
    log(db, "Portal access revoked", "client", client.id,
        user_id=actor.id, ip=client_ip(request))
    db.commit()
    request.session["chart_note"] = f"Portal access removed for {client.first_name}."
    return RedirectResponse(f"/clients/{client_id}#portal", status_code=303)


@app.post("/clients/{client_id}/messages")
def staff_message_send(client_id: int, request: Request, body: str = F(""),
                       db: Session = Depends(get_db),
                       actor: User = Depends(needs(perms.SUBMISSIONS_SEND))):
    client = get_or_404(db, Client, client_id)
    text = body.strip()[:portal.MAX_MESSAGE_LENGTH]
    if text:
        db.add(portal.PatientMessage(client_id=client.id, sender_role="staff",
                                     staff_user_id=actor.id, body=text))
        log(db, "Message sent to patient", "client", client.id,
            user_id=actor.id, ip=client_ip(request))
        db.commit()
    return RedirectResponse(f"/clients/{client_id}#messages", status_code=303)


@app.get("/messages", response_class=HTMLResponse)
def staff_messages_inbox(request: Request, db: Session = Depends(get_db),
                         _=Depends(needs(perms.SUBMISSIONS_SEND))):
    """Every patient with an unread message, most recent first.

    The same shape as /results: a thread nobody has opened is the failure
    with real consequences, so it gets its own screen rather than a badge
    buried on a chart nobody happened to open today.
    """
    unread_client_ids = (
        db.query(portal.PatientMessage.client_id)
        .filter_by(sender_role="patient", read_by_staff_at=None)
        .distinct().all())
    ids = [row[0] for row in unread_client_ids]
    clients = (db.query(Client).filter(Client.id.in_(ids)).all() if ids else [])
    rows = []
    for c in clients:
        last = (db.query(portal.PatientMessage).filter_by(client_id=c.id)
                .order_by(portal.PatientMessage.id.desc()).first())
        rows.append({"client": c, "last": last})
    rows.sort(key=lambda r: r["last"].sent_at, reverse=True)
    return render("messages_inbox.html", ctx(
        request, db, nav="messages", rows=rows))
