"""The Match column: what the patient told us, against what the hospital holds.

This is the last column of the export and the reason the export exists. A
coordinator screening for a trial needs to know, per patient, whether the
self-reported history agrees with the record - because the disagreements are
where the screening effort goes.

Three rules shaped this file:

**The mapping is explicit, not fuzzy.** A patient ticks "Heart condition"; the
archive says "Heart Attack". Those are the same fact in two vocabularies, and
only a table someone can read and argue with should decide that. String
similarity would quietly pair "Prostate Cancer" with "Cancer" and, just as
quietly, "Kidney disease" with "Kidney Stones" - which are not the same thing.

**Silence is not disagreement.** A condition the patient did not tick is not a
denial; most intake forms only list a handful of conditions. So an archive entry
with no corresponding tick is reported as *only in records*, never as a conflict.

**What cannot be checked is said so.** The archive holds no psychiatric
diagnoses and no medication list, so the ADHD questionnaire's psychiatric and
medication answers have nothing to compare against. Those are reported as
`no basis for comparison` rather than being silently counted as agreement.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

from .models import SubmissionStatus

# --- the vocabulary bridge ------------------------------------------------
#
# Left: what the questionnaire offers the patient to tick (question 14).
# Right: the Condition values the hospital archive actually uses.
#
# Only pairs a clinician would accept. Where the archive has nothing that means
# the same thing, the entry maps to an empty set - the patient can report it,
# and the honest answer is that the archive cannot confirm or deny it.
CONDITION_MAP: dict[str, set[str]] = {
    "Diabetes":               {"Diabetes"},
    "Cancer":                 {"Cancer", "Prostate Cancer"},
    "Heart condition":        {"Heart Attack", "Heart Disease"},
    "Cardiovascular disease": {"Heart Disease", "Heart Attack", "Stroke", "Hypertension"},
    "Asthma":                 {"Respiratory Infection"},
    "Kidney disease":         {"Kidney Stones"},
    "Rheumatoid arthritis":   {"Osteoarthritis"},

    # Present on the form, absent from this archive. Listed deliberately so the
    # gap is visible here rather than looking like an oversight.
    "Epilepsy":          set(),
    "Bleeding disorder": set(),
    "Ulcerative colitis": set(),
    "Irritable bowel":   set(),
    "HIV / AIDS":        set(),
    "Liver disease":     set(),
    "Thyroid condition": set(),
    "Osteoporosis":      set(),
    "Other(s)":          set(),
}

# The reverse direction, built once: archive condition -> the form labels that
# would cover it. Used to decide whether an archive entry was reportable at all.
_REVERSE: dict[str, set[str]] = {}
for _label, _conditions in CONDITION_MAP.items():
    for _c in _conditions:
        _REVERSE.setdefault(_c, set()).add(_label)


# Questions whose answers the archive has no column for. Matched on a fragment
# of the question text, the same way records.py finds its columns.
UNCOMPARABLE = (
    "other psychiatric",           # Q5  - archive holds no psychiatric diagnoses
    "prescribed medications",      # Q12 - archive holds procedures, not drugs
    "supplements",                 # Q13
)


@dataclass
class MatchResult:
    """One patient's comparison, ready to render or write to a cell."""

    linked: bool = False                      # has a hospital number at all
    hospital_id: int | None = None
    visits: int = 0
    confirmed: list[str] = field(default_factory=list)      # both sides agree
    patient_only: list[str] = field(default_factory=list)   # reported, not in archive
    records_only: list[str] = field(default_factory=list)   # in archive, not reported
    unverifiable: list[str] = field(default_factory=list)   # archive has no such column

    @property
    def comparable(self) -> int:
        """How many facts could actually be checked against the archive.

        Only conditions that exist in both vocabularies count. A question the
        archive has no column for is not a failed comparison - it is not a
        comparison at all, and including it would make every score look worse
        the more the form asks.
        """
        return len(self.confirmed) + len(self.patient_only) + len(self.records_only)

    @property
    def score(self) -> int | None:
        """Percentage of comparable facts the two sides agree on.

        None, never zero, when nothing could be compared. A score of 0% means
        "everything checkable disagreed"; no score means "nothing was
        checkable", and a coordinator must be able to tell those apart.
        """
        total = self.comparable
        return round(len(self.confirmed) / total * 100) if total else None

    @property
    def score_text(self) -> str:
        """'50% (1 of 2 agree)' - the number with its denominator attached.

        The denominator matters more than the percentage here. With two
        comparable conditions the only possible scores are 0, 50 and 100, and a
        bare '50%' invites a confidence the sample size does not support.
        """
        if self.score is None:
            return ""
        n, total = len(self.confirmed), self.comparable
        return f"{self.score}% ({n} of {total} agree)"

    @property
    def verdict(self) -> str:
        """The short word that goes in the Match cell."""
        if not self.linked:
            return "No hospital ID"
        if not self.visits:
            return "ID not found in archive"
        if not (self.confirmed or self.patient_only or self.records_only):
            return "Nothing to compare"
        if self.patient_only or self.records_only:
            return "Partial match"
        return "Match"

    @property
    def summary(self) -> str:
        """The cell's detail: what agreed, what did not, in that order."""
        if not self.linked:
            return "No hospital ID on file - link the patient to compare."
        if not self.visits:
            return f"Hospital ID {self.hospital_id} has no record in the archive."

        bits = []
        if self.confirmed:
            bits.append("confirmed: " + ", ".join(sorted(self.confirmed)))
        if self.patient_only:
            bits.append("patient reported only: " + ", ".join(sorted(self.patient_only)))
        if self.records_only:
            bits.append("in records only: " + ", ".join(sorted(self.records_only)))
        if self.unverifiable:
            bits.append("no basis for comparison: " + ", ".join(sorted(self.unverifiable)))
        return "; ".join(bits) or "No overlapping fields."


def latest_screening(client):
    """The most recently submitted form that asked about medical conditions.

    A re-screen supersedes. Reading every form the patient ever sent would mean a
    condition ticked by mistake in March still counts in September, however
    carefully they corrected it - and a screening decision has to rest on what
    the patient says now, not on everything they have ever said.

    The earlier forms are not lost; they stay readable on the Submissions screen,
    which is where a disagreement between two of them belongs.
    """
    candidates = [
        s for s in client.submissions_list
        if s.status in (SubmissionStatus.submitted, SubmissionStatus.reviewed)
        and any("diagnosed with any of the following" in (a.question.text or "").lower()
                for a in (s.answers or []))
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda s: (s.submitted_at or datetime.min, s.id))


def _reported_conditions(client) -> set[str]:
    """The conditions ticked on the patient's most recent screening form."""
    sub = latest_screening(client)
    if sub is None:
        return set()
    ticked: set[str] = set()
    for answer in sub.answers or []:
        text = (answer.question.text or "").lower()
        if "diagnosed with any of the following" not in text:
            continue
        ticked.update(v.strip() for v in answer.values if v and v.strip())
    return ticked


def _uncomparable_answers(client) -> list[str]:
    """Questions on that same form which the archive has no column for.

    Scoped to the latest screening form too. Mixing "what she says now" with
    "what she once said cannot be checked" would describe two different
    questionnaires as though they were one.
    """
    sub = latest_screening(client)
    if sub is None:
        return []
    named: list[str] = []
    for answer in sub.answers or []:
        if not answer.values:
            continue
        text = (answer.question.text or "").lower()
        for fragment in UNCOMPARABLE:
            if fragment in text:
                label = (answer.question.text or "")[:48].rstrip(" :?")
                if label not in named:
                    named.append(label)
    return named


def compare(client, hospital_records) -> MatchResult:
    """Compare one patient's answers against their rows in the archive."""
    result = MatchResult(linked=bool(client.hospital_id),
                         hospital_id=client.hospital_id,
                         visits=len(hospital_records))
    if not result.linked or not hospital_records:
        return result

    archive = {r.condition for r in hospital_records if r.condition}
    reported = _reported_conditions(client)
    result.unverifiable = _uncomparable_answers(client)

    matched_archive: set[str] = set()
    for label in reported:
        equivalents = CONDITION_MAP.get(label, set())
        hit = equivalents & archive
        if hit:
            result.confirmed.append(f"{label} = {'/'.join(sorted(hit))}")
            matched_archive |= hit
        elif equivalents:
            # The form label maps to something the archive uses, and that
            # something is not on this patient's record.
            result.patient_only.append(label)
        else:
            # The archive has no column for this at all - not a disagreement.
            if label not in result.unverifiable:
                result.unverifiable.append(label)

    # Anything in the archive the patient did not report. Only counts when the
    # form actually offered them a way to report it.
    for condition in sorted(archive - matched_archive):
        if _REVERSE.get(condition):
            result.records_only.append(condition)

    return result


# --- sanity-checking a link ------------------------------------------------
#
# Staff type a patient number by hand. Type 8 instead of 9 and a different real
# person's medical history attaches to this patient, and the Match column then
# reports a discrepancy that does not exist. These checks are the speed bump.
#
# They warn. They never block, and they are never evidence on their own, for a
# reason worth stating: the data legitimately disagrees. An archive row is a
# snapshot of one past visit, and people's records are not tidy. A check that
# blocks would be worked around within a week; a check that cries wolf would be
# ignored within two.

SEX_QUESTION = "sex"                  # the ADHD form's "Sex:" field
BINARY = {"female", "male"}


def _recorded_sex(client) -> str:
    """The patient's own answer to a plain 'Sex' field, if a form asked one.

    Gender identity is deliberately NOT read here. The archive records a binary
    Gender; a trans patient's gender identity will differ from it for reasons
    that have nothing to do with whether the link is right, and a warning that
    fires on that would be both wrong and harmful - a "these may be different
    people" flag on a correctly linked record, shown to staff.
    """
    for sub in client.submissions_list:
        for answer in sub.answers or []:
            if (answer.question.text or "").lower().startswith("please enter your info"):
                for label, value in (answer.mapping or {}).items():
                    if label.strip().lower().rstrip(":") == SEX_QUESTION:
                        return (value or "").strip()
    return ""


def _age_now(client) -> int | None:
    if not client.dob:
        return None
    today = date.today()
    return (today.year - client.dob.year -
            ((today.month, today.day) < (client.dob.month, client.dob.day)))


def check_link(client, hospital_records) -> list[str]:
    """What a person should look at before trusting this link. Never blocking."""
    notes: list[str] = []

    if not hospital_records:
        notes.append(f"Number {client.hospital_id} is not in the archive at all. "
                     f"Either it is mistyped, or this patient's history has not "
                     f"been imported yet.")
        return notes

    # A date of birth is a far stronger signal than an age, so use it when the
    # archive has one: two people can be 61, but a matching birth date is a
    # deliberate coincidence.
    born = {r.dob for r in hospital_records if r.dob}
    if client.dob and born and client.dob not in born:
        shown = ", ".join(d.strftime("%d %b %Y") for d in sorted(born))
        notes.append(
            f"The archive gives this number a date of birth of {shown}; "
            f"{client.first_name or 'this patient'} is recorded as "
            f"{client.dob:%d %b %Y}. Different dates of birth usually mean "
            f"different people.")

    names = {r.name for r in hospital_records if r.name}
    if names and client.name and client.name.lower() not in {n.lower() for n in names}:
        notes.append(f"The archive calls this number {', '.join(sorted(names))}; "
                     f"this patient is {client.name}. Worth a look - names are "
                     f"spelled differently and do change, so this alone proves "
                     f"nothing.")

    ages = sorted({r.age for r in hospital_records if r.age})
    now = _age_now(client)
    if now is not None and ages:
        # The only genuinely impossible direction. A past visit recording an age
        # HIGHER than the patient is today cannot be the same person. A much
        # lower age is just an old visit, which is normal and not worth a word.
        older = [a for a in ages if a > now + 1]
        if older:
            notes.append(
                f"The archive records this number as aged {', '.join(map(str, older))}, "
                f"but {client.first_name or 'this patient'} is {now} today. Nobody gets "
                f"younger - this is very likely a different person.")

    sex = _recorded_sex(client).lower()
    archive_sexes = {r.gender.strip().lower() for r in hospital_records if r.gender}
    if sex in BINARY and archive_sexes and archive_sexes <= BINARY and sex not in archive_sexes:
        notes.append(
            f"The patient's form says {sex.title()}; the archive says "
            f"{'/'.join(sorted(s.title() for s in archive_sexes))}. Worth a second "
            f"look, though records disagree for legitimate reasons and this alone "
            f"does not mean the number is wrong.")

    if not client.dob and not sex:
        notes.append("Nothing to check this number against - no date of birth on "
                     "file and no form answer giving sex. The link is taken on trust.")

    return notes
