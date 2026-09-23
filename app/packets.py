"""The Adult Packet w/consent, transcribed from the real IntakeQ editor.

Question text and item labels are read off the screenshots. Where an option list
was cut off mid-scroll the visible entries are kept and the rest marked with a
trailing "..." entry, so nobody mistakes a partial list for a complete one.

Shape of a question tuple:
    (text, QuestionType, required, page, options, rows, items)

Shape of an item tuple:
    (label, ItemKind, options, width)   width 2 = full row
"""

from __future__ import annotations

from .models import ItemKind as K
from .models import QuestionType as T

MARITAL = "Single\nMarried\nDomestic Partner\nSeparated\nDivorced\nWidowed"

GENDER = ("Female\nMale\nUnknown\n"
          "Female-to-male/Transgender male/Trans man\n"
          "Male-to-female/Transgender female/Trans woman\n"
          "Genderqueer, neither exclusively male nor female\n"
          "Additional gender category / other\nChoose not to disclose")

RACE = ("American Indian or Alaska Native\nAsian\nBlack or African American\n"
        "Native Hawaiian or other Pacific Islander\nWhite\nOther")

# Full names, not postal codes. "AK / AL / AZ" reads as noise to a patient
# filling this in on a phone, and the stored value is what ends up in the
# exported spreadsheet - so it should be the readable one.
STATES = "\n".join([
    "Alabama", "Alaska", "Arizona", "Arkansas", "California", "Colorado",
    "Connecticut", "Delaware", "District of Columbia", "Florida", "Georgia",
    "Hawaii", "Idaho", "Illinois", "Indiana", "Iowa", "Kansas", "Kentucky",
    "Louisiana", "Maine", "Maryland", "Massachusetts", "Michigan", "Minnesota",
    "Mississippi", "Missouri", "Montana", "Nebraska", "Nevada", "New Hampshire",
    "New Jersey", "New Mexico", "New York", "North Carolina", "North Dakota",
    "Ohio", "Oklahoma", "Oregon", "Pennsylvania", "Rhode Island",
    "South Carolina", "South Dakota", "Tennessee", "Texas", "Utah", "Vermont",
    "Virginia", "Washington", "West Virginia", "Wisconsin", "Wyoming",
    "Other / outside the US",
])

YES_NO = "Yes\nNo"
YES_NO_NEVER = "Yes\nNo\nNever"

SEVERITY = "0\n1\n2\n3\n4"

SEVERITY_ROWS = ("Depressed mood\nLoss of interest\nAnxiety or worry\n"
                 "Sleep disturbance\nAppetite change\nIrritability\n"
                 "Concentration difficulty\nFatigue\nThoughts of self-harm")

ADULT_PACKET = [
    # 1 -------------------------------------------------------------------
    ("Please enter your information.", T.mixed_controls, True, 1, "", "", [
        ("First Name:", K.text, "", 1, True),
        ("Middle Initials:", K.text, "", 1),
        ("Last Name:", K.text, "", 1, True),
        ("Date of Birth:", K.date, "", 1, True),
        ("Marital Status:", K.list, MARITAL, 1),
        ("Gender Identity:", K.list, GENDER, 1),
        ("Ethnicity", K.text, "", 1),
        ("Race", K.list, RACE, 1),
        ("Phone Type", K.list, "Home\nMobile\nWork", 1),
        ("Phone Number", K.text, "", 1),
        ("Email:", K.text, "", 1),
        ("Secondary Phone", K.text, "", 1),
        ("Preferred contact method:", K.list,
         "Mobile Phone\nHome Phone\nWork Phone\nEmail", 1),
        ("Street Address:", K.text, "", 1),
        ("Apt./Unit:", K.text, "", 1),
        ("City:", K.text, "", 1),
        ("State:", K.list, STATES, 1),
        ("Zip Code:", K.text, "", 1),
    ]),

    # 2 -------------------------------------------------------------------
    ("Emergency Contact", T.mixed_controls, False, 2, "", "", [
        ("Relationship to Contact", K.text, "", 1),
        ("First Name", K.text, "", 1),
        ("Middle Name", K.text, "", 1),
        ("Last Name", K.text, "", 1),
        ("Phone Type", K.list, "Home\nCell\nWork", 1),
        ("Phone Number", K.text, "", 1),
        ("Address Line 1", K.text, "", 1),
        ("Address Line 2", K.text, "", 1),
    ]),

    # 3 -------------------------------------------------------------------
    ("Primary Insurance", T.mixed_controls, False, 3, "", "", [
        ("Insurance Company", K.text, "", 1),
        ("Policy / Member Number", K.text, "", 1),
        ("Group Number", K.text, "", 1),
        ("Policy Holder Name", K.text, "", 1),
        ("Policy Holder Date of Birth", K.date, "", 1),
        ("Relationship to Policy Holder", K.list,
         "Self\nSpouse\nChild\nOther", 1),
    ]),

    # 4 -------------------------------------------------------------------
    ("Please upload front and back pictures of your insurance card",
     T.file_upload, False, 3, "", "", []),

    # 5 -------------------------------------------------------------------
    ("Please enter your pharmacy information below", T.mixed_controls, False, 4,
     "", "", [
        ("Pharmacy Name", K.text, "", 1),
        ("Pharmacy Phone", K.text, "", 1),
        ("Street Address", K.text, "", 1),
        ("City", K.text, "", 1),
        ("State", K.list, STATES, 1),
        ("Zip Code", K.text, "", 1),
     ]),

    # 6 -------------------------------------------------------------------
    ("Please state your presenting problems", T.long_text, True, 5, "", "", []),

    # 7 -------------------------------------------------------------------
    ("Please take a few minutes to complete the following. Check the number that "
     "applies to you. The numbers range from 0 meaning not present through 4 "
     "meaning severe problem",
     T.matrix, True, 6, SEVERITY, SEVERITY_ROWS, []),

    # 8 -------------------------------------------------------------------
    ("Medical History Please fill the following in detail using the other option "
     "for the conditions not listed. Please type N/A where needed.",
     T.mixed_controls, False, 7, "", "", [
        ("4. Medical History (required)", K.list,
         "Head trauma\nAnemia\nArthritis/GOUT\nsleep apnea\nSkin conditions\nOther", 1),
        ("Heart conditions", K.list,
         "High blood pressure\nArrhythmias\nCongestive heart failure\n"
         "Heart attack\nOther", 1),
        ("Gastrointestinal", K.list,
         "GERD\nUlcers\nHeart burn\nGallbladder disease\nIBS\nOther", 1),
        ("Endocrine", K.list,
         "Thyroid problem\nHigh cholesterol\nDiabetes type 1/type 2\nLupus\nOther", 1),
        ("Neurological", K.list, "Seizures\nStroke\nMigraines\nOther", 1),
        ("Respiratory", K.list, "Asthma\nBronchitis\nPneumonia\nCOPD\nOther", 1),
        ("Genitourinary", K.list, "Kidney stones\nUTI\nOther", 1),
        ("Other Medical problems", K.text, "", 1),
        ("6. Surgeries:", K.text, "", 2),
        ("7. Past Psych History?", K.list,
         "Medication management\nTherapy/Counselling\nIntensive outpatient program\n"
         "Drug detox/rehab\nECT\nTMS\nOther", 2),
        ("If any psychiatric medication management, please specify names of all past "
         "medications tried", K.text, "", 2),
        ("Please specify, if any, previous psychiatrist", K.text, "", 1),
        ("Any Psychiatric hospitalisations?", K.list, YES_NO, 1),
     ]),

    # 9 -------------------------------------------------------------------
    ("Which medications (psychotropic or not) are you currently taking?",
     T.long_text, False, 8, "", "", []),

    # 10 ------------------------------------------------------------------
    ("Do you use any of the following? Answer specific frequency, quantity, form "
     "of use, start age / for how long, if sober for how long and relapse reasons "
     "if any, etc.",
     T.mixed_controls, False, 9, "", "", [
        ("Caffeine", K.list, YES_NO_NEVER, 1),
        ("Caffeine - details", K.text, "", 1),
        ("Alcohol", K.list, YES_NO_NEVER, 1),
        ("Alcohol - details", K.text, "", 1),
        ("Drugs", K.list, YES_NO_NEVER, 1),
        ("Drugs - details", K.text, "", 1),
        ("Nicotine", K.list, YES_NO_NEVER, 1),
        ("Nicotine - details", K.text, "", 1),
     ]),

    # 11 ------------------------------------------------------------------
    ("CAGE-AID", T.mixed_controls, False, 10, "", "", [
        ("Have you ever felt you ought to cut down on your drinking or drug use?",
         K.list, YES_NO, 2),
        ("Have people annoyed you by criticizing your drinking or drug use?",
         K.list, YES_NO, 2),
        ("Have you ever felt bad or guilty about your drinking or drug use?",
         K.list, YES_NO, 2),
        ("Have you ever had a drink or used drugs first thing in the morning to "
         "steady your nerves or to get rid of a hangover?", K.list, YES_NO, 2),
        ("Are you a current smoker?", K.list, YES_NO, 2),
     ]),
]
