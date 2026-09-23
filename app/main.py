"""FastAPI application - staff screens and the patient-facing form."""

from __future__ import annotations

import os
import secrets
import subprocess
from datetime import date, datetime
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
from . import schedule
from . import trials
from .trials import Trial
from . import matching
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
    return render(
        "dashboard.html",
        ctx(request, db, received=received, pending=pending, events=events, nav="home"),
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
    auth.sign_in(request, user)
    user.last_login = datetime.utcnow()
    log(db, "Staff Logged In", "user", user.id, user_id=user.id, ip=client_ip(request))
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
        #  Paperwork this patient completed in IntakeQ before they were ever a
        #  row here. Listed beside the forms sent from this app on purpose: to
        #  the person reading the chart they are the same thing, and which
        #  system happened to collect one is not a distinction worth a click.
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
        run=intakeq.latest_run(db), day_limit=intakeq.PER_DAY))


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
    log(db, f"IntakeQ import {run.status}: {run.clients_new} new patients, "
            f"{run.intakes_new} intakes, {run.requests_used} requests used",
        "integration", "intakeq")
    db.commit()
    return RedirectResponse("/integrations#intakeq-import", status_code=303)


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
                       User.role.in_([UserRole.practitioner, UserRole.owner]))
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
