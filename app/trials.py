"""Trials, their protocols, and whether a patient looks suitable.

A protocol is a list of criteria, and each criterion is a rule the app can check
against what it already holds: the patient's age, what they answered on a form,
what the hospital archive says, what the chart records. Criteria are rows, not
code, because the people who know the protocol are coordinators, not developers,
and a protocol that needs a deployment to correct is a protocol nobody corrects.

Three rules run through this file.

**Unknown is not met.** A criterion the app cannot check - because the patient
never answered that question, or has no hospital number - is reported as
`UNKNOWN`, never as satisfied. A screening list that quietly counts silence as a
yes sends people to a site visit they were never eligible for.

**The app screens, it does not decide.** The verdict is "looks eligible" or
"needs review", never "enrolled". Eligibility is a clinician's judgement made
against the full protocol document; this narrows a list of six hundred patients
to the dozen worth reading properly.

**Every verdict shows its working.** A result is a list of criteria with what
each one checked and what it found, so a coordinator can disagree with it.
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass, field
from datetime import date, datetime

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .models import Base


class CriterionKind(str, enum.Enum):
    inclusion = "inclusion"      # must be true
    exclusion = "exclusion"      # must be false


class Rule(str, enum.Enum):
    """What a criterion actually checks. Deliberately few.

    Every rule here answers a question from data the app already holds. A rule
    that needs information nobody collects is a rule that always returns
    UNKNOWN, which helps nobody - so the list grows only when the form does.
    """

    age_between = "age_between"                  # min_value .. max_value
    sex_is = "sex_is"                            # target: Female | Male
    answered = "answered"                        # target: question fragment
    answer_includes = "answer_includes"          # question fragment :: option
    reported_condition = "reported_condition"    # target: a Q14 condition label
    archive_condition = "archive_condition"      # target: an archive Condition
    has_hospital_record = "has_hospital_record"  # linked and found
    taking_medication = "taking_medication"      # target: drug name (chart)
    manual = "manual"                            # a human must read the notes


RULE_LABEL = {
    Rule.age_between: "Age is between",
    Rule.sex_is: "Sex is",
    Rule.answered: "Answered the question",
    Rule.answer_includes: "Answer includes",
    Rule.reported_condition: "Patient reported the condition",
    Rule.archive_condition: "Hospital archive records the condition",
    Rule.has_hospital_record: "Is linked to a hospital record",
    Rule.taking_medication: "Chart lists the medication",
    Rule.manual: "Checked by hand",
}


# What each check needs before it can mean anything. A criterion saved without
# its target is not a strict criterion - it is one that can never match, and it
# reports "unknown" forever while looking like a real line of the protocol.
#
# The message is what a person is shown, so it says what to type, not what is
# missing. "Target is required" leaves somebody staring at a box.
NEEDS_TARGET = {
    Rule.answered:
        'Which question? Type part of its text, for example "Vyvanse".',
    Rule.answer_includes:
        'Which answer? Type part of the question, then "::", then the exact '
        'option - for example "Vyvanse::Yes".',
    Rule.reported_condition:
        'Which condition? Use the wording from question 14, for example "Epilepsy".',
    Rule.archive_condition:
        "Which condition? Use the hospital archive's own wording, for example "
        '"Heart Attack".',
    Rule.taking_medication:
        'Which medication? A drug name, for example "Lisinopril".',
    Rule.sex_is:
        'Which one? "Female" or "Male".',
}


# "Vyvanse :: Yes", "Vyvanse : : Yes", "Vyvanse::Yes" all mean the same thing to
# the person typing them, so they mean the same thing here. Two colons with any
# spacing around or between them is the separator; a single colon is left alone,
# because question text can legitimately contain one.
_SEPARATOR = re.compile(r"\s*:\s*:\s*")


def tidy_target(target: str) -> str:
    """Normalise what somebody typed into what the matcher expects."""
    return _SEPARATOR.sub("::", (target or "").strip())


def problems_with(rule: "Rule", target: str, low, high) -> list[str]:
    """Everything wrong with a criterion, so all of it can be shown at once.

    Returning a list rather than raising on the first fault matters: a form that
    reports one problem per attempt makes somebody submit three times to learn
    three things.
    """
    faults = []
    target = tidy_target(target)

    if rule in NEEDS_TARGET and not target:
        faults.append(NEEDS_TARGET[rule])

    if rule == Rule.answer_includes and target and "::" not in target:
        faults.append('An "Answer includes" target needs "::" between the '
                      'question and the option, for example "Vyvanse::Yes". '
                      f'You typed "{target}".')

    if rule == Rule.age_between:
        if low is None and high is None:
            faults.append("An age check needs a Min, a Max, or both.")
        elif low is not None and high is not None and low > high:
            faults.append(f"Min ({low}) is above Max ({high}).")

    if rule not in NEEDS_TARGET and rule != Rule.age_between and target:
        faults.append(f'"{RULE_LABEL.get(rule, rule.value)}" takes no target, '
                      f'so "{target}" would be ignored. Clear it, or choose a '
                      f'check that uses it.')
    return faults


class Trial(Base):
    """One study the practice is screening for."""

    __tablename__ = "trials"

    id: Mapped[int] = mapped_column(primary_key=True)
    nct_id: Mapped[str] = mapped_column(String(32), default="")
    name: Mapped[str] = mapped_column(String(255))
    sponsor: Mapped[str] = mapped_column(String(255), default="")
    phase: Mapped[str] = mapped_column(String(32), default="")
    condition: Mapped[str] = mapped_column(String(128), default="")
    site: Mapped[str] = mapped_column(String(255), default="")
    status: Mapped[str] = mapped_column(String(32), default="Recruiting")
    notes: Mapped[str] = mapped_column(Text, default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    criteria: Mapped[list["Criterion"]] = relationship(
        back_populates="trial", cascade="all, delete-orphan",
        order_by="Criterion.position")

    @property
    def inclusions(self) -> list["Criterion"]:
        return [c for c in self.criteria if c.kind == CriterionKind.inclusion]

    @property
    def exclusions(self) -> list["Criterion"]:
        return [c for c in self.criteria if c.kind == CriterionKind.exclusion]

    @property
    def registry_url(self) -> str:
        return f"https://clinicaltrials.gov/study/{self.nct_id}" if self.nct_id else ""


class Criterion(Base):
    """One checkable line of a protocol."""

    __tablename__ = "trial_criteria"

    id: Mapped[int] = mapped_column(primary_key=True)
    trial_id: Mapped[int] = mapped_column(ForeignKey("trials.id"))
    kind: Mapped[CriterionKind] = mapped_column(Enum(CriterionKind))
    rule: Mapped[Rule] = mapped_column(Enum(Rule))

    # What the rule compares against. `target` holds a question fragment, an
    # option, a condition or a drug name depending on the rule; the two numbers
    # are only used by age_between.
    target: Mapped[str] = mapped_column(String(255), default="")
    min_value: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_value: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # The protocol's own wording, so a coordinator reads the criterion as the
    # document states it rather than as the rule engine expresses it.
    text: Mapped[str] = mapped_column(Text, default="")
    position: Mapped[int] = mapped_column(Integer, default=0)

    trial: Mapped[Trial] = relationship(back_populates="criteria")

    @property
    def summary(self) -> str:
        if self.text:
            return self.text
        label = RULE_LABEL.get(self.rule, self.rule.value)
        if self.rule == Rule.age_between:
            return f"{label} {self.min_value or 0} and {self.max_value or 120}"
        return f"{label} {self.target}".strip()


# --- evaluation -----------------------------------------------------------

MET, FAILED, UNKNOWN = "met", "failed", "unknown"


@dataclass
class Check:
    criterion: Criterion
    result: str            # MET | FAILED | UNKNOWN
    found: str             # what the app actually saw

    @property
    def blocks(self) -> bool:
        """Does this stop the patient, on its own?"""
        if self.criterion.kind == CriterionKind.inclusion:
            return self.result == FAILED
        return self.result == MET          # an exclusion that is true excludes


@dataclass
class Screening:
    trial: Trial
    checks: list[Check] = field(default_factory=list)

    @property
    def blocking(self) -> list[Check]:
        return [c for c in self.checks if c.blocks]

    @property
    def unknown(self) -> list[Check]:
        return [c for c in self.checks if c.result == UNKNOWN]

    @property
    def verdict(self) -> str:
        if not self.checks:
            return "No criteria"
        if self.blocking:
            return "Not eligible"
        if self.unknown:
            return "Needs review"
        return "Looks eligible"

    def tally(self, kind) -> tuple[int, int]:
        """(met, total) for one kind of criterion.

        Split by kind because the two answer different questions. "3 of 4
        inclusion" means the patient nearly qualifies; "4 of 4 exclusion" means
        four separate things rule them out. One combined number hides which of
        those you are looking at.
        """
        of_kind = [c for c in self.checks if c.criterion.kind == kind]
        met = [c for c in of_kind if c.result != UNKNOWN and not c.blocks]
        return len(met), len(of_kind)

    @property
    def badge(self) -> str:
        """The short label a card shows: meets / exclusion / verification."""
        if not self.checks:
            return "no criteria"
        if self.blocking:
            return "potential exclusion"
        if self.unknown:
            return "needs verification"
        return "meets"

    @property
    def score_text(self) -> str:
        decided = [c for c in self.checks if c.result != UNKNOWN and not c.blocks]
        return f"{len(decided)} of {len(self.checks)} criteria met"

    @property
    def summary(self) -> str:
        if not self.checks:
            return "This trial has no criteria yet."
        if self.blocking:
            return "; ".join(f"{c.criterion.summary} - {c.found}"
                             for c in self.blocking)
        if self.unknown:
            return ("nothing rules them out, but "
                    + "; ".join(f"{c.criterion.summary} - {c.found}"
                                for c in self.unknown))
        return "every criterion checked and met"


def _age(client) -> int | None:
    if not client.dob:
        return None
    today = date.today()
    return (today.year - client.dob.year -
            ((today.month, today.day) < (client.dob.month, client.dob.day)))


def _answers(client):
    """Every answer on the patient's most recently submitted form."""
    from .matching import latest_screening
    sub = latest_screening(client)
    if sub is None:
        # No screening form - fall back to any submission with answers, so a
        # patient who filled a different questionnaire is not treated as silent.
        done = [s for s in client.submissions_list if s.answers]
        if not done:
            return []
        sub = max(done, key=lambda s: s.submitted_at or datetime.min)
    return list(sub.answers or [])


def _find_answer(client, fragment: str):
    needle = (fragment or "").strip().lower()
    for answer in _answers(client):
        if needle and needle in (answer.question.text or "").lower():
            return answer
    return None


def evaluate(client, trial: Trial, archive) -> Screening:
    """Check one patient against one protocol."""
    screening = Screening(trial=trial)
    archive_conditions = {r.condition for r in archive if r.condition}

    for criterion in trial.criteria:
        screening.checks.append(_check(client, criterion, archive_conditions))
    return screening


def _check(client, c: Criterion, archive_conditions: set[str]) -> Check:
    if c.rule == Rule.age_between:
        age = _age(client)
        if age is None:
            return Check(c, UNKNOWN, "no date of birth on file")
        low, high = c.min_value or 0, c.max_value or 200
        inside = low <= age <= high
        return Check(c, MET if inside else FAILED, f"aged {age}")

    if c.rule == Rule.sex_is:
        from .matching import _recorded_sex
        sex = _recorded_sex(client)
        if not sex:
            return Check(c, UNKNOWN, "sex not answered on any form")
        return Check(c, MET if sex.lower() == c.target.strip().lower() else FAILED,
                     f"form says {sex}")

    if c.rule == Rule.answered:
        answer = _find_answer(client, c.target)
        if answer is None or not answer.values:
            return Check(c, UNKNOWN, "question not answered")
        return Check(c, MET, answer.display[:80])

    if c.rule == Rule.answer_includes:
        question, _, option = c.target.partition("::")
        answer = _find_answer(client, question)
        if answer is None or not answer.values:
            return Check(c, UNKNOWN, "question not answered")
        wanted = option.strip().lower()
        hit = any(wanted == (v or "").strip().lower() for v in answer.values)
        if not hit:                      # allow a partial match on long options
            hit = any(wanted and wanted in (v or "").lower() for v in answer.values)
        return Check(c, MET if hit else FAILED, answer.display[:80])

    if c.rule == Rule.reported_condition:
        from .matching import _reported_conditions
        reported = _reported_conditions(client)
        if not reported:
            return Check(c, UNKNOWN, "no conditions question answered")
        hit = c.target.strip().lower() in {r.lower() for r in reported}
        return Check(c, MET if hit else FAILED,
                     "reported: " + (", ".join(sorted(reported)) or "none"))

    if c.rule == Rule.archive_condition:
        if not client.hospital_id:
            return Check(c, UNKNOWN, "not linked to a hospital record")
        if not archive_conditions:
            return Check(c, UNKNOWN, "hospital number has no archive rows")
        hit = c.target.strip().lower() in {a.lower() for a in archive_conditions}
        return Check(c, MET if hit else FAILED,
                     "archive: " + ", ".join(sorted(archive_conditions)))

    if c.rule == Rule.has_hospital_record:
        if not client.hospital_id:
            return Check(c, FAILED, "no hospital ID on file")
        return Check(c, MET if archive_conditions else FAILED,
                     f"number {client.hospital_id}"
                     + ("" if archive_conditions else " is not in the archive"))

    if c.rule == Rule.taking_medication:
        drugs = [m.name for m in client.active_medications]
        if not drugs:
            return Check(c, UNKNOWN, "no medications on the chart")
        hit = any(c.target.strip().lower() in (d or "").lower() for d in drugs)
        return Check(c, MET if hit else FAILED, "chart: " + ", ".join(drugs))

    # Rule.manual, and anything added to the enum but not handled here.
    return Check(c, UNKNOWN, "must be checked by hand")
