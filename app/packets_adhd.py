"""ADHD in Adults Questionnaire, transcribed from the live IntakeQ form.

Questions 1-14, read off screenshots a2-a10. Option lists are reproduced
verbatim, including the wording that reads oddly ("I'd like to talk about it, if
you can call me to discuss" sits among diagnosis options) - this is a screening
form in use, and quietly tidying its wording would make the export disagree with
the paper.

Three of the questions are tables in IntakeQ. There is no table control here, so
each cell becomes a labelled field in a mixed-controls block:

    Q10 habits        7 substances x (How much?, How often?)  -> 14 fields
    Q11 clinicians    2 rows x 4 columns                      ->  8 fields
    Q12 medications   2 rows x 3 columns                      ->  6 fields
    Q13 supplements   2 rows x 3 columns                      ->  6 fields

The label carries the row and column ("Smoking - How often?"), so the answer
lands in its own cell in the export rather than inside a joined string. That is
also why the answers are keyed by label: adding a row later must not shift what
every previous answer means.

Shape of a question tuple:
    (text, QuestionType, required, page, options, rows, items)
Shape of an item tuple:
    (label, ItemKind, options, width)   width 2 = full row
"""

from __future__ import annotations

from .models import ItemKind as K
from .models import QuestionType as T
from .packets import STATES

SEX = "Female\nMale\nIntersex"

# Q2 - deliberately not a yes/no. The form offers four ways of being unsure,
# because at screening "I am not sure" is a useful answer, not a failure to answer.
ADHD_DIAGNOSIS = "\n".join([
    "Yes from a mental health professional",
    "Yes but I'm not sure where I got it or who gave it to me",
    "No, but I think I do likely qualify and would like to get an evaluation",
    "I am not sure",
    "I'd like to talk about it, if you can call me to discuss",
])

TRIAL_INTEREST = "\n".join([
    "Yes I would like to participate",
    "I am not sure yet, but would like to learn more",
    "I do not want to particpate",
    "I do not know if I have a diagnosis of ADHD and am interested in being "
    "evaluated and participating if I am diagnosed",
])

PSYCH_DIAGNOSES = "\n".join([
    "None",
    "Major Depressive Disorder",
    "Generalized Anxiety Disorder",
    "Bipolar Disorder 1 or 2",
    "Schizophrenia or Schizoaffective",
    "PTSD",
    "Anorexia Nervosa",
    "Panic Disorder with or w/o Agoraphobia",
    "Social Anxiety Disorder",
    "OCD",
    "Binge Eating Disorder",
])

PSYCH_MEDS = "\n".join([
    "Medication(s) for Depression",
    "Medication(s) for Anxiety",
    "Medication(s) for Bipolar Disorder",
    "Medication(s) for Schizophrenia/Schizoaffective",
    "Psychotherapy for Mental Health symptoms other than Binge Eating Disorder",
])

ACCESS_SUPPORT = "\n".join([
    "Paid for your time participating",
    "Paid uber rides to/from visits",
    "Lodging / Hotel Room night before the visit",
    "Live over 50 miles away from San Juan Capistrano",
    "Drive to San Juan Capistrano would take longer than 50 minutes",
])

# Q14. These are the labels app/matching.py maps to the hospital archive's
# vocabulary - change one here and the Match column stops seeing it.
MEDICAL_CONDITIONS = "\n".join([
    "Diabetes", "Epilepsy", "Heart condition",
    "Cancer", "Bleeding disorder", "Thyroid condition",
    "Irritable bowel", "Ulcerative colitis", "Liver disease",
    "Asthma", "HIV / AIDS", "Osteoporosis",
    "Rheumatoid arthritis", "Kidney disease", "Cardiovascular disease",
    "Other(s)",
])

HABITS = ["Smoking", "Alcohol", "Recreational drugs", "Tea", "Coffee",
          "Sleeping pills", "Laxatives / Purgatives"]

#  The practice's own name, not a hard-coded one. A consent form names the
#  organisation the patient is granting permission to, so it has to follow the
#  practice branding rather than sit frozen in the source - a consent naming
#  the wrong organisation is not a cosmetic error.
def consent_text() -> str:
    from .config import load
    return (f"By signing here I give {load()['name']} "
            "Permission to obtain medical records, private health information "
            "from a physician, clinician, therapist, or other health "
            "establishment.")


CONSENT_TEXT = consent_text()

THANK_YOU = (
    "Thank you for taking the time to fill out this HIPAA Compliant "
    "Questionnaire. The research department will be reaching out. Please save "
    "the following number in your caller ID / Contacts / Phone Book so that you "
    "know we are calling.\n\n"
    "909-955-5865\n\n"
    "You can also visit our Virtual Front Desk by going to: "
    "https://meridianbh.doxy.me/research\n\n"
    "You can chat or have a video call with our research department here."
)


def _grid(rows: list[str], columns: list[str]) -> list[tuple]:
    """A table flattened to 'Row - Column' text fields, reading across."""
    return [(f"{row} - {col}", K.text, "", 1) for row in rows for col in columns]


# Columns added beyond the IntakeQ original, at the practice's request. A
# screening decision turns on how long somebody has been on a drug - a trial's
# washout rule is counted in weeks - and "How long?" invites "a while", which
# cannot be counted. Asking when it started produces a date the app can subtract.
SINCE = "Taking it since (e.g. Mar 2024)"


ADHD_PACKET = [
    # 1 --------------------------------------------------------------------
    ("Please enter your information.", T.mixed_controls, True, 1, "", "", [
        ("First Name:", K.text, "", 1, True),
        ("Last Name:", K.text, "", 1, True),
        ("Date of Birth:", K.date, "", 1, True),
        ("Sex:", K.list, SEX, 1),
        ("City:", K.text, "", 1),
        ("State:", K.list, STATES, 1),
        ("Zip Code:", K.text, "", 1),
        ("Mobile Phone:", K.text, "", 1),
        ("Home Phone:", K.text, "", 1),
        ("Email:", K.text, "", 1),
    ]),

    # 2 --------------------------------------------------------------------
    ("Do you have a diagnosis of ADHD (Attention Deficit Hyperactivity Disorder)?",
     T.checkbox, True, 2, ADHD_DIAGNOSIS, "", []),

    # 3 --------------------------------------------------------------------
    ("If you have a diagnosis of ADHD and you remember where you may have gotten "
     "it, please sign below to give us permission to obtain medical records from "
     "a physician, clinician or therapist.",
     T.signature, False, 3, CONSENT_TEXT, "", []),

    # 4 --------------------------------------------------------------------
    ("ADHD is a serious disorder that can affect people from childhood onwards. "
     "Treatment options are still limited and participating in a clinical trial "
     "for a new compound may lead to the development of a new FDA approved "
     "treatment option. Are you interested in participating in a clinical trial "
     "that includes possibly having the placebo (sugar pill) or the "
     "investigational medication (3 different dosing types.)",
     T.checkbox, True, 4, TRIAL_INTEREST, "", []),

    # 5 --------------------------------------------------------------------
    ("What kind of other psychiatric (or mental health) diagnoses do you have?",
     T.checkbox, False, 5, PSYCH_DIAGNOSES, "", []),

    # 6 --------------------------------------------------------------------
    ("Have you taken Vyvanse in the past?", T.radio, False, 5, "Yes\nNo\nMaybe",
     "", []),

    # 7 --------------------------------------------------------------------
    ("Are you currently on medication for other psychiatric (mental health) "
     "diagnoses or symptoms?", T.checkbox, False, 5, PSYCH_MEDS, "", []),

    # 8 --------------------------------------------------------------------
    ("Are you interested in getting paid for clinical trials, paid uber rides, "
     "lodging, or other access support?", T.checkbox, False, 6, ACCESS_SUPPORT,
     "", []),

    # 9 --------------------------------------------------------------------
    (THANK_YOU, T.rich_text, False, 7, "", "", []),

    # 10 -------------------------------------------------------------------
    ("Your habits:", T.mixed_controls, False, 8, "", "",
     _grid(HABITS, ["How much?", "How often?", "Since when?"])),

    # 11 -------------------------------------------------------------------
    ("Are you currently under the care of a family physician or any other health "
     "professional? If yes, please indicate:", T.mixed_controls, False, 9, "", "",
     _grid(["1", "2"], ["Health professional's name",
                        "Health professional's contact",
                        "Condition", "Treatment"])),

    # 12 -------------------------------------------------------------------
    ("Please list any prescribed medications you take:", T.mixed_controls, False,
     10, "", "", _grid(["1", "2", "3", "4"], ["Name", "Dosage", SINCE])),

    # 13 -------------------------------------------------------------------
    ("Please list any supplements you currently take or have taken in the recent "
     "past:", T.mixed_controls, False, 10, "", "",
     _grid(["1", "2", "3"], ["Name of supplement", "Dosage", SINCE])),

    # 14 -------------------------------------------------------------------
    ("Have you been diagnosed with any of the following conditions?",
     T.checkbox, False, 11, MEDICAL_CONDITIONS, "", []),
]
