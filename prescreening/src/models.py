"""Shared record types. No I/O, no dependencies on other project modules."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum


class Verdict(str, Enum):
    """The three marks from the proposal. There is deliberately no fourth."""

    MEETS = "meets"
    POTENTIAL_EXCLUSION = "potential_exclusion"
    NEEDS_VERIFICATION = "needs_verification"


@dataclass
class Medication:
    name: str
    dose: str | None = None
    start: date | None = None
    stop: date | None = None
    is_active: bool = True
    # Set when the chart records a dose change; the 8-week clock restarts from here.
    current_dose_since: date | None = None

    def days_on_current_dose(self, index: date) -> int | None:
        """None means 'the chart does not say' — never treat it as zero."""
        anchor = self.current_dose_since or self.start
        if anchor is None:
            return None
        return ((self.stop or index) - anchor).days


@dataclass
class Diagnosis:
    icd10: str
    description: str = ""
    recorded: date | None = None


@dataclass
class Appointment:
    when: date
    provider: str | None = None


@dataclass
class PatientRecord:
    """Everything known about one referral. Holds PHI; stays in-process."""

    study_id: str
    name: str = ""
    dob: date | None = None
    phone: str | None = None
    email: str | None = None
    city: str | None = None
    zip_code: str | None = None
    state: str | None = None
    provider: str | None = None

    tebra_patient_id: str | None = None
    intakeq_client_id: str | None = None

    medications: list[Medication] = field(default_factory=list)
    diagnoses: list[Diagnosis] = field(default_factory=list)
    appointments: list[Appointment] = field(default_factory=list)

    # From the IntakeQ referral form.
    referral_type: str | None = None
    availability: str | None = None
    intakeq_email: str | None = None
    rating_scales: list[tuple[str, str, date | None]] = field(default_factory=list)
    consents: list[tuple[str, date | None]] = field(default_factory=list)
    prescreening_sent: date | None = None

    # True when the chart yielded no medication list at all, as opposed to an
    # empty one. The rules engine must not read "no data" as "no medications".
    medications_unavailable: bool = False
    diagnoses_unavailable: bool = False

    def age_at(self, index: date) -> int | None:
        if self.dob is None:
            return None
        return index.year - self.dob.year - (
            (index.month, index.day) < (self.dob.month, self.dob.day)
        )

    def active_medications(self) -> list[Medication]:
        return [m for m in self.medications if m.is_active]

    def inactive_medications(self) -> list[Medication]:
        return [m for m in self.medications if not m.is_active]


@dataclass
class CriterionResult:
    criterion_id: str
    description: str
    verdict: Verdict
    evidence: str

    def __str__(self) -> str:
        return f"{self.criterion_id} [{self.verdict.value}] {self.evidence}"
