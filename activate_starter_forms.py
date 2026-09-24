"""Attach the general consents, and switch on the two fully-written forms.

    python activate_starter_forms.py

seed.py creates 20 consent documents and 9 questionnaires, but only wires
a handful of them together - most consents start attached to nothing, and
most questionnaires start (or were later switched) inactive, so Send only
ever offered one or two real choices. This does two things, idempotently:

1. Attaches the nine consents that apply practice-wide - privacy practice,
   financial policy, telemedicine, and so on - to the "Consent Forms"
   questionnaire, so sending it actually shows all nine, not zero.

2. Activates "Adult Packet W/Consent" (11 real questions, 10 pages) and
   "Minor Packet (Kareo Supplement)" (10 real questions, 8 pages, 6
   consents already attached) - both checked and confirmed to have real
   content, not placeholder stubs.

Deliberately NOT touched, and why:

* The "SP -" prefixed consents (Spanish-language versions), Spravato
  Consent (treatment-specific), and FAMILY COURT/MEDIATION/HEALTH
  ASSESSMENT (situational) - attaching these to a general consent packet
  is a real clinical/legal decision, not a default a script should make.
* "Minor Packet W/Consent" - a same-named but DIFFERENT form from the one
  activated above, with exactly one placeholder question. Activating it
  would put a stub in front of a patient.
* Edinburgh Scale, MDQ, ASRM, ROI - still single-question placeholders in
  the seed data itself, never transcribed from the real forms.

Safe to re-run: attaching an already-attached consent, or activating an
already-active form, is a no-op.
"""

from __future__ import annotations

from app.models import ConsentForm, Form, FormConsent, SessionLocal, init_db

GENERAL_CONSENTS = [
    "Acknowledgment of Privacy Practice",
    "Consumer Notice of Rights and Responsibilities",
    "Financial Policy",
    "IPMG Release of Information",
    "IPMG Social Media Release Form",
    "Locations",
    "Mental Health Disclosure Form",
    "PROFESSIONAL SERVICE & DOCUMENTATION FEES & DISCLAIMER",
    "Telemedicine",
]

CONSENT_FORMS_QUESTIONNAIRE = "Consent Forms"

FORMS_TO_ACTIVATE = [
    "Adult Packet W/Consent",
    "Minor Packet (Kareo Supplement)",
]


def main() -> None:
    init_db()
    db = SessionLocal()

    form = db.query(Form).filter_by(name=CONSENT_FORMS_QUESTIONNAIRE).first()
    if form is None:
        print(f'No form named {CONSENT_FORMS_QUESTIONNAIRE!r} - nothing to attach to.')
    else:
        attached = {fc.consent_id for fc in form.consents}
        for name in GENERAL_CONSENTS:
            consent = db.query(ConsentForm).filter_by(name=name).first()
            if consent is None:
                print(f'  no consent document named {name!r} - skipped')
                continue
            if consent.id in attached:
                print(f'  already attached: {name!r}')
                continue
            db.add(FormConsent(form_id=form.id, consent_id=consent.id))
            print(f'  attached: {name!r}')

    for name in FORMS_TO_ACTIVATE:
        f = db.query(Form).filter_by(name=name).first()
        if f is None:
            print(f'No form named {name!r} - skipped')
        elif f.is_active:
            print(f'already active: {name!r}')
        else:
            f.is_active = True
            print(f'activated: {name!r}')

    db.commit()
    db.close()


if __name__ == "__main__":
    main()
