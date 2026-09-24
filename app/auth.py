"""Staff authentication.

Design notes:

* **Deny by default.** Access is enforced in middleware against an allow-list of
  public paths, not by remembering to decorate each route. A new staff route is
  protected the moment it exists; forgetting is not possible.

* **scrypt, not a hash.** Passwords go through `hashlib.scrypt` with a per-user
  random salt. It is memory-hard, it is in the standard library, and it needs no
  wheel that might not build on Windows. SHA-256 of a password is not storage,
  it is a rainbow-table lookup waiting to happen.

* **Constant-time comparison.** `hmac.compare_digest` throughout, so a wrong
  password cannot be found one byte at a time by timing the response.

* **Lockout and idle timeout** exist because this holds PHI. HIPAA's Security Rule
  asks for automatic logoff; five failed attempts locking an account for fifteen
  minutes is the ordinary answer to online guessing.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from .models import SessionLocal, User, log

ROOT = Path(__file__).resolve().parent.parent
SECRET_FILE = ROOT / "config" / "session.key"
FIRST_RUN_FILE = ROOT / "first-run-credentials.txt"

# scrypt parameters. n=2**14 keeps a single hash around 50-100ms on a laptop,
# which is slow enough to make guessing expensive and fast enough for a login.
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 14, 8, 1

MIN_PASSWORD_LENGTH = 10
IDLE_TIMEOUT = timedelta(minutes=20)
MAX_ATTEMPTS = 5
LOCKOUT = timedelta(minutes=15)

# Paths reachable without a session. Everything else requires one.
PUBLIC_PREFIXES = ("/login", "/logout", "/static/", "/f/", "/survey/", "/setup",
                   "/forgot", "/reset/", "/request-access", "/register",
                   # The patient portal runs its own, entirely separate
                   # authentication - see app/portal.py. This middleware
                   # checks for "uid", a staff session; a patient is never
                   # going to have one, and must not be redirected into the
                   # staff login trying to reach their own.
                   "/portal")

# email -> (failed count, first failure time). In-process; a multi-worker
# deployment needs this in the database or a shared cache instead.
_attempts: dict[str, tuple[int, datetime]] = {}


# ----------------------------------------------------------------- passwords

def hash_password(password: str) -> str:
    """'scrypt$<salt b64>$<derived b64>' - salt travels with the hash."""
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode("utf-8"), salt=salt,
                        n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32)
    return f"scrypt${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def verify_password(password: str, stored: str) -> bool:
    if not stored or not stored.startswith("scrypt$"):
        return False
    try:
        _, salt_b64, dk_b64 = stored.split("$", 2)
        salt, expected = base64.b64decode(salt_b64), base64.b64decode(dk_b64)
    except (ValueError, TypeError):
        return False
    actual = hashlib.scrypt(password.encode("utf-8"), salt=salt,
                            n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=len(expected))
    return hmac.compare_digest(actual, expected)


# ------------------------------------------------------------------ lockout

def is_locked(email: str) -> timedelta | None:
    """Remaining lockout, or None if the account may attempt a login."""
    record = _attempts.get(email.lower())
    if not record:
        return None
    count, first = record
    if count < MAX_ATTEMPTS:
        return None
    elapsed = datetime.utcnow() - first
    if elapsed >= LOCKOUT:
        _attempts.pop(email.lower(), None)
        return None
    return LOCKOUT - elapsed


def record_failure(email: str) -> None:
    key = email.lower()
    count, first = _attempts.get(key, (0, datetime.utcnow()))
    if datetime.utcnow() - first > LOCKOUT:
        count, first = 0, datetime.utcnow()
    _attempts[key] = (count + 1, first)


def clear_failures(email: str) -> None:
    _attempts.pop(email.lower(), None)


# ------------------------------------------------------------------ sessions

def session_secret() -> str:
    """Stable across restarts, or every restart logs everyone out.

    Three sources, in order. The environment comes first because a serverless
    host has no writable disk and, more importantly, no single disk: every
    instance would mint its own key and sessions would break at random as
    requests landed on different ones.
    """
    from_env = os.environ.get("SESSION_SECRET", "").strip()
    if from_env:
        return from_env

    try:
        SECRET_FILE.parent.mkdir(exist_ok=True)
    except OSError:
        # Read-only filesystem. Generate one and warn: the app still works, but
        # a restart signs everyone out, so this is a misconfiguration, not a mode.
        print("WARNING: no SESSION_SECRET set and the disk is read-only - "
              "sessions will not survive a restart. Set SESSION_SECRET.")
        return secrets.token_urlsafe(48)

    if SECRET_FILE.exists():
        key = SECRET_FILE.read_text(encoding="utf-8").strip()
        if key:
            return key
    key = secrets.token_urlsafe(48)
    try:
        SECRET_FILE.write_text(key, encoding="utf-8")
        os.chmod(SECRET_FILE, 0o600)
    except OSError:
        # Either Windows without the right ACL support, or a read-only disk.
        # Neither is worth refusing to start over; the file is gitignored anyway.
        pass
    return key


def current_user(request: Request, db: Session) -> User | None:
    uid = request.session.get("uid")
    if not uid:
        return None
    user = db.get(User, uid)
    return user if user and user.is_active else None


def sign_in(request: Request, user: User) -> None:
    request.session.clear()          # new session id on privilege change
    request.session["uid"] = user.id
    request.session["seen"] = datetime.utcnow().isoformat()


# ---------------------------------------------------------------- MFA pending
#
# A user whose password verified but whose second factor has not is not signed
# in - `current_user()` reads only "uid", never "pending_mfa_uid", so a request
# in this state is indistinguishable from an anonymous one everywhere except
# the one route that checks for it. That is the entire safety property this
# state exists for: a bug anywhere else in the app cannot accidentally treat a
# pending login as an authenticated one, because nothing else knows to look.

PENDING_MFA_TIMEOUT = timedelta(minutes=5)


def begin_mfa_challenge(request: Request, user: User) -> None:
    """Password verified; waiting on a code. Grants nothing by itself."""
    request.session.clear()
    request.session["pending_mfa_uid"] = user.id
    request.session["pending_mfa_at"] = datetime.utcnow().isoformat()


def pending_mfa_user(request: Request, db: Session) -> User | None:
    """Who is mid-login, or None if nobody is, or the wait ran out.

    Timed independently of the idle-session clock, and much shorter: five
    minutes is enough to find a phone and read a code off it, and an
    indefinitely-open half-login is a state with a password already proven
    and no reason to leave it standing.
    """
    uid = request.session.get("pending_mfa_uid")
    started = request.session.get("pending_mfa_at")
    if not uid or not started:
        return None
    try:
        began = datetime.fromisoformat(started)
    except ValueError:
        return None
    if datetime.utcnow() - began > PENDING_MFA_TIMEOUT:
        request.session.clear()
        return None
    user = db.get(User, uid)
    return user if user and user.is_active else None


def sign_out(request: Request) -> None:
    request.session.clear()


def touch(request: Request) -> bool:
    """Refresh the idle timer. False means the session has timed out."""
    seen = request.session.get("seen")
    if not seen:
        return False
    try:
        last = datetime.fromisoformat(seen)
    except ValueError:
        return False
    if datetime.utcnow() - last > IDLE_TIMEOUT:
        return False
    request.session["seen"] = datetime.utcnow().isoformat()
    return True


# ---------------------------------------------------------------- middleware

def system_claimed() -> bool:
    """Has anybody set up an account yet?

    Self-registration is disabled in an internal system - anyone reaching the URL
    could otherwise mint themselves access to patient records. But the FIRST
    account has to come from somewhere, so setup is open exactly until it is used
    and closed permanently afterwards.
    """
    db = SessionLocal()
    try:
        return db.query(User).filter(User.password_hash != "",
                                     User.is_active.is_(True)).count() > 0
    finally:
        db.close()


async def auth_middleware(request: Request, call_next):
    path = request.url.path

    # Nobody has an account yet: everything leads to setup.
    if not system_claimed() and not path.startswith(("/static/", "/setup")):
        return RedirectResponse("/setup", status_code=303)

    if path.startswith(PUBLIC_PREFIXES):
        return await call_next(request)

    if not request.session.get("uid"):
        return RedirectResponse(f"/login?next={path}", status_code=303)

    if not touch(request):
        request.session.clear()
        return RedirectResponse("/login?timeout=1", status_code=303)

    # A temporary password is a credential someone else has seen. Until it is
    # replaced the account can reach its own settings and nothing else.
    if path not in ("/account", "/account/password", "/pending"):
        db = SessionLocal()
        try:
            user = current_user(request, db)
            if user and user.must_change_password:
                return RedirectResponse("/account?mustchange=1", status_code=303)
            # Self-registered and not yet approved: one screen, nothing else.
            if user and user.role.value == "pending":
                return RedirectResponse("/pending", status_code=303)
        finally:
            db.close()

    return await call_next(request)


# ---------------------------------------------------------------- first run

def create_first_owner(name: str, email: str, password: str) -> User:
    """Create the founding account from the setup screen.

    Refuses once the system is claimed, so the open setup route cannot be used a
    second time to mint another owner. A shipped default account would be a
    shipped vulnerability; letting the first real person name themselves avoids
    one existing at all.
    """
    if system_claimed():
        raise PermissionError("This system has already been set up.")

    from .models import UserRole

    db = SessionLocal()
    try:
        address = email.strip().lower()
        user = db.query(User).filter(User.email == address).first()
        if user:
            user.name, user.role, user.is_active = name.strip(), UserRole.owner, True
        else:
            user = User(email=address, name=name.strip(), role=UserRole.owner)
            db.add(user)
        user.password_hash = hash_password(password)
        user.must_change_password = False
        db.flush()
        log(db, "System Set Up", "user", user.id, user_id=user.id)
        db.commit()
        db.refresh(user)
        return user
    finally:
        db.close()
