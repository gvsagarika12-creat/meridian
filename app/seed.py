"""Seed the database with the practice's form structure.

Branding is NOT here - it lives in config/practice.json, outside version control.

Form names, folder names and consent form names are the actual ones from the account.
Question text comes from the visible part of the Minor Packet. Client names are
invented - no real patient data is in this file.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from .clinical import (SEED, Allergy, ClinicalNote, LabOrder, Medication,
                       Problem, VitalSigns)
from .packets import ADULT_PACKET
from .models import (
    AuditEvent, Client, ConsentForm, Folder, Form, FormConsent, Question,
    QuestionItem,
    QuestionType, SessionLocal, Submission, SubmissionStatus, User, UserRole, init_db,
)

# Card stripe colours, cycling the way the IntakeQ grid does.
COLOURS = ["#9aa5b1", "#9aa5b1", "#3d9ad1", "#7cb342", "#e0605a", "#8e6bb5", "#e8b93d"]

FORMS = [
    # Three of these are fully populated so there is something real to click through;
    # the rest exist so the card grid and folders look like the real account.
    ("Minor Packet (Kareo Supplement)", False),
    ("Adult Packet W/Consent", True),
    ("Consent Forms", True),
    ("Minor Packet W/Consent", True),
    ("Edinburgh Scale", True),
    ("MDQ", True),
    ("Altman Self-Rating Mania Scale (ASRM)", True),
    ("Release Of Information - ROI", True),
]

FOLDERS = [
    "ADHD FOR KIDS AND TEACHERS",
    "AfterVisit Forms",
    "Group Therapy Questionnaires",
    "LANCASTER THERAPY PATIENTS",
    "MJM- PMHNP",
    "Nutritional Assessment And Management",
    "Other",
    "PreVisit Forms",
    "Research Forms",
    "Spravato",
    "Transcranial Magnetic Stimulation Q...",
    "Transgender Services",
    "Umbrella Consent Form Research",
]

CONSENTS = [
    "HIPAA Compliant Questionnaire",
    "TMS Communication Consent",
    "TMS Questionnaire Consent",
    "TMS Not Interested",
    "Talent Release Form",
    "Genetic Testing MTHFR Gene",
    "Mental Health Disclosure Form",
    "PROFESSIONAL SERVICE & DOCUMENTATION FEES & DISCLAIMER",
    "Social Media Release Form",
    "SP - Acknowledgment of Privacy Practice",
    "SP - Consent to Treat Children - Copy",
    "FAMILY COURT/MEDIATION/HEALTH ASSESSMENT",
    "Financial Policy",
    "Research Release Form",
    "Consumer Notice of Rights and Responsibilities",
    "Locations",
    "Acknowledgment of Privacy Practice",
    "Telemedicine",
    "Release of Information",
    "Spravato Consent",
]

# All ten questions of the Minor Packet, transcribed from the real editor screen.
# Tuple shape: (text, type, required, page, options, rows)
MINOR_PACKET_QUESTIONS = [
    ("Please enter patient's information.",
     QuestionType.address, True, 1, "", ""),

    ("Please state the patient's presenting problems",
     QuestionType.long_text, True, 1, "", ""),

    ("Please take a few minutes to complete the following. Check the number that "
     "applies to you. The numbers range from 0 meaning not present through 4 meaning "
     "severe problem",
     QuestionType.matrix, True, 2,
     "0\n1\n2\n3\n4",
     # PLACEHOLDER rows - the symptom list was off-screen in the screenshot.
     # Replace these with the real items from the printed Minor Packet.
     "Depressed mood\nLoss of interest\nAnxiety or worry\nSleep disturbance\n"
     "Appetite change\nIrritability\nConcentration difficulty\nFatigue\n"
     "Thoughts of self-harm"),

    ("Medical History Please fill the following in detail using the other option for "
     "the conditions not listed. Please type N/A where needed.",
     QuestionType.checkbox, False, 3,
     "Asthma\nDiabetes\nHypertension\nSeizures\nThyroid disorder\nOther", ""),

    ("Does patient use any of the following? Answer specific frequency, quantity, "
     "form of use, start age / for how long, if sober for how long and relapse "
     "reasons if any, etc.",
     QuestionType.checkbox, False, 4,
     "Alcohol\nNicotine\nCannabis\nStimulants\nOpioids\nNone", ""),

    ("Please describe in detail if there were significant events around the birth "
     "of the child.",
     QuestionType.long_text, False, 5, "", ""),

    ("Developmental History (Continued) Please answer yes or no if there was a delay "
     "in child milestones. If yes, was any intervention done?",
     QuestionType.long_text, False, 6, "", ""),

    ("Developmental History (Continued) If any of the following questions are not "
     "applicable, please answer N/A",
     QuestionType.long_text, False, 6, "", ""),

    ("Education History If any of the following questions are not applicable, please "
     "answer N/A",
     QuestionType.long_text, False, 7, "", ""),

    # CAGE-AID is four yes/no items sharing one scale - a matrix, not free text.
    ("CAGE-AID",
     QuestionType.matrix, False, 8,
     "Yes\nNo",
     "Have you ever felt you ought to cut down on your drinking or drug use?\n"
     "Have people annoyed you by criticizing your drinking or drug use?\n"
     "Have you ever felt bad or guilty about your drinking or drug use?\n"
     "Have you ever had a drink or used drugs first thing in the morning?"),
]


# --------------------------------------------------------------------------
# Consent Forms - a short packet that is mostly attestations plus a signature.
# Also not transcribed; the consent NAMES are real, the wording is not.
# --------------------------------------------------------------------------
CONSENT_PACKET_QUESTIONS = [
    ("Please enter patient's information.",
     QuestionType.address, True, 1, "", ""),

    ("Date of birth", QuestionType.date, True, 1, "", ""),

    ("Please read each document below and tick to confirm you have read and agree "
     "to it.",
     QuestionType.checkbox, True, 2,
     "Acknowledgment of Privacy Practice\nFinancial Policy\n"
     "Consumer Notice of Rights and Responsibilities\nTelemedicine\n"
     "Release of Information", ""),

    ("How would you prefer we contact you?",
     QuestionType.radio, True, 3,
     "Email\nText message\nPhone call\nPost", ""),

    ("I consent to being contacted on the method selected above.",
     QuestionType.attestation, True, 3, "", ""),

    ("Type your full name to sign.",
     QuestionType.signature, True, 4, "", ""),

    ("Relationship to patient, if signing on their behalf",
     QuestionType.short_text, False, 4, "", ""),
]


def seed() -> None:
    init_db()
    s = SessionLocal()
    if s.query(Form).count():
        s.close()
        return

    # Demo accounts for trying the roles out. None has a password, so none can
    # sign in until the owner sets one from Staff -> Reset password. The owner
    # account itself is created by the first-run setup screen, not here - a
    # shipped account with a shipped password is a shipped vulnerability.
    s.add_all([
        User(email="admin@example.com", name="Practice Admin", role=UserRole.admin),
        User(email="clinician@example.com", name="Dr Clinician",
             role=UserRole.practitioner),
        User(email="coordinator@example.com", name="Research Coordinator",
             role=UserRole.front_desk),
        User(email="compliance@example.com", name="Compliance Auditor",
             role=UserRole.read_only),
    ])

    folders = [Folder(name=n, position=i) for i, n in enumerate(FOLDERS)]
    s.add_all(folders)

    consents = [ConsentForm(name=n) for n in CONSENTS]
    s.add_all(consents)
    s.flush()

    forms = []
    for i, (name, active) in enumerate(FORMS):
        f = Form(name=name, is_active=active, colour=COLOURS[i % len(COLOURS)])
        forms.append(f)
        s.add(f)
    s.flush()

    # Give the Minor Packet its real questions; everything else gets a placeholder
    # so the editor has something to show.
    minor = forms[0]
    for pos, (text, qtype, required, page, opts, rows) in enumerate(MINOR_PACKET_QUESTIONS):
        s.add(Question(form_id=minor.id, text=text, qtype=qtype, required=required,
                       position=pos, page=page, options_raw=opts, rows_raw=rows))
    # Adult Packet - transcribed from the real editor, including every sub-field
    # of its Mixed Controls blocks.
    for pos, (text, qtype, required, page, opts, rows, items) in enumerate(ADULT_PACKET):
        q = Question(form_id=forms[1].id, text=text, qtype=qtype, required=required,
                     position=pos, page=page, options_raw=opts, rows_raw=rows)
        s.add(q)
        s.flush()
        for ipos, spec in enumerate(items):
            label, kind, iopts, width = spec[:4]
            required = spec[4] if len(spec) > 4 else False
            s.add(QuestionItem(question_id=q.id, label=label, kind=kind,
                               options_raw=iopts, position=ipos, width=width,
                               required=required))

    for pos, (text, qtype, required, page, opts, rows) in enumerate(CONSENT_PACKET_QUESTIONS):
        s.add(Question(form_id=forms[2].id, text=text, qtype=qtype, required=required,
                       position=pos, page=page, options_raw=opts, rows_raw=rows))

    # The remainder are placeholders - enough to fill the grid, nothing more.
    for f in forms[3:]:
        s.add(Question(form_id=f.id, text="Please enter patient's information.",
                       qtype=QuestionType.address, required=True, position=0, page=1))

    for c in consents[:6]:
        s.add(FormConsent(form_id=minor.id, consent_id=c.id))

    clients = [
        Client(first_name="Maria", last_name="Alvarez", email="m.alvarez@example.com",
               phone="(909) 555-0142", city="Redlands", state="CA", postal_code="92373",
               allow_email=True, allow_sms=True, contact_preference="email"),
        Client(first_name="Daniel", last_name="Okafor", email="d.okafor@example.com",
               phone="(909) 555-0177", city="Loma Linda", state="CA", postal_code="92354",
               allow_email=True, allow_sms=False, contact_preference="email"),
        Client(first_name="Priya", last_name="Raman", email="p.raman@example.com",
               phone="(951) 555-0198", city="Riverside", state="CA", postal_code="92501",
               allow_email=False, allow_sms=True, contact_preference="sms"),
        Client(first_name="Thomas", last_name="Bell", email="t.bell@example.com",
               phone="(909) 555-0110", city="Yucaipa", state="CA", postal_code="92399",
               allow_email=False, allow_sms=False, contact_preference="none"),
    ]
    s.add_all(clients)
    s.flush()

    now = datetime.utcnow()
    # These carry no answers, so they must not claim to be submitted. A row that
    # says "submitted" and opens empty reads as a bug in the app rather than as
    # seed data, and sends whoever sees it looking for the missing answers.
    # In progress is the truth: sent, and in one case opened but not finished.
    in_progress = [
        (clients[0], forms[1], SubmissionStatus.opened, 5),
        (clients[1], forms[2], SubmissionStatus.sent, 5),
        (clients[2], forms[4], SubmissionStatus.sent, 6),
        (clients[3], forms[5], SubmissionStatus.opened, 9),
    ]
    for client, form, status, hours in in_progress:
        s.add(Submission(form_id=form.id, client_id=client.id, status=status,
                         form_version=form.version, read_by_staff=False,
                         sent_at=now - timedelta(hours=hours + 1)))

    pending = [
        (clients[1], forms[0], 0.05), (clients[2], forms[3], 9),
        (clients[3], forms[6], 10), (clients[0], forms[7], 10),
    ]
    for client, form, hours in pending:
        s.add(Submission(form_id=form.id, client_id=client.id, form_version=form.version,
                         status=SubmissionStatus.sent, sent_at=now - timedelta(hours=hours)))

    events = [
        ("Form Sent", 0.05), ("Client Logged In", 0.1), ("Form Submitted", 5),
        ("Consent Form Signed", 5), ("Client Logged In", 5), ("Form Submitted", 5),
        ("Consent Form Signed", 5), ("Client Logged In", 5), ("Form Submitted", 6),
        ("Consent Form Signed", 7),
    ]
    for action, hours in events:
        s.add(AuditEvent(action=action, at=now - timedelta(hours=hours)))

    # ---- clinical record, transcribed from the Tebra facesheet screenshots ----
    # Demo patient only. The medication and diagnosis lists are the real ones
    # from the screenshot; the patient they belonged to is not in this file.
    maria = clients[0]
    rx = date(2026, 8, 25)
    for nm, dose, fm in [("Adderall", "20 mg", "tablet"),
                         ("cloNIDine HCL", "0.1 mg", "tablet"),
                         ("Xanax", "0.5 mg", "tablet"),
                         ("PROzac", "20 mg", "capsule")]:
        s.add(Medication(client_id=maria.id, source=SEED, name=nm, dose=dose, form=fm,
                         prescribed_on=rx, is_active=True))
    s.add(Medication(client_id=maria.id, source=SEED, name="Sertraline", dose="50 mg", form="tablet",
                     prescribed_on=date(2025, 11, 2), stopped_on=date(2026, 7, 30),
                     is_active=False, notes="Switched to fluoxetine"))

    for desc, code in [("Recurrent major depression (disorder)", "F33.1"),
                       ("Circadian rhythm sleep disorder, shift work type", "G47.26"),
                       ("Attention deficit hyperactivity disorder, combined type (disorder)", "F90.2"),
                       ("Generalized anxiety disorder (disorder)", "F41.1"),
                       ("Recurrent depression (disorder)", "F33.9")]:
        s.add(Problem(client_id=maria.id, description=desc, icd10=code, source=SEED))

    s.add(VitalSigns(client_id=maria.id, taken_on=date(2022, 8, 8), bp="120 / 86",
                     hr="89 bpm", temp="97 F", height="5' 10\"", weight="177 lbs 0 oz",
                     bmi="25.39", spo2="99%"))

    # Free-text history, as the facesheet shows it. Newlines are real line breaks
    # in the chart, so they are written with chr(10) joins rather than escapes.
    s.add(ClinicalNote(client_id=maria.id, kind="PMHx", body="\n".join([
        "Asthma",
        "Depression",
        "Mole(s)",
        "Comments: presenting problems - severe bipolar depression, OCD "
        "(obsessive-compulsive disorder), ADHD (attention deficit hyperactivity "
        "disorder). Past medical history: BP. Past psych history: no. "
        "Medications: yes - fluoxetine 20 mg, Vyvanse 30 mg, "
        "hydrochlorothiazide 12.5 mg.",
    ])))

    s.add(ClinicalNote(client_id=maria.id, kind="SHx", body="\n".join([
        "Alcohol: Occasional drink",
        "Birth Gender: Female",
        "Cardiovascular: Eat healthy meals / Regular exercise",
        "Drug Abuse: No illicit drug use",
        "Safety: Household smoke detector / Wear seatbelts",
        "Sexual Activity: Not sexually active / Safe sex practices",
        "Tobacco: Never smoker",
        "Comments: Caffeine - yes, 1 cup a day. Smoke - no. Tobacco - no. "
        "Alcohol - yes, every day 3 cups of wine or two beers, sometimes more. "
        "Drugs - no.",
    ])))

    s.add(LabOrder(client_id=maria.id, name="Comprehensive Drug Analysis, Ur",
                   ordered_on=date(2023, 10, 28), status="Result Ready"))
    s.add(LabOrder(client_id=maria.id, name="urine drug screen",
                   ordered_on=date(2023, 10, 14), status="Needs Results"))

    # A second patient with a thinner chart, so the sheet is not uniform.
    daniel = clients[1]
    s.add(Medication(client_id=daniel.id, source=SEED, name="Escitalopram", dose="10 mg",
                     form="tablet", prescribed_on=date(2026, 6, 14)))
    s.add(Problem(client_id=daniel.id, source=SEED, description="Major depressive disorder, single episode",
                  icd10="F32.9"))
    s.add(Allergy(client_id=daniel.id, substance="Penicillin", reaction="Rash",
                  severity="Moderate"))

    # Dates of birth, so the sheet has an age column worth reading.
    maria.dob = date(1991, 3, 14)
    daniel.dob = date(1978, 11, 2)
    clients[2].dob = date(2004, 6, 28)
    clients[3].dob = date(1965, 1, 9)

    s.commit()
    s.close()


if __name__ == "__main__":
    seed()
    print("Seeded.")
