"""Sending form links by email and SMS.

The hard part here is not the sending. It is deciding what may appear in the message.

**Message bodies name no form and no condition.** Your form library includes
"Edinburgh Scale" (a postnatal depression screen), "MDQ" (bipolar screening) and
"Spravato Consent" (treatment-resistant depression). A text message reading
"complete your Edinburgh Scale" discloses a diagnosis to whoever is holding the
phone, and email and SMS are both unencrypted in transit and at rest on the device.
So the default body says only that the practice has sent a form. `INCLUDE_FORM_NAME`
exists to override that; leaving it off is the safe default and the reason it is off.

**The link is a credential, not a reference.** It is never written to the message log,
because the log is readable by staff who may not be entitled to open that submission.

**Backends are pluggable and default to writing a file.** Nothing is transmitted until
someone configures a real provider, so a development machine cannot accidentally text
a patient. Every provider that carries PHI needs a BAA with you - Twilio, SendGrid,
Mailgun and AWS SES all offer one; using any of them without it is the violation the
BAA exists to prevent.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
import smtplib
import ssl
from dataclasses import dataclass
from datetime import date
from email.message import EmailMessage
from pathlib import Path
from typing import Protocol

ROOT = Path(__file__).resolve().parent.parent
OUTBOX = ROOT / "outbox.log"

# Off by default and deliberately so - see the module docstring.
INCLUDE_FORM_NAME = os.environ.get("MESSAGING_INCLUDE_FORM_NAME", "").lower() in ("1", "true", "yes")


class SendFailed(Exception):
    """Delivery failed. The message is safe to show staff - no PHI, no credentials."""


@dataclass
class Message:
    to: str
    subject: str
    body: str
    channel: str          # "email" | "sms"


# ------------------------------------------------------------------ composing

def compose(channel: str, *, first_name: str, practice: dict, url: str,
            expires: date | None, form_name: str | None) -> Message:
    """Build the message. Says as little as the recipient needs to act."""
    name = first_name or "there"
    practice_name = practice.get("name", "your practice")
    phone = practice.get("phone", "")
    expiry = f" It expires on {expires.strftime('%d %B %Y')}." if expires else ""
    what = f' ("{form_name}")' if (INCLUDE_FORM_NAME and form_name) else ""

    if channel == "sms":
        # Kept short; most carriers split beyond 160 characters and each part bills.
        body = (f"{practice_name}: you have a form to complete{what}. {url}"
                f"{expiry} Questions? {phone}")
        return Message(to="", subject="", body=body, channel="sms")

    body = (
        f"Hello {name},\n\n"
        f"{practice_name} has sent you a form{what} to complete before your appointment.\n\n"
        f"{url}\n"
        f"{('This link expires on ' + expires.strftime('%d %B %Y') + '.') if expires else ''}\n\n"
        f"Please do not forward this link - it opens your form.\n\n"
        f"If you were not expecting this, please call us on {phone}.\n\n"
        f"{practice_name}"
    )
    return Message(to="", subject=f"A form from {practice_name}",
                   body=body, channel="email")


# ------------------------------------------------------------------- backends

class Backend(Protocol):
    name: str
    def send(self, message: Message) -> None: ...


class FileBackend:
    """Default. Writes to outbox.log and transmits nothing.

    A development machine must not be one misconfiguration away from texting a
    real patient, so sending is opt-in rather than opt-out.
    """

    name = "file (nothing is transmitted)"

    def send(self, message: Message) -> None:
        try:
            self._write(OUTBOX, message)
        except OSError:
            # Read-only disk (a serverless host). Fall back to the log stream,
            # which is the only durable place there anyway.
            print(f"{message.channel.upper()} -> {message.to}: "
                  f"{message.subject or '(no subject)'}  [not transmitted]")

    @staticmethod
    def _write(path, message: Message) -> None:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(f"\n{'=' * 70}\n{message.channel.upper()} -> {message.to}\n")
            if message.subject:
                fh.write(f"Subject: {message.subject}\n")
            fh.write(f"{'-' * 70}\n{message.body}\n")


# The name this app gives in EHLO. Passing one explicitly matters more than it
# looks: without it smtplib calls socket.getfqdn(), which does a reverse-DNS
# lookup on the machine's own hostname - and in a serverless sandbox that has no
# stable hostname, that syscall fails with EBUSY before a socket is ever opened.
# The send then reports "could not reach the mail server", which is wrong and
# sends everybody looking at firewalls.
EHLO_NAME = os.environ.get("SMTP_EHLO_NAME", "") or "ipmg-clinic"


class SmtpBackend:
    """Works with SES, SendGrid, Mailgun or any SMTP relay. Needs a BAA."""

    name = "smtp"

    def __init__(self, db=None) -> None:
        self.host = os.environ.get("SMTP_HOST", "")
        self.port = int(os.environ.get("SMTP_PORT", "587"))
        self.user = os.environ.get("SMTP_USER", "")
        self.password = os.environ.get("SMTP_PASSWORD", "")
        self.sender = os.environ.get("SMTP_FROM", "")

    def send(self, message: Message) -> None:
        if not (self.host and self.sender):
            raise SendFailed("SMTP is not configured (SMTP_HOST / SMTP_FROM).")
        if self.user and not self.password:
            raise SendFailed(
                "SMTP_USER is set but SMTP_PASSWORD is empty. Gmail and Office 365 "
                "both need an app password here, not the account password.")

        msg = EmailMessage()
        msg["From"] = self.sender
        msg["To"] = message.to
        msg["Subject"] = message.subject
        msg.set_content(message.body)
        try:
            with smtplib.SMTP(self.host, self.port, timeout=20,
                              local_hostname=EHLO_NAME) as smtp:
                smtp.starttls(context=ssl.create_default_context())
                if self.user:
                    smtp.login(self.user, self.password)
                smtp.send_message(msg)
        except SendFailed:
            raise
        except Exception as exc:
            raise SendFailed(self._explain(exc)) from exc

    def _explain(self, exc: Exception) -> str:
        """Turn the library's exception into something a person can act on.

        "SMTPAuthenticationError" tells staff nothing about what to do next, and
        "email could not be sent" tells them less. None of these mention the
        recipient or the message, so none of them carry PHI.
        """
        name = type(exc).__name__
        if isinstance(exc, smtplib.SMTPAuthenticationError):
            return (f"{self.host} rejected the username or password. For Gmail this "
                    f"is almost always an app password that is missing, expired, or "
                    f"still has its spaces in it.")
        if isinstance(exc, smtplib.SMTPRecipientsRefused):
            return "The mail server refused the recipient address."
        if isinstance(exc, smtplib.SMTPSenderRefused):
            return (f"The mail server refused {self.sender} as the sender. It usually "
                    f"has to match SMTP_USER.")
        if isinstance(exc, (TimeoutError, OSError)) and "getaddrinfo" in str(exc):
            return f"Could not resolve {self.host}. Check SMTP_HOST and the connection."
        if isinstance(exc, (TimeoutError, ConnectionRefusedError, OSError)):
            return (f"Could not reach {self.host}:{self.port}. A firewall or the "
                    f"network may be blocking outbound SMTP on that port.")
        return f"Email could not be sent ({name})."


class ResendBackend:
    """Email over HTTPS instead of SMTP.

    An alternative, not a workaround. Vercel blocks outbound port 25 only; 587
    and 465 are open, and SmtpBackend does work from there - this was verified
    against the live deployment after an earlier note here wrongly claimed
    otherwise. What this backend buys is not reachability but independence from
    a mail server: one HTTPS call, no handshake to fail, and no relay to keep
    credentials for.

    The same practice address can still be the sender: Resend delivers from a
    domain you verify with them, so mail continues to come from the practice
    rather than from a third party's address.

    Needs a BAA before real patient mail, the same as every other provider.
    """

    name = "resend (https)"
    ENDPOINT = "https://api.resend.com/emails"

    def __init__(self, db=None) -> None:
        self.key = os.environ.get("RESEND_API_KEY", "")
        # Falls back to SMTP_FROM so one address configures both backends.
        self.sender = (os.environ.get("RESEND_FROM")
                       or os.environ.get("SMTP_FROM", ""))

    def send(self, message: Message) -> None:
        if not self.key:
            raise SendFailed("RESEND_API_KEY is not set.")
        if not self.sender:
            raise SendFailed("No sender address (RESEND_FROM or SMTP_FROM).")

        payload = json.dumps({
            "from": self.sender,
            "to": [message.to],
            "subject": message.subject,
            "text": message.body,
        }).encode()
        request = urllib.request.Request(
            self.ENDPOINT, data=payload, method="POST",
            headers={"Authorization": f"Bearer {self.key}",
                     "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                if response.status >= 300:
                    raise SendFailed(f"Email rejected (HTTP {response.status}).")
        except SendFailed:
            raise
        except urllib.error.HTTPError as exc:
            raise SendFailed(self._explain(exc)) from exc
        except Exception as exc:
            raise SendFailed(f"Email could not be sent ({type(exc).__name__}).") from exc

    def _explain(self, exc: "urllib.error.HTTPError") -> str:
        """Resend's own words, when they are safe to repeat.

        Its error bodies describe the request - an unverified domain, a bad key,
        a malformed address - and never echo the message, so nothing here can
        leak what was being sent or to whom beyond what staff already know.
        """
        try:
            detail = json.loads(exc.read().decode("utf-8", "replace")).get("message", "")
        except Exception:                                   # noqa: BLE001
            detail = ""
        if exc.code in (401, 403):
            return ("Resend rejected the API key. Check RESEND_API_KEY, and that "
                    "it has not been revoked." + (f" ({detail})" if detail else ""))
        if exc.code == 422:
            return (f"Resend refused the message: {detail or 'invalid request'}. "
                    f"The sending domain usually has to be verified first.")
        if exc.code == 429:
            return "Resend rate limit reached. Try again shortly."
        return f"Email rejected (HTTP {exc.code}){f': {detail}' if detail else ''}."


class TwilioBackend:
    """SMS via Twilio's REST API - no SDK, just a POST. Needs a BAA and 10DLC."""

    name = "twilio"

    def __init__(self, db=None) -> None:
        self.sid = os.environ.get("TWILIO_ACCOUNT_SID", "")
        self.token = os.environ.get("TWILIO_AUTH_TOKEN", "")
        self.sender = os.environ.get("TWILIO_FROM", "")

    def send(self, message: Message) -> None:
        if not (self.sid and self.token and self.sender):
            raise SendFailed("Twilio is not configured (TWILIO_ACCOUNT_SID / _AUTH_TOKEN / _FROM).")
        import requests
        try:
            r = requests.post(
                f"https://api.twilio.com/2010-04-01/Accounts/{self.sid}/Messages.json",
                auth=(self.sid, self.token),
                data={"From": self.sender, "To": message.to, "Body": message.body},
                timeout=20,
            )
            if r.status_code >= 300:
                # Twilio echoes the body in errors; never surface that to staff.
                raise SendFailed(f"SMS rejected by provider (HTTP {r.status_code}).")
        except SendFailed:
            raise
        except Exception as exc:
            raise SendFailed(f"SMS could not be sent: {type(exc).__name__}") from exc


def backend_for(channel: str, db=None) -> Backend:
    """Chosen by EMAIL_BACKEND / SMS_BACKEND. Unset means the file backend."""
    choice = os.environ.get(
        "EMAIL_BACKEND" if channel == "email" else "SMS_BACKEND", "file")
    if channel == "email" and choice == "smtp":
        return SmtpBackend(db)
    if channel == "email" and choice == "resend":
        return ResendBackend(db)
    if channel == "sms" and choice == "twilio":
        return TwilioBackend(db)
    return FileBackend()


def describe(db=None) -> dict[str, str]:
    return {"email": backend_for("email", db).name,
            "sms": backend_for("sms", db).name,
            "include_form_name": "yes" if INCLUDE_FORM_NAME else "no"}
