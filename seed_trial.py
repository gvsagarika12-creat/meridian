"""Create the adult ADHD trial and a starter protocol.

    python seed_trial.py

Every criterion here is checkable against a question the ADHD in Adults
Questionnaire already asks, or against the hospital archive. That is the only
reason this particular set exists - it is NOT the protocol. A real protocol has
inclusion and exclusion criteria that this form does not ask about at all
(weight, ECG, prior stimulant washout, pregnancy status), and those belong in
the trial as `manual` criteria until the form collects them.

Replace this the moment you have the registered protocol. The NCT number given
was NCT0691112, which is seven digits; registry IDs are eight, and no study
matches it, so the trial is created without one.
"""

from __future__ import annotations

from app.models import SessionLocal, init_db
from app.trials import Criterion, CriterionKind as K, Rule as R, Trial

NAME = "Adult ADHD investigational compound"

Q_DIAGNOSIS = "diagnosis of ADHD"
Q_INTEREST = "interested in participating in a clinical trial"
Q_PSYCH = "other psychiatric (or mental health) diagnoses"
Q_VYVANSE = "Vyvanse"

CRITERIA = [
    # (kind, rule, target, min, max, the protocol's own wording)
    (K.inclusion, R.age_between, "", 18, 65,
     "Adults aged 18 to 65 at screening"),

    (K.inclusion, R.answer_includes,
     f"{Q_DIAGNOSIS}::Yes from a mental health professional", None, None,
     "Has an ADHD diagnosis made by a mental health professional"),

    (K.inclusion, R.answer_includes,
     f"{Q_INTEREST}::Yes I would like to participate", None, None,
     "Has said they want to take part"),

    (K.inclusion, R.answered, Q_PSYCH, None, None,
     "Psychiatric history has been given"),

    # Exclusions. Each one EXCLUDES the patient when it is true.
    (K.exclusion, R.answer_includes,
     f"{Q_PSYCH}::Bipolar Disorder 1 or 2", None, None,
     "Bipolar disorder I or II"),

    (K.exclusion, R.answer_includes,
     f"{Q_PSYCH}::Schizophrenia or Schizoaffective", None, None,
     "Schizophrenia or schizoaffective disorder"),

    (K.exclusion, R.answer_includes, f"{Q_VYVANSE}::Yes", None, None,
     "Previously treated with Vyvanse"),

    (K.exclusion, R.reported_condition, "Epilepsy", None, None,
     "History of seizures"),

    (K.exclusion, R.archive_condition, "Heart Attack", None, None,
     "Myocardial infarction in the hospital record"),

    (K.exclusion, R.archive_condition, "Heart Disease", None, None,
     "Cardiovascular disease in the hospital record"),

    # What the form cannot answer. Present so the gap is visible on screen
    # rather than absent and mistaken for a clean pass.
    (K.inclusion, R.manual, "", None, None,
     "Weight and BMI within protocol limits - not collected on this form"),
    (K.inclusion, R.manual, "", None, None,
     "Screening ECG reviewed - not collected on this form"),
    (K.exclusion, R.manual, "", None, None,
     "Pregnant or breastfeeding - not collected on this form"),
]


def main() -> None:
    init_db()
    db = SessionLocal()

    trial = db.query(Trial).filter_by(name=NAME).first()
    if trial:
        for c in list(trial.criteria):
            db.delete(c)
        db.flush()
        print(f"rewriting the criteria on trial {trial.id}")
    else:
        trial = Trial(
            name=NAME,
            nct_id="",
            sponsor="",
            phase="",
            condition="Attention deficit hyperactivity disorder, adult",
            site="San Juan Capistrano",
            status="Recruiting",
            notes=("Starter protocol. Every criterion below is one the ADHD in "
                   "Adults Questionnaire can actually answer; the real protocol "
                   "has more. The NCT number supplied (NCT0691112) is seven "
                   "digits and matches no registered study - add the correct "
                   "eight-digit ID and replace these criteria with the "
                   "registered ones before screening anybody for real."))
        db.add(trial)
        db.flush()
        print(f"created trial {trial.id}")

    for pos, (kind, rule, target, lo, hi, text) in enumerate(CRITERIA):
        db.add(Criterion(trial_id=trial.id, kind=kind, rule=rule, target=target,
                         min_value=lo, max_value=hi, text=text, position=pos))
    db.commit()

    trial = db.query(Trial).filter_by(name=NAME).one()
    print(f"\n{trial.name}")
    print(f"  {len(trial.inclusions)} inclusion, {len(trial.exclusions)} exclusion")
    for c in trial.criteria:
        mark = "+" if c.kind == K.inclusion else "-"
        hand = "  (by hand)" if c.rule == R.manual else ""
        print(f"   {mark} {c.text}{hand}")


if __name__ == "__main__":
    main()
