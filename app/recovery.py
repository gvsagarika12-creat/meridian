"""Forgot-password and request-access: the two doors on the sign-in screen.

**Reset tokens are stored, single-use and short-lived.** Stored rather than signed so
they can be revoked; single-use because a link that still works after the password
changed is a back door; two hours because a reset link sitting in a mailbox is a
standing key to the account.

**The form answers the same way whether the account exists or not.** Otherwise it
becomes a way to discover who works here.

**Requesting access is not signing up.** It records a request; an administrator
approves it and the account is created then. Open self-registration on a system
holding patient records would let anyone who finds the address give themselves a
view of charts.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Form as F, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from . import auth, config, messaging
from .models import AccessRequest, MessageLog, PasswordReset, SessionLocal, User, log

router = APIRouter()

RESET_LIFETIME = timedelta(hours=2)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    return fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "")


def _page(template: str, request: Request, /, **extra):
    """Positional-only first arg: this page passes a `name` form field through
    **extra, which would otherwise collide with the parameter name."""
    from .main import render
    ctx = {"request": request, "practice": config.load()}
    ctx.update(extra)
    return render(template, ctx)


# ------------------------------------------------------------ forgot password

@router.get("/forgot", response_class=HTMLResponse)
def forgot(request: Request):
    return _page("forgot.html", request, sent=False, error="", email="",
                 link="", unconfigured=isinstance(
                     messaging.backend_for("email"), messaging.FileBackend))


@router.post("/forgot", response_class=HTMLResponse)
def do_forgot(request: Request, email: str = F(""), db: Session = Depends(get_db)):
    address = email.strip().lower()
    user = db.query(User).filter(User.email == address, User.is_active.is_(True)).first()
    shown_link = ""
    unconfigured = isinstance(messaging.backend_for("email", db), messaging.FileBackend)

    if user:
        token = secrets.token_urlsafe(32)
        db.add(PasswordReset(user_id=user.id, token=token,
                             expires_at=datetime.utcnow() + RESET_LIFETIME))
        link = f"{str(request.base_url).rstrip('/')}/reset/{token}"
        practice = config.load()
        message = messaging.Message(
            to=user.email,
            subject=f"Reset your {practice.get('name', 'account')} password",
            body=(
                f"Hello {user.name},\n\n"
                f"Someone asked to reset the password on this account.\n\n"
                f"{link}\n\n"
                f"The link works once and expires in 2 hours.\n"
                f"If this was not you, ignore this message. Nothing has changed.\n"
            ),
            channel="email",
        )
        backend = messaging.backend_for("email", db)
        try:
            backend.send(message)
            status, detail = "sent", ""
        except messaging.SendFailed as exc:
            status, detail = "failed", str(exc)

        # No mail server configured means the message went to outbox.log and the
        # person is staring at "check your email" with nothing to check. On a local
        # install, show them the link instead of sending them on a hunt.
        #
        # This does reveal whether an account exists, which the "same answer either
        # way" rule above exists to prevent. That trade is acceptable only because
        # anyone who can reach this screen on a local install can already run
        # admin.py and take over any account. Configure SMTP and both the hunt and
        # the disclosure go away.
        if isinstance(backend, messaging.FileBackend):
            shown_link = link
        db.add(MessageLog(channel="email", recipient=user.email, status=status,
                          detail=detail, backend=backend.name))
        log(db, "Password Reset Requested", "user", user.id, ip=_ip(request))
        db.commit()

    # Same answer either way - no account enumeration, except for the local-install
    # case handled above.
    return _page("forgot.html", request, sent=True, error="", email=address,
                 link=shown_link, unconfigured=unconfigured)


@router.get("/reset/{token}", response_class=HTMLResponse)
def reset_form(token: str, request: Request, db: Session = Depends(get_db)):
    pr = db.query(PasswordReset).filter_by(token=token).first()
    return _page("reset.html", request, token=token, dead=not (pr and pr.is_usable), error="")


@router.post("/reset/{token}", response_class=HTMLResponse)
def do_reset(token: str, request: Request, password: str = F(""), confirm: str = F(""),
             db: Session = Depends(get_db)):
    pr = db.query(PasswordReset).filter_by(token=token).first()
    if not pr or not pr.is_usable:
        return _page("reset.html", request, token=token, dead=True, error="")

    if len(password) < auth.MIN_PASSWORD_LENGTH:
        return _page("reset.html", request, token=token, dead=False,
                     error=f"Password must be at least {auth.MIN_PASSWORD_LENGTH} characters.")
    if password != confirm:
        return _page("reset.html", request, token=token, dead=False,
                     error="The two passwords do not match.")

    user = db.get(User, pr.user_id)
    user.password_hash = auth.hash_password(password)
    user.must_change_password = False
    now = datetime.utcnow()
    pr.used_at = now
    # Burn every other outstanding link for this account.
    for other in db.query(PasswordReset).filter_by(user_id=user.id, used_at=None).all():
        other.used_at = now
    auth.clear_failures(user.email)
    log(db, "Password Reset Completed", "user", user.id, user_id=user.id, ip=_ip(request))
    db.commit()
    return RedirectResponse("/login?reset=1", status_code=303)


# ------------------------------------------------------------ request access

@router.get("/request-access", response_class=HTMLResponse)
def request_access(request: Request):
    return _page("request_access.html", request, sent=False, error="",
                 name="", email="", note="")


@router.post("/request-access", response_class=HTMLResponse)
def do_request_access(request: Request, name: str = F(""), email: str = F(""),
                      note: str = F(""), db: Session = Depends(get_db)):
    address = email.strip().lower()
    if not name.strip():
        return _page("request_access.html", request, sent=False,
                     error="Enter your name.", name=name, email=email, note=note)
    if "@" not in address or "." not in address.split("@")[-1]:
        return _page("request_access.html", request, sent=False,
                     error="Enter a valid email address.", name=name, email=email, note=note)

    already = db.query(AccessRequest).filter_by(email=address, status="pending").first()
    if not already:
        db.add(AccessRequest(name=name.strip(), email=address, note=note.strip()[:500]))
        log(db, "Access Requested", "access_request", address, ip=_ip(request))
        db.commit()

    return _page("request_access.html", request, sent=True, error="",
                 name="", email="", note="")


# ---------------------------------------------------------------- registration

@router.get("/register", response_class=HTMLResponse)
def register(request: Request):
    return _page("register.html", request, error="", name="", email="")


@router.post("/register", response_class=HTMLResponse)
def do_register(request: Request, name: str = F(""), email: str = F(""),
                password: str = F(""), confirm: str = F(""),
                db: Session = Depends(get_db)):
    """Open sign-up. The account is real and can sign in - it just cannot reach
    anything until somebody with access assigns it a role.

    That separation is the whole point. "Anyone may create a login" and "anyone may
    read patient records" are different statements, and only the first one is true
    here."""
    from .models import UserRole

    address = email.strip().lower()

    if not name.strip():
        error = "Enter your name."
    elif "@" not in address or "." not in address.split("@")[-1]:
        error = "Enter a valid email address."
    elif len(password) < auth.MIN_PASSWORD_LENGTH:
        error = f"Password must be at least {auth.MIN_PASSWORD_LENGTH} characters."
    elif password != confirm:
        error = "The two passwords do not match."
    else:
        error = ""

    if error:
        return _page("register.html", request, error=error, name=name, email=email)

    existing = db.query(User).filter(User.email == address).first()
    if existing:
        # Not "that email is taken" - that would confirm who has an account here.
        return _page("register.html", request,
                     error="If that address can be used, you will hear from us. "
                           "Try signing in, or use 'Forgot your password?'.",
                     name=name, email=email)

    user = User(email=address, name=name.strip(), role=UserRole.pending,
                password_hash=auth.hash_password(password), must_change_password=False)
    db.add(user)
    db.flush()
    log(db, "Account Self-Registered", "user", user.id, ip=_ip(request))
    db.commit()

    auth.sign_in(request, user)
    return RedirectResponse("/pending", status_code=303)
