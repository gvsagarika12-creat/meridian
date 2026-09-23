"""Two-factor authentication for staff logins.

Scoped to staff, not "everyone the way the plan for another product listed
it." This app has no patient portal - a patient's only credential is the
one-time link in `app/patient.py`, revoked by being looked up, not typed - so
"patient two-step login" and "patient-portal idle auto-logoff" have nothing to
attach to yet. Building patient MFA before a patient account exists is MFA on
top of nothing; the day a patient portal is built, it earns its own scoping,
not a retrofit of this module's shape.

**TOTP, not a hand-rolled scheme.** The algorithm is RFC 6238 - a shared secret,
the current 30-second time step, HMAC-SHA1, six digits - and it has to
interoperate with whatever authenticator app the practice already uses
(Google Authenticator, Authy, 1Password). Getting that byte-for-byte right by
hand is a lot of surface area for a security primitive to get subtly wrong in
a way nobody notices until the one time it matters; `pyotp` is a small, widely
used, pure-Python implementation of exactly this standard, so the code here is
the parts that are specific to this app - storage, enrollment, backup codes -
not the cryptography.

**The secret is encrypted at rest, under its own key.** Reusing
`credentials.py`'s encryption is the wrong kind of reuse: a Fernet key is
already a single point of failure, and giving one key two different secrets to
protect (a Tebra password and a user's second factor) means compromising
either use erodes confidence in both. Derived from the same SESSION_SECRET
root, salted differently, so the two encryption domains do not share a key
even though they share a source.

**Backup codes are hashed, not encrypted.** A TOTP secret has to be decrypted
back to a live code generator every time somebody signs in, so it is
recoverable by design. A backup code is presented once and never needed again -
exactly a password's shape - so it gets a password's treatment: scrypt,
one-way, and a code that matches a hash tells you nothing else the hash did
not already tell you.

**Enrollment is opt-in per user, not forced on everyone at once.** Six
existing staff accounts have never seen this screen; switching MFA on for all
of them the moment this ships would lock somebody out on their next sign-in
with no warning. A user turns it on for themselves, from the Account page, and
sees their backup codes before it is enforced on their own login - the
practice can decide later whether to make it mandatory, but that is a policy
choice for the practice, not a default this code should make for them.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from datetime import datetime

import pyotp
from sqlalchemy import DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .models import Base

BACKUP_CODE_COUNT = 10
#  How many codes hashlib.scrypt is asked to check per verification attempt at
#  most. A user has ten unused codes at enrollment; checking every one of them
#  against scrypt's deliberately slow hash on every login attempt is the
#  correct cost for ten strangers guessing and the wrong cost for nobody
#  guessing, but ten scrypt calls is still comfortably sub-second.


class MfaBackupCode(Base):
    """One single-use recovery code.

    Its own table, one row per code, rather than a JSON list on the user row -
    the same reasoning `PasswordReset` already uses for a single-use token: a
    row that is spent gets a used_at timestamp instead of being edited out of
    a blob, so "which codes has this person actually used, and when" stays a
    question the database can answer directly.
    """

    __tablename__ = "mfa_backup_codes"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    code_hash: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    user: Mapped["User"] = relationship()                          # noqa: F821


# --------------------------------------------------------------- encryption
#
# The same derivation credentials.py uses - scrypt over SESSION_SECRET, base64
# into a Fernet key - with a salt of its own, so a TOTP secret and a stored
# integration credential are never protected by the identical key material.


def _key() -> bytes:
    from .auth import session_secret

    secret = session_secret().strip()
    if not secret:
        raise RuntimeError("No session secret, so MFA secrets cannot be encrypted.")
    raw = hashlib.scrypt(secret.encode("utf-8"), salt=b"user-mfa-secrets-v1",
                         n=2 ** 14, r=8, p=1, dklen=32)
    return base64.urlsafe_b64encode(raw)


def _fernet():
    from cryptography.fernet import Fernet
    return Fernet(_key())


def _encrypt(secret: str) -> str:
    return _fernet().encrypt(secret.encode("utf-8")).decode("ascii")


def _decrypt(token: str) -> str:
    return _fernet().decrypt(token.encode("ascii")).decode("utf-8")


# ------------------------------------------------------------------ enrolling


def new_secret() -> str:
    """A fresh base32 TOTP secret. Not stored yet - see `begin_enrollment`."""
    return pyotp.random_base32()


def provisioning_uri(secret: str, *, email: str, issuer: str) -> str:
    """The otpauth:// URI an authenticator app turns into a QR code.

    No QR image is generated here - that needs an image library for a code the
    manual entry key already covers, and every authenticator app supports
    typing a key in by hand. The URI is shown as text too, for an app that
    accepts a pasted link.
    """
    return pyotp.totp.TOTP(secret).provisioning_uri(name=email, issuer_name=issuer)


def begin_enrollment(db, user) -> str:
    """Generate and store a secret, but do not turn MFA on yet.

    Storing before confirming is deliberate: the secret has to exist somewhere
    for the confirmation step to check a code against, and storing it encrypted
    now rather than passing it through the browser in a hidden field means it
    never has to be re-typed or re-derived if the confirm page is reloaded.
    MFA only becomes enforced in `confirm_enrollment`, once a real code from
    the user's own app has proven the secret actually reached them.
    """
    secret = new_secret()
    user.mfa_secret_encrypted = _encrypt(secret)
    user.mfa_enabled = False
    return secret


def pending_secret(user) -> str | None:
    """The plaintext of a secret already stored but not yet confirmed.

    Used only to redraw the setup screen after a wrong first code, so the
    manual-entry key stays the same across a retry instead of silently
    swapping to a second secret the user never scanned.
    """
    if not user.mfa_secret_encrypted:
        return None
    return _decrypt(user.mfa_secret_encrypted)


def confirm_enrollment(db, user, code: str) -> list[str] | None:
    """Check the first code and, if it is right, switch MFA on.

    Returns the plaintext backup codes - the only moment they exist outside a
    scrypt hash - or None if the code did not verify, in which case nothing
    about the user changes and they may try again or re-enroll.
    """
    if not user.mfa_secret_encrypted or not verify_code(user, code):
        return None
    user.mfa_enabled = True
    user.mfa_enrolled_at = datetime.utcnow()
    return _issue_backup_codes(db, user)


def _issue_backup_codes(db, user) -> list[str]:
    """Replace this user's backup codes with a fresh set of ten.

    Old ones are deleted outright rather than kept around unused: a stale
    backup code from an enrollment nobody remembers is a working credential
    with no record of when someone was told about it.
    """
    from .models import log

    db.query(MfaBackupCode).filter_by(user_id=user.id).delete()
    plain = []
    for _ in range(BACKUP_CODE_COUNT):
        code = "-".join([secrets.token_hex(2), secrets.token_hex(2)])
        plain.append(code)
        db.add(MfaBackupCode(user_id=user.id,
                             code_hash=_hash_backup_code(code)))
    log(db, "MFA backup codes issued", "user", user.id, user_id=user.id)
    return plain


def _hash_backup_code(code: str) -> str:
    """scrypt, the same primitive and cost as a password - see the module
    docstring for why a backup code gets a password's treatment, not the TOTP
    secret's."""
    from .auth import hash_password

    return hash_password(code.strip().lower())


# ------------------------------------------------------------------ verifying


def verify_code(user, code: str) -> bool:
    """A live TOTP code from the authenticator app.

    `valid_window=1` accepts the step either side of now, which is the
    standard tolerance for clock drift between the practice's machine and
    whichever phone somebody is holding - wide enough to forgive a phone whose
    clock is thirty seconds off, narrow enough that it is still a six-digit
    guess with a one-in-a-million chance per attempt, not a minute-wide window
    a script could sit inside.
    """
    if not user.mfa_secret_encrypted:
        return False
    code = (code or "").strip().replace(" ", "")
    if not code:
        return False
    try:
        secret = _decrypt(user.mfa_secret_encrypted)
    except Exception:                                    # noqa: BLE001
        return False
    return pyotp.TOTP(secret).verify(code, valid_window=1)


def verify_backup_code(db, user, code: str) -> bool:
    """A recovery code, burned the moment it is used.

    Every unused code for this user is checked - there is no way to look one
    up by its hash, which is the point of hashing it - but the count is ten,
    so ten scrypt calls on a login attempt is a cost worth paying for not being
    able to reverse the hash.
    """
    from .auth import verify_password
    from .models import log

    typed = (code or "").strip().lower()
    if not typed:
        return False
    unused = (db.query(MfaBackupCode)
              .filter_by(user_id=user.id, used_at=None).all())
    for row in unused:
        if verify_password(typed, row.code_hash):
            row.used_at = datetime.utcnow()
            log(db, "MFA backup code used", "user", user.id, user_id=user.id)
            return True
    return False


def regenerate_backup_codes(db, user) -> list[str]:
    """A fresh set of ten, replacing whatever is left of the old ones.

    The public entry point for `_issue_backup_codes` - called from the
    self-service Account page once MFA is already on, as distinct from
    `confirm_enrollment`, which issues the first set as part of turning it on.
    """
    return _issue_backup_codes(db, user)


def remaining_backup_codes(db, user) -> int:
    return (db.query(MfaBackupCode)
            .filter_by(user_id=user.id, used_at=None).count())


def disable(db, user, *, by_admin: bool = False) -> None:
    """Turn MFA off and remove the secret and every backup code.

    Deleting the secret rather than merely clearing `mfa_enabled` matters: an
    encrypted secret sitting in the database with the feature "off" is one
    config flag away from being live again with a key nobody re-verified, and
    a lost-device reset should mean starting enrollment over, not resuming an
    old one.

    Logs its own audit line only for the self-service case, where the account
    holder is unambiguously the actor. An admin-triggered reset is logged by
    the route that calls this instead, against the admin's own id - the caller
    holds that id and this function does not, and a log line naming nobody
    ("by an administrator", with no id attached) is worse than one written by
    whoever actually has the identity to put in it.
    """
    from .models import log

    user.mfa_secret_encrypted = None
    user.mfa_enabled = False
    user.mfa_enrolled_at = None
    db.query(MfaBackupCode).filter_by(user_id=user.id).delete()
    if not by_admin:
        log(db, "MFA turned off", "user", user.id, user_id=user.id)
