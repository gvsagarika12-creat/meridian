"""Check whether email is actually configured, and optionally send one.

    python test-email.py                  # report the configuration, send nothing
    python test-email.py you@example.com  # actually send a test message there

Sending is opt-in and goes only where you name on the command line. A tool that
emails somebody the moment you run it is not a diagnostic, it is an accident.

The message carries no patient name, no form name and no condition - the same
rule the app's own messages follow.
"""

from __future__ import annotations

import os
import sys

from app import messaging

CHECKS = [
    ("EMAIL_BACKEND", "smtp", "must be 'smtp'; 'file' writes to outbox.log and sends nothing"),
    ("SMTP_HOST", None, "e.g. smtp.gmail.com"),
    ("SMTP_PORT", None, "587 for STARTTLS"),
    ("SMTP_USER", None, "the mailbox you are sending through"),
    ("SMTP_PASSWORD", None, "Gmail: an App Password, not your normal password"),
    ("SMTP_FROM", None, "usually the same as SMTP_USER"),
]


def report() -> bool:
    print("Email configuration\n" + "-" * 58)
    ready = True
    for key, expected, hint in CHECKS:
        raw = os.environ.get(key, "")
        shown = "(not set)" if not raw else (
            f"{len(raw)} characters, hidden" if "PASSWORD" in key else raw)
        ok = bool(raw) and (expected is None or raw == expected)
        ready = ready and ok
        print(f"  {'OK ' if ok else '-- '} {key:16} {shown}")
        if not ok:
            print(f"      {hint}")

    print(f"\nActive backend: {messaging.backend_for("email").name}")
    if not ready:
        print("\nNot ready to send. Edit .env, then restart the app - it reads .env\n"
              "once at startup, so a change made while it is running has no effect.")
    return ready


def send(to: str) -> None:
    print(f"\nSending a test message to {to} ...")
    try:
        messaging.backend_for("email").send(messaging.Message(
            channel="email", to=to,
            subject="Meridian - test message",
            body=("This is a test from the practice application.\n\n"
                  "If you are reading it, outgoing email works and forms sent to "
                  "patients will reach them.\n\n"
                  "No patient information is included in this message."),
        ))
    except messaging.SendFailed as exc:
        print(f"  FAILED: {exc}")
        raise SystemExit(1)
    except Exception as exc:                       # noqa: BLE001 - report anything
        print(f"  FAILED: {type(exc).__name__}: {exc}")
        raise SystemExit(1)
    print("  Sent. Check the inbox, and the spam folder if it is not there.")


if __name__ == "__main__":
    ready = report()
    recipient = sys.argv[1] if len(sys.argv) > 1 else ""
    if not recipient:
        print("\nTo send a real test:  python test-email.py you@example.com")
        raise SystemExit(0 if ready else 1)
    if "@" not in recipient:
        raise SystemExit(f"{recipient!r} is not an email address.")
    send(recipient)
