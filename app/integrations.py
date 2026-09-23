"""The systems this app talks to, and whether it can reach them.

Every check here makes a real network call. None of them reports success from
the presence of a credential: a key sitting in a variable proves only that
somebody typed something, and a screen that turns green on that basis is worse
than no screen - it is a screen that lies on the day it matters.

So each connection has three honest states:

    not configured   the credentials are absent, and we say which
    untested         configured, but nobody has pressed the button
    a real result    what the far end actually said, quoted

Two of the four are implemented against live APIs and two are not yet. That is
stated on the screen rather than hidden, because a client being shown this
deserves to know which lights are real.
"""

from __future__ import annotations

import json
import os
import smtplib
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime

from . import credentials as creds


@dataclass
class Result:
    ok: bool
    summary: str
    detail: str = ""
    at: datetime = field(default_factory=datetime.utcnow)


@dataclass
class Connection:
    key: str
    name: str
    purpose: str
    settings: list[tuple[str, str, bool]]     # (variable, what it is, is a secret)
    implemented: bool
    note: str = ""
    #  Whether the credentials can be typed into the screen. Only the two that
    #  are still waiting on the provider are editable. Mail already works from
    #  its environment variables, and moving a working credential to a second
    #  source to gain an input box nobody needs is a change with a downside and
    #  no upside.
    editable: bool = False

    def missing(self, db=None) -> list[str]:
        return [var for var, _what, _secret in self.settings
                if not creds.resolve(db, var)]

    def configured(self, db=None) -> bool:
        return not self.missing(db)

    def shown(self, db=None) -> list[tuple[str, str, str, bool, str]]:
        """Each setting as (variable, what it is, value, is a secret, source).

        The source is carried through because a value can now come from two
        places. "Which of the two am I looking at?" is the first question
        anybody asks when a credential is not behaving, and leaving them to
        infer it from behaviour is how an afternoon disappears.
        """
        out = []
        for var, what, secret in self.settings:
            raw = creds.resolve(db, var)
            where = creds.source_of(db, var)
            #  A value still wrapped in quotation marks is a real and nearly
            #  invisible fault. A .env file writes SMTP_HOST="mail.example.com"
            #  and the quotes are file syntax; python-dotenv strips them, so the
            #  practice copy works. Piping that same line into a hosting
            #  provider stores the quotes as part of the value, and the hosted
            #  copy then tries to resolve a hostname beginning with a quote.
            #  The resulting error names neither the quote nor the variable, so
            #  say so here rather than leave it to be found by exhaustion.
            quoted = len(raw) > 1 and raw[0] == raw[-1] and raw[0] in "\"'"
            if where == "broken":
                value = "stored, but cannot be decrypted - set it again"
            elif not raw:
                value = "not set"
            elif secret:
                value = (f"{len(raw)} characters, hidden"
                         + (" - WRAPPED IN QUOTES, probably wrong" if quoted else ""))
            else:
                value = raw + ("   <- WRAPPED IN QUOTES, probably wrong" if quoted else "")
            out.append((var, what, value, secret, where))
        return out


CONNECTIONS = [
    Connection(
        key="intakeq",
        editable=True,
        name="IntakeQ",
        purpose="Pull patient submissions and form definitions from the practice's "
                "existing IntakeQ account, so paperwork already filled in there "
                "does not have to be re-entered here.",
        settings=[("INTAKEQ_API_KEY", "API key from Settings, Integrations", True)],
        implemented=True,
    ),
    Connection(
        key="tebra",
        editable=True,
        name="Tebra",
        purpose="Read the clinical chart - medications, diagnoses, past visits - so "
                "what a patient reports on a form can be checked against what the "
                "practice already holds.",
        settings=[("TEBRA_CUSTOMER_KEY", "Customer key from Tebra support", True),
                  ("TEBRA_USER", "API user, usually an email address", False),
                  ("TEBRA_PASSWORD", "Password for that API user", True)],
        implemented=True,
        note="Tebra's API is SOAP and the account has to be enabled for API access "
             "by their support team first. This check has never run against a live "
             "account, so treat its first result as information about the "
             "credentials, not proof the integration works.",
    ),
    Connection(
        key="email",
        name="Email",
        purpose="Send patients the link to their form.",
        settings=[("EMAIL_BACKEND", "smtp, resend, or file", False),
                  ("SMTP_HOST", "Mail server", False),
                  ("SMTP_PORT", "587 submission, 465 implicit TLS", False),
                  ("SMTP_USER", "Mailbox", False),
                  ("SMTP_PASSWORD", "Password or app password", True),
                  ("SMTP_FROM", "Address patients see", False)],
        implemented=True,
    ),
]

BY_KEY = {c.key: c for c in CONNECTIONS}


# --- the checks ------------------------------------------------------------

def check_intakeq(db=None) -> Result:
    """Ask IntakeQ for one questionnaire. Any answer proves the key works."""
    key = creds.resolve(db, "INTAKEQ_API_KEY")
    if not key:
        return Result(False, "No API key set.",
                      "Add INTAKEQ_API_KEY. In IntakeQ: Settings, Integrations, "
                      "Developer API.")
    request = urllib.request.Request(
        "https://intakeq.com/api/v1/questionnaires",
        headers={"X-Auth-Key": key, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            body = response.read(200_000)
        forms = json.loads(body or b"[]")
        return Result(True, f"Connected. {len(forms)} questionnaire(s) visible.",
                      ", ".join(str(f.get("Name", "?")) for f in forms[:5]))
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return Result(False, "IntakeQ rejected the key.",
                          "Check INTAKEQ_API_KEY, and that the key has not been "
                          "revoked in Settings, Integrations.")
        return Result(False, f"IntakeQ replied HTTP {exc.code}.", exc.reason or "")
    except Exception as exc:                            # noqa: BLE001
        return Result(False, "Could not reach intakeq.com.", type(exc).__name__)


TEBRA_ENDPOINT = "https://webservice.kareo.com/services/soap/2.1/KareoServices.svc"
TEBRA_ENVELOPE = """<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"
               xmlns:kareo="http://www.kareo.com/api/schemas/">
  <soap:Body>
    <kareo:GetPractices>
      <kareo:request>
        <kareo:RequestHeader>
          <kareo:CustomerKey>{customer_key}</kareo:CustomerKey>
          <kareo:Password>{password}</kareo:Password>
          <kareo:User>{user}</kareo:User>
        </kareo:RequestHeader>
        <kareo:Fields><kareo:PracticeName>true</kareo:PracticeName></kareo:Fields>
      </kareo:request>
    </kareo:GetPractices>
  </soap:Body>
</soap:Envelope>"""


def check_tebra(db=None) -> Result:
    """Ask Tebra to list the practices this API user can see.

    GetPractices is the smallest call that proves all three credentials at once
    and reads nothing about any patient - a connection test should not touch a
    medical record to prove it can reach the server.
    """
    key = creds.resolve(db, "TEBRA_CUSTOMER_KEY")
    user = creds.resolve(db, "TEBRA_USER")
    password = creds.resolve(db, "TEBRA_PASSWORD")
    absent = [n for n, v in (("TEBRA_CUSTOMER_KEY", key), ("TEBRA_USER", user),
                             ("TEBRA_PASSWORD", password)) if not v]
    if absent:
        return Result(False, "Credentials missing.", "Not set: " + ", ".join(absent))

    def escape(value: str) -> str:
        return (value.replace("&", "&amp;").replace("<", "&lt;")
                     .replace(">", "&gt;"))

    body = TEBRA_ENVELOPE.format(customer_key=escape(key), user=escape(user),
                                 password=escape(password)).encode("utf-8")
    request = urllib.request.Request(
        TEBRA_ENDPOINT, data=body, method="POST",
        headers={"Content-Type": "text/xml; charset=utf-8",
                 "SOAPAction": "http://www.kareo.com/api/schemas/KareoServices/GetPractices"})
    try:
        with urllib.request.urlopen(request, timeout=25) as response:
            text = response.read(200_000).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read(2000).decode("utf-8", "replace") if exc.fp else ""
        # A SOAP fault arrives as HTTP 500 with the reason inside it.
        reason = ""
        if "<faultstring>" in detail:
            reason = detail.split("<faultstring>", 1)[1].split("</faultstring>", 1)[0]
        return Result(False, f"Tebra replied HTTP {exc.code}.",
                      reason or (exc.reason or "")[:200])
    except Exception as exc:                            # noqa: BLE001
        return Result(False, "Could not reach webservice.kareo.com.",
                      type(exc).__name__)

    if "<ErrorMessage>" in text:
        message = text.split("<ErrorMessage>", 1)[1].split("</ErrorMessage>", 1)[0]
        if message.strip():
            return Result(False, "Tebra rejected the credentials.", message[:300])
    count = text.count("<PracticeData")
    return Result(True, f"Connected. {count} practice(s) visible."
                        if count else "Connected. Tebra answered with no practices.",
                  "GetPractices returned without an error.")


def check_email(db=None) -> Result:
    backend = (os.environ.get("EMAIL_BACKEND") or "file").strip()
    if backend == "file":
        return Result(False, "Not sending.",
                      "EMAIL_BACKEND is 'file' - messages are written to a log and "
                      "nothing is transmitted.")
    if backend == "resend":
        key = (os.environ.get("RESEND_API_KEY") or "").strip()
        if not key:
            return Result(False, "RESEND_API_KEY is not set.", "")
        request = urllib.request.Request(
            "https://api.resend.com/domains",
            headers={"Authorization": f"Bearer {key}"})
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                response.read(50_000)
            return Result(True, "Resend accepted the API key.", "Sends over HTTPS.")
        except urllib.error.HTTPError as exc:
            return Result(False, f"Resend replied HTTP {exc.code}.",
                          "Check RESEND_API_KEY." if exc.code in (401, 403) else "")
        except Exception as exc:                        # noqa: BLE001
            return Result(False, "Could not reach api.resend.com.", type(exc).__name__)

    host = (os.environ.get("SMTP_HOST") or "").strip()
    if not host:
        return Result(False, "SMTP_HOST is not set.", "")
    port = int(os.environ.get("SMTP_PORT") or 587)
    user = (os.environ.get("SMTP_USER") or "").strip()
    password = os.environ.get("SMTP_PASSWORD") or ""
    try:
        from .messaging import EHLO_NAME
        with smtplib.SMTP(host, port, timeout=20,
                          local_hostname=EHLO_NAME) as smtp:
            smtp.ehlo()
            smtp.starttls(context=ssl.create_default_context())
            if user:
                smtp.login(user, password)
        return Result(True, f"Connected to {host}:{port} and signed in.",
                      "Nothing was sent.")
    except smtplib.SMTPAuthenticationError:
        return Result(False, f"{host} rejected the username or password.", "")
    except Exception as exc:                            # noqa: BLE001
        # Name the exception. "Could not reach" covers a DNS failure, a refused
        # connection and a timeout, which need three different fixes - and
        # guessing which one it was is how a wrong explanation gets repeated.
        return Result(False, f"Could not reach {host}:{port}.",
                      f"{type(exc).__name__}: {str(exc)[:160]}")


CHECKS = {"intakeq": check_intakeq, "tebra": check_tebra,
          "email": check_email}


def run(key: str, db=None) -> Result:
    check = CHECKS.get(key)
    if not check:
        return Result(False, "No such connection.", "")
    return check(db)
