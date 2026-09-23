"""The masking boundary.

Everything above this module may hold PHI. Nothing below it may. `to_masked_payload`
is the only sanctioned way to build something that gets sent to an external service,
and `assert_no_phi` is the tripwire that proves it worked.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

# HIPAA Safe Harbor (45 CFR 164.514(b)(2)) removes all date elements finer than a year
# and requires ages over 89 to be aggregated.
MAX_REPORTABLE_AGE = 89


class PHILeakError(RuntimeError):
    """Raised when an identifier is found in a payload about to leave the process.

    This is a hard stop, never a warning. If it fires, the payload builder has a bug
    and the run must not continue.
    """


@dataclass
class Medication:
    name: str
    dose: str | None
    start: date | None
    stop: date | None
    is_active: bool


@dataclass
class Diagnosis:
    icd10: str
    description: str


@dataclass
class PatientRecord:
    """The full, identified record. Never serialized outbound."""

    study_id: str
    name: str
    dob: date | None
    phone: str | None
    email: str | None
    city: str | None
    zip_code: str | None
    state: str | None
    tebra_patient_id: str | None
    intakeq_client_id: str | None
    medications: list[Medication] = field(default_factory=list)
    diagnoses: list[Diagnosis] = field(default_factory=list)


def _age_from(dob: date | None, index: date) -> int | str | None:
    if dob is None:
        return None
    age = index.year - dob.year - ((index.month, index.day) < (dob.month, dob.day))
    return f"{MAX_REPORTABLE_AGE + 1}+" if age > MAX_REPORTABLE_AGE else age


def _days_between(start: date | None, end: date | None) -> int | None:
    if start is None or end is None:
        return None
    return (end - start).days


def to_masked_payload(record: PatientRecord, index: date | None = None) -> dict[str, Any]:
    """Build the de-identified payload.

    Medication timing is expressed as durations, not dates. `days_active` is what the
    trial's 8-week stability rule actually needs, and a duration carries no date element,
    so Safe Harbor is satisfied without losing the clinical signal.
    """
    index = index or date.today()

    meds = []
    for m in record.medications:
        meds.append(
            {
                "name": m.name,
                "dose": m.dose,
                "active": m.is_active,
                # Active: how long they have been on it. Stopped: how long ago it ended.
                "days_active": _days_between(m.start, m.stop or index),
                "days_since_stopped": None if m.is_active else _days_between(m.stop, index),
            }
        )

    return {
        "study_id": record.study_id,
        "age": _age_from(record.dob, index),
        "state": record.state,  # State is permitted; city and ZIP are not.
        "medications": meds,
        "diagnoses": [{"icd10": d.icd10, "description": d.description} for d in record.diagnoses],
    }


def _identifier_fragments(record: PatientRecord) -> list[str]:
    """Every literal string that must not appear in an outbound payload."""
    frags: list[str] = []

    for part in re.split(r"[\s,]+", record.name or ""):
        if len(part) >= 3:  # skip initials and particles — too short to match safely
            frags.append(part)

    for value in (record.phone, record.email, record.city, record.zip_code,
                  record.tebra_patient_id, record.intakeq_client_id):
        if value and len(str(value)) >= 3:
            frags.append(str(value))

    if record.dob:
        frags += [
            record.dob.isoformat(),
            record.dob.strftime("%m/%d/%Y"),
            record.dob.strftime("%m-%d-%Y"),
        ]

    # Digits only, so formatting differences don't slip past: (909) 555-0134 vs 9095550134
    if record.phone:
        digits = re.sub(r"\D", "", record.phone)
        if len(digits) >= 7:
            frags.append(digits)

    return frags


def assert_no_phi(payload: dict[str, Any], record: PatientRecord) -> None:
    """Refuse to send a payload containing any known identifier for this patient.

    Exact matching against the identifiers we hold right now — stronger than heuristic
    scrubbing, because there is no guessing about what counts as a name.
    """
    blob = json.dumps(payload, default=str).lower()
    blob_digits = re.sub(r"\D", "", blob)

    for frag in _identifier_fragments(record):
        needle = frag.lower()
        if needle and needle in blob:
            raise PHILeakError(
                f"identifier for {record.study_id} found in outbound payload "
                f"(matched a {len(frag)}-character fragment); refusing to send"
            )
        digits = re.sub(r"\D", "", frag)
        if len(digits) >= 7 and digits in blob_digits:
            raise PHILeakError(
                f"digit sequence for {record.study_id} found in outbound payload; refusing to send"
            )

    # A bare date anywhere in the payload means a builder regressed to sending dates.
    if re.search(r"\b\d{4}-\d{2}-\d{2}\b", blob):
        raise PHILeakError(
            f"payload for {record.study_id} contains an ISO date; send durations, not dates"
        )


def build_safe_payload(record: PatientRecord, index: date | None = None) -> dict[str, Any]:
    """Build and verify in one call. Use this — not `to_masked_payload` directly."""
    payload = to_masked_payload(record, index)
    assert_no_phi(payload, record)
    return payload
