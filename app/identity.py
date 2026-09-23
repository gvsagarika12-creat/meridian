"""Finding a patient in the archive, and adding one who is not there.

Typing a hospital number by hand has two faults. It assumes somebody already
knows the number, and a single mistyped digit silently attaches a different
person's medical history - which the app can warn about afterwards but cannot
prevent.

So the app proposes instead. Given a patient's name, date of birth and sex it
scores every archive row and offers the likely ones with a confidence figure and
the reasons behind it. A person confirms or rejects; nothing links itself.

Two rules shaped the scoring.

**Date of birth dominates.** Two people share a name far more often than they
share a birth date, so a matching date is worth more than everything else
combined, and a *conflicting* date is disqualifying however well the names read.

**Every score shows its working.** A bare "88% confidence" is unarguable. The
reasons are listed so a coordinator can disagree with the number.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from .hospital import HospitalRecord

# Numbers the practice issues for patients the hospital has never seen. Far
# above the hospital's own range so the two can never collide, and so that any
# number this large is instantly recognisable as one we issued.
PRACTICE_BASE = 900_000


@dataclass
class Candidate:
    record: HospitalRecord
    score: int                       # 0-100
    reasons: list[str] = field(default_factory=list)
    against: list[str] = field(default_factory=list)

    @property
    def confidence(self) -> str:
        if self.score >= 85:
            return "high"
        if self.score >= 60:
            return "likely"
        return "weak"


def _norm(value: str) -> str:
    return (value or "").strip().lower()


def _age_on(born: date | None, today: date | None = None) -> int | None:
    if not born:
        return None
    today = today or date.today()
    return today.year - born.year - ((today.month, today.day) < (born.month, born.day))


def score_one(client, record: HospitalRecord) -> Candidate:
    """How likely is this archive row to be this patient?"""
    c = Candidate(record=record, score=0)

    first = _norm(client.first_name)
    last = _norm(client.last_name)
    r_name = _norm(record.name)
    r_parts = r_name.split()

    # --- date of birth, the strongest signal available ---------------------
    if client.dob and record.dob:
        if client.dob == record.dob:
            c.score += 55
            c.reasons.append(f"date of birth matches exactly ({record.dob:%d %b %Y})")
        else:
            # Not merely unhelpful - actively contradicting. Say so and stop
            # the score from climbing on the strength of a common surname.
            c.against.append(f"different date of birth "
                             f"({record.dob:%d %b %Y} vs {client.dob:%d %b %Y})")
            c.score -= 45

    # --- name ---------------------------------------------------------------
    if last and any(last == p for p in r_parts):
        c.score += 20
        c.reasons.append(f"surname matches ({client.last_name})")
    if first and r_parts and first == r_parts[0]:
        c.score += 20
        c.reasons.append(f"first name matches ({client.first_name})")
    elif first and r_parts and r_parts[0].startswith(first[:1]) and len(first) > 1:
        # "Kathryn" against "Katherine" - worth surfacing, not worth much.
        c.score += 5
        c.reasons.append("first names are similar")

    # --- age, when there is no date of birth to use instead ------------------
    age_now = _age_on(client.dob)
    if age_now is not None and record.age:
        gap = abs(age_now - record.age)
        if not (client.dob and record.dob):        # dob already counted above
            if gap <= 1:
                c.score += 10
                c.reasons.append(f"age agrees ({record.age})")
            elif gap > 12:
                c.score -= 15
                c.against.append(f"age is {record.age}, this patient is {age_now}")
        elif record.age > age_now + 1:
            c.against.append(f"recorded as older ({record.age}) than the patient is now")

    # --- sex ----------------------------------------------------------------
    from .matching import _recorded_sex
    sex = _norm(_recorded_sex(client))
    r_sex = _norm(record.gender)
    if sex and r_sex:
        if sex == r_sex:
            c.score += 5
            c.reasons.append(f"sex agrees ({record.gender})")
        else:
            c.score -= 25
            c.against.append(f"archive says {record.gender}, the form says "
                             f"{_recorded_sex(client)}")

    c.score = max(0, min(100, c.score))
    return c


def candidates(db, client, limit: int = 4, floor: int = 35) -> list[Candidate]:
    """The archive rows most likely to be this patient.

    Narrowed in SQL first - scoring 984 rows in Python to show four of them is
    work nobody asked for, and it would get worse with a real archive.
    """
    if client.hospital_id:
        return []

    query = db.query(HospitalRecord)
    conditions = []
    if client.dob:
        conditions.append(HospitalRecord.dob == client.dob)
    if client.last_name.strip():
        conditions.append(HospitalRecord.name.ilike(f"%{client.last_name.strip()}%"))
    if client.first_name.strip():
        conditions.append(HospitalRecord.name.ilike(f"%{client.first_name.strip()}%"))
    if not conditions:
        return []

    from sqlalchemy import or_
    rows = query.filter(or_(*conditions)).limit(200).all()

    scored = [score_one(client, r) for r in rows]
    scored = [s for s in scored if s.score >= floor]
    scored.sort(key=lambda s: (-s.score, s.record.patient_id))

    # One row per patient number: a returning patient has several visits, and
    # offering the same person four times is not four candidates.
    seen, unique = set(), []
    for s in scored:
        if s.record.patient_id in seen:
            continue
        seen.add(s.record.patient_id)
        unique.append(s)
    return unique[:limit]


def next_practice_number(db) -> int:
    """The next number to issue for a patient the hospital has never seen."""
    from sqlalchemy import func
    highest = (db.query(func.max(HospitalRecord.patient_id))
               .filter(HospitalRecord.patient_id > PRACTICE_BASE).scalar())
    return (highest or PRACTICE_BASE) + 1
