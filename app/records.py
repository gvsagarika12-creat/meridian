"""The combined sheet - one row per patient, form answers and chart side by side.

This is the point of the whole platform. In the real setup the answers live in
IntakeQ and the medications live in Tebra, and somebody matches them by hand.
Here both hang off the same Client row, so the sheet is a join, not a
reconciliation, and the download is the thing a coordinator actually wanted.

Every column is declared once, in COLUMNS, and both the screen and the
spreadsheet read from it. Two lists would drift within a week.
"""

from __future__ import annotations

import io
from datetime import date, datetime

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from sqlalchemy.orm import object_session

from . import matching
from . import trials as trial_screening
from .trials import Trial
from .hospital import records_for
from .models import SubmissionStatus


def _archive(client):
    """This patient's rows in the hospital archive.

    Looked up through the session the client is already attached to. There is no
    relationship to traverse because there is no foreign key - the two systems
    share a number, not a key, and modelling it as a key would be a lie that
    breaks the moment a patient has no number yet.
    """
    session = object_session(client)
    if session is None:
        return []
    return records_for(session, client.hospital_id)


def _match(client) -> matching.MatchResult:
    return matching.compare(client, _archive(client))


def _joined(values: list[str]) -> str:
    return "; ".join(v for v in values if v)


def _meds(client, active: bool) -> str:
    rows = []
    for m in sorted(client.medications, key=lambda x: (x.prescribed_on or date.min),
                    reverse=True):
        if m.is_active != active:
            continue
        piece = m.display
        if m.prescribed_on:
            piece += f" (started {m.prescribed_on:%d %b %Y}"
            days = m.days_active
            piece += f", {days} days)" if days is not None else ")"
        rows.append(piece)
    return _joined(rows)


def _tidy_grid(mapping: dict) -> str:
    """Turn a flattened table back into readable lines.

    The ADHD questionnaire stores its tables as one field per cell, labelled
    "Row - Column": {"1 - Name": "Paracetamol", "1 - Dosage": "500mg"}. Printed
    straight into a summary cell that reads as
    "1 - Name: Paracetamol; 1 - Dosage: 500mg", which is accurate and horrible.

    Regroup by the row, drop the row number, and join the values: the reader
    wanted "Paracetamol 500mg, since sep 2025", and every field is still in its
    own cell on the answers sheet for anyone who needs the detail.
    """
    rows: dict[str, list[str]] = {}
    for label, value in mapping.items():
        value = (value or "").strip()
        if not value or value.lower() in ("n/a", "na", "none", "-"):
            continue
        row, _, column = label.partition(" - ")
        rows.setdefault(row.strip(), []).append((column.strip(), value))

    lines = []
    for row, cells in rows.items():
        joined = ", ".join(v for _c, v in cells)
        # A numbered row ("1", "2") is just a slot, so its number adds nothing.
        # A named one ("Smoking", "Coffee") is the substance and has to stay.
        lines.append(joined if row.isdigit() else f"{row}: {joined}")
    return "; ".join(lines)


def _answer(client, *needles: str) -> str:
    """Most recent answer whose question text contains any of `needles`.

    Several needles, not one, because a column has to survive the form changing.
    "Current medications" was written against the Adult Packet's wording; the
    ADHD questionnaire asks the same thing as "Please list any prescribed
    medications you take". One needle meant the column silently emptied for every
    patient on the newer form - which reads as broken software rather than as a
    question nobody was asked.

    Newest submission first, and the first needle that hits wins, so a patient
    who has filled both forms shows their most recent answer.
    """
    subs = sorted((s for s in client.submissions_list if s.answers),
                  key=lambda s: s.submitted_at or datetime.min, reverse=True)
    wanted = [n.lower() for n in needles if n]
    for sub in subs:
        for a in sub.answers:
            text = (a.question.text or "").lower()
            if any(n in text for n in wanted):
                # A table answer reads far better regrouped than flattened.
                if a.mapping and any(" - " in k for k in a.mapping):
                    return _tidy_grid(a.mapping)
                return a.display
    return ""


def _latest_submission(client):
    subs = [s for s in client.submissions_list
            if s.status in (SubmissionStatus.submitted, SubmissionStatus.reviewed)]
    return max(subs, key=lambda s: s.submitted_at or datetime.min, default=None)


# (heading, how to get it, width). One definition, two consumers.
COLUMNS = [
    ("Study ID",        lambda c: f"RD-{c.id:04d}",                             11),
    ("Hospital ID",     lambda c: str(c.hospital_id) if c.hospital_id else "",  12),
    ("Patient Name",    lambda c: c.name,                                       22),
    ("DOB",             lambda c: c.dob.strftime("%d %b %Y") if c.dob else "",  13),
    ("Age",             lambda c: _age(c),                                       6),
    ("City",            lambda c: c.city,                                       14),
    ("Zip",             lambda c: c.postal_code,                                 8),
    ("Phone",           lambda c: c.phone,                                      16),
    ("Email",           lambda c: c.email,                                      26),
    ("Diagnoses",       lambda c: _joined([p.display for p in c.active_problems]), 42),
    ("Active Meds",     lambda c: _meds(c, active=True),                        46),
    ("Inactive Meds",   lambda c: _meds(c, active=False),                       36),
    ("Allergies",       lambda c: _joined([f"{a.substance} ({a.reaction})".replace(" ()", "")
                                           for a in c.allergies]) or "None documented", 22),
    ("Latest Vitals",   lambda c: _vitals(c),                                   30),
    # The ADHD questionnaire has no "presenting problem" question at all - it
    # asks about an ADHD diagnosis instead. Left as one needle deliberately: an
    # empty cell here is true, and filling it from a different question would be
    # answering something the patient was never asked.
    ("Presenting Problem", lambda c: _answer(c, "presenting problem"),          40),
    ("Current Medications (self-reported)",
     lambda c: _answer(c, "medications (psychotropic or not)",
                       "prescribed medications you take"),                      40),
    ("Substance Use",   lambda c: _answer(c, "Do you use any of the following",
                                          "Your habits"),                       34),
    ("Forms Received",  lambda c: str(sum(1 for s in c.submissions_list
                                          if s.status in (SubmissionStatus.submitted,
                                                          SubmissionStatus.reviewed))), 15),
    ("Last Form",       lambda c: (_latest_submission(c).form.name
                                   if _latest_submission(c) else ""),           28),
    ("Last Submitted",  lambda c: (_latest_submission(c).submitted_at.strftime("%d %b %Y")
                                   if _latest_submission(c) and
                                   _latest_submission(c).submitted_at else ""), 14),
    ("Chart data as of", lambda c: datetime.utcnow().strftime("%d %b %Y %H:%M"), 18),

    # --- the hospital archive, and the comparison ------------------------
    ("Archive Visits",  lambda c: str(len(_archive(c))) if c.hospital_id else "", 13),
    ("Archive Conditions",
     lambda c: _joined(sorted({r.condition for r in _archive(c) if r.condition})), 30),
    ("Archive Procedures",
     lambda c: _joined(sorted({r.procedure for r in _archive(c) if r.procedure})), 30),
    ("Archive Outcome",
     lambda c: _joined(sorted({r.outcome for r in _archive(c) if r.outcome})),   16),
    ("Match",           lambda c: _match(c).verdict,                            18),
    ("Match %",         lambda c: ("" if _match(c).score is None
                                   else f"{_match(c).score}%"),                 10),
    ("Agreed / Compared", lambda c: ("" if _match(c).score is None else
                                     f"{len(_match(c).confirmed)} of "
                                     f"{_match(c).comparable}"),                17),
    ("Match Detail",    lambda c: _match(c).summary,                            64),

    # --- trial screening -------------------------------------------------
    ("Trial",           lambda c: _screen(c)[0],                                34),
    ("Eligibility",     lambda c: _screen(c)[1],                                16),
    ("Criteria Met",    lambda c: _screen(c)[2],                                18),
    ("Eligibility Detail", lambda c: _screen(c)[3],                             64),
]


def _screen(client) -> tuple[str, str, str, str]:
    """Screen against the one active trial, if there is exactly one.

    Deliberately not a column per trial: a practice screening for six studies
    would get a sheet nobody can read across. With more than one active trial
    the cell says so and points at the Trials screen, where the comparison
    belongs.
    """
    session = object_session(client)
    if session is None:
        return ("", "", "", "")
    open_trials = session.query(Trial).filter_by(is_active=True).all()
    if not open_trials:
        return ("", "", "", "")
    if len(open_trials) > 1:
        return (f"{len(open_trials)} active trials", "", "",
                "More than one trial is recruiting - see the Trials screen.")
    trial = open_trials[0]
    s = trial_screening.evaluate(client, trial, _archive(client))
    return (trial.name, s.verdict, s.score_text, s.summary)


def _age(c) -> str:
    if not c.dob:
        return ""
    today = date.today()
    return str(today.year - c.dob.year -
               ((today.month, today.day) < (c.dob.month, c.dob.day)))


def _vitals(c) -> str:
    v = c.latest_vitals
    if not v:
        return ""
    parts = [f"BP {v.bp}" if v.bp else "", f"HR {v.hr}" if v.hr else "",
             f"Wt {v.weight}" if v.weight else "", f"BMI {v.bmi}" if v.bmi else ""]
    stamp = f" ({v.taken_on:%d %b %Y})" if v.taken_on else ""
    return _joined(parts) + stamp


#  A cell that opens with =, +, -, @ (or a leading tab/CR) is a formula to
#  Excel, not text - and several columns here are patient-typed free text
#  ("Presenting Problem", "Substance Use") that nobody has ever validated
#  against that. A prefixed apostrophe is the same fix spreadsheet software
#  uses when a person types a leading "=" themselves: it forces the cell to
#  stay text instead of being evaluated as a formula when the file is opened.
_FORMULA_LEADERS = ("=", "+", "-", "@", "\t", "\r")


def _neutralize(value):
    if isinstance(value, str) and value[:1] in _FORMULA_LEADERS:
        return "'" + value
    return value


def row_for(client) -> list[str]:
    return [fn(client) for _, fn, _ in COLUMNS]


def headings() -> list[str]:
    return [h for h, _, _ in COLUMNS]


def to_excel(clients) -> bytes:
    """The download. Returns bytes so the caller decides how to serve them."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Patient records"

    header_fill = PatternFill("solid", fgColor="1B2430")
    for col, (heading, _, width) in enumerate(COLUMNS, start=1):
        cell = ws.cell(row=1, column=col, value=heading)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = header_fill
        cell.alignment = Alignment(vertical="center", wrap_text=True)
        ws.column_dimensions[get_column_letter(col)].width = width

    for r, client in enumerate(clients, start=2):
        for c, value in enumerate(row_for(client), start=1):
            cell = ws.cell(row=r, column=c, value=_neutralize(value))
            cell.alignment = Alignment(vertical="top", wrap_text=True)

    ws.freeze_panes = "C2"
    ws.auto_filter.ref = ws.dimensions

    _add_answers_sheet(wb, clients)

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _add_answers_sheet(wb, clients) -> None:
    """Second sheet: every answer, one row each.

    The summary sheet has a fixed set of columns, so most of what a patient types
    has nowhere to go - eighteen name and address fields, a nine-row severity
    matrix, the medical history block. Those are stored but invisible there.
    This sheet carries all of it, so "download everything" is true rather than
    nearly true.
    """
    ws = wb.create_sheet("Form answers")
    headings = ["Study ID", "Patient", "Form", "Submitted", "Page",
                "Question", "Field", "Answer"]
    widths = [11, 22, 26, 14, 6, 52, 30, 46]

    header_fill = PatternFill("solid", fgColor="1B2430")
    for col, (heading, width) in enumerate(zip(headings, widths), start=1):
        cell = ws.cell(row=1, column=col, value=heading)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = header_fill
        cell.alignment = Alignment(vertical="center", wrap_text=True)
        ws.column_dimensions[get_column_letter(col)].width = width

    r = 2
    for client in clients:
        for sub in sorted(client.submissions_list,
                          key=lambda x: x.submitted_at or datetime.min):
            if not sub.answers:
                continue
            submitted = sub.submitted_at.strftime("%d %b %Y") if sub.submitted_at else ""
            by_question = {a.question_id: a for a in sub.answers}

            for q in sub.form.questions:
                answer = by_question.get(q.id)
                if not answer or not answer.values:
                    continue

                # A block or matrix answer becomes one row per field, so each
                # value sits in its own cell rather than inside a joined string.
                if answer.mapping:
                    for field, value in answer.mapping.items():
                        _write(ws, r, [f"RD-{client.id:04d}", client.name, sub.form.name,
                                       submitted, q.page, q.text, field, value])
                        r += 1
                else:
                    _write(ws, r, [f"RD-{client.id:04d}", client.name, sub.form.name,
                                   submitted, q.page, q.text, "", answer.display])
                    r += 1

    ws.freeze_panes = "C2"
    if r > 2:
        ws.auto_filter.ref = f"A1:H{r - 1}"


def _write(ws, row: int, values: list) -> None:
    for col, value in enumerate(values, start=1):
        cell = ws.cell(row=row, column=col, value=_neutralize(value))
        cell.alignment = Alignment(vertical="top", wrap_text=True)
