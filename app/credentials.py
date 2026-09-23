"""Credentials entered through the app rather than typed into a server.

The Connections screen used to be able to report that a key was missing and
nothing else. Setting one meant editing a `.env` file on the practice machine,
or running the hosting provider's command line tool - which in practice means
the credential can only ever be changed by whoever set the system up. A
practice administrator should be able to paste in an API key.

Three decisions worth stating, because each of them rules out an easier option.

**Encrypted, not plain.** A credential in a database table is a credential in
every backup of that database, and backups get copied to laptops and external
drives in a way the live database never is. The row holds a Fernet token, and
the key for it is derived from SESSION_SECRET, which lives in the environment -
so the database alone is not enough to read them back.

**Stored beats the environment.** When a value exists in both places the stored
one wins, because it is the one somebody set deliberately and most recently: an
environment variable needs a redeploy to change, so a UI that silently lost to
one would look broken. Which source each value came from is shown on screen, so
this is never a guess.

**Never shown back.** A stored secret can be replaced or removed, never read.
`value_of` exists for the code that makes the call; nothing renders it. A screen
that prints an API key is a screen that leaks it to whoever is standing behind
you, and to every screenshot pasted into a support thread.
"""

from __future__ import annotations

import base64
import hashlib
import os
from datetime import datetime

from sqlalchemy import DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .models import Base


class Credential(Base):
    """One secret, encrypted, addressed by the variable name it stands in for."""

    __tablename__ = "credentials"

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    secret: Mapped[str] = mapped_column(Text)               # a Fernet token
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    #  The name as text, not a foreign key to users. The audit table learned
    #  this the hard way: accounts are local to each copy of the database, so a
    #  row pointing at user 4 breaks the moment the data is moved to the hosted
    #  copy, where user 4 is somebody else or nobody.
    updated_by: Mapped[str] = mapped_column(String(120), default="")


# --------------------------------------------------------------- encryption

def _key() -> bytes:
    """A Fernet key derived from SESSION_SECRET.

    Derived rather than stored so there is no second secret to manage, and
    salted with a constant specific to this use so that the same session secret
    protecting cookies does not also directly become the credential key.
    """
    #  Imported here, not at module scope: models imports this module at the
    #  bottom of its own file, and auth imports models, so a top-level import
    #  would close that loop.
    from .auth import session_secret

    secret = session_secret().strip()
    if not secret:
        raise RuntimeError("No session secret, so credentials cannot be encrypted.")
    raw = hashlib.scrypt(secret.encode("utf-8"), salt=b"practice-credentials-v1",
                         n=2 ** 14, r=8, p=1, dklen=32)
    return base64.urlsafe_b64encode(raw)


def available() -> bool:
    """Whether storing a credential is possible at all on this machine.

    The session secret is resolved the same way the login cookie resolves it -
    environment first, then a file beside the database. On a host with neither,
    one is generated per process, which would make stored credentials
    unreadable after a restart; that case is reported rather than allowed, so
    nobody saves a key into a black hole.
    """
    try:
        import cryptography.fernet  # noqa: F401
    except ImportError:
        return False
    from .auth import SECRET_FILE, session_secret
    if (os.environ.get("SESSION_SECRET") or "").strip():
        return True
    return bool(session_secret()) and SECRET_FILE.exists()


def _fernet():
    from cryptography.fernet import Fernet
    return Fernet(_key())


# ------------------------------------------------------------------ writing

def put(db, name: str, value: str, who: str = "") -> None:
    token = _fernet().encrypt(value.encode("utf-8")).decode("ascii")
    row = db.get(Credential, name)
    if row:
        row.secret = token
        row.updated_at = datetime.utcnow()
        row.updated_by = who[:120]
    else:
        db.add(Credential(name=name, secret=token, updated_by=who[:120]))


def drop(db, name: str) -> bool:
    row = db.get(Credential, name)
    if not row:
        return False
    db.delete(row)
    return True


# ------------------------------------------------------------------ reading

def value_of(db, name: str) -> str:
    """The decrypted secret, or "" if absent or unreadable.

    Unreadable happens for one reason worth naming: SESSION_SECRET changed, so
    the key no longer matches what encrypted the row. Returning "" makes that
    look exactly like "not set", which is the truth from the caller's point of
    view - the value cannot be used either way.
    """
    row = db.get(Credential, name)
    if not row:
        return ""
    try:
        return _fernet().decrypt(row.secret.encode("ascii")).decode("utf-8")
    except Exception:                                   # noqa: BLE001
        return ""


def readable(db, name: str) -> bool:
    """Stored, and the key still decrypts it."""
    row = db.get(Credential, name)
    if not row:
        return False
    try:
        _fernet().decrypt(row.secret.encode("ascii"))
        return True
    except Exception:                                   # noqa: BLE001
        return False


def resolve(db, name: str) -> str:
    """What the code should actually use: stored first, environment second."""
    if db is not None:
        stored = value_of(db, name)
        if stored:
            return stored
    return (os.environ.get(name) or "").strip().strip('"').strip("'")


def source_of(db, name: str) -> str:
    """Where resolve() got it: 'stored', 'environment', 'broken', or ''."""
    if db is not None:
        row = db.get(Credential, name)
        if row:
            return "stored" if readable(db, name) else "broken"
    return "environment" if (os.environ.get(name) or "").strip() else ""
