"""Set working passwords on the app's own built-in demo accounts.

    python seed_demo_credentials.py

seed.py already creates four staff identities - admin@example.com,
clinician@example.com, coordinator@example.com, compliance@example.com -
one per role, so every permission tier has something to sign in as. But
it creates them with no password at all (`User.password_hash` defaults
to ""), so none of them can actually log in until somebody sets one.

This sets a password on the two seeded identities a walkthrough actually
needs - Admin and Doctor - and issues patient portal access for Maria
Alvarez, the first seeded demo patient.

*** No password is hardcoded here. Deliberately - a script committed to
a repository is a script anyone with read access to that repository can
read, and this one is meant to run against a real, reachable deployment.
Set MERIDIAN_DEMO_ADMIN_PASSWORD / _DOCTOR_PASSWORD / _PATIENT_PASSWORD
in the environment before running it, or leave any of them unset and a
random one is generated and printed once, the same temp-password pattern
users.py already uses for a real new member of staff. Write down what
gets printed - it is not stored anywhere else, including in this
terminal's scrollback once you close it. ***

Safe to re-run: it always resets these three, on purpose - a demo
credential nobody's certain still matches what was last written down is
worse than one you know you can regenerate on demand.
"""

from __future__ import annotations

import os
from datetime import datetime

from app import auth
from app.models import Client, SessionLocal, User, init_db
from app.users import temp_password

ADMIN_EMAIL = "admin@example.com"
DOCTOR_EMAIL = "clinician@example.com"
PATIENT_EMAIL = "m.alvarez@example.com"


def _password(env_var: str) -> str:
    return os.environ.get(env_var) or temp_password()


def main() -> None:
    init_db()
    db = SessionLocal()

    admin = db.query(User).filter_by(email=ADMIN_EMAIL).first()
    if admin is None:
        print(f"No user {ADMIN_EMAIL} - run seed.py first.")
    else:
        password = _password("MERIDIAN_DEMO_ADMIN_PASSWORD")
        admin.password_hash = auth.hash_password(password)
        admin.must_change_password = False
        admin.is_active = True
        print(f"admin  -> {admin.email} / {password}  (role: {admin.role.value})")

    doctor = db.query(User).filter_by(email=DOCTOR_EMAIL).first()
    if doctor is None:
        print(f"No user {DOCTOR_EMAIL} - run seed.py first.")
    else:
        password = _password("MERIDIAN_DEMO_DOCTOR_PASSWORD")
        doctor.password_hash = auth.hash_password(password)
        doctor.must_change_password = False
        doctor.is_active = True
        print(f"doctor -> {doctor.email} / {password}  (role: {doctor.role.value})")

    patient = db.query(Client).filter_by(email=PATIENT_EMAIL).first()
    if patient is None:
        print(f"No client {PATIENT_EMAIL} - run seed.py first.")
    else:
        password = _password("MERIDIAN_DEMO_PATIENT_PASSWORD")
        patient.portal_password_hash = auth.hash_password(password)
        patient.portal_issued_at = datetime.utcnow()
        print(f"patient-> {patient.email} / {password}  ({patient.name}, "
             f"client {patient.id})")

    db.commit()
    db.close()


if __name__ == "__main__":
    main()
