# Meridian Research Pre-Screening — Python pipeline

Pulls referrals from IntakeQ, matches each person to their Tebra chart, and writes the
30-column review workbook. Claude never sees patient-identifying data.

## The short version

**Most of this needs no AI at all.**

Of the 30 workbook columns:

| | Count | Who fills it |
|---|---|---|
| Staff-owned | 8 | Your team types them. The pipeline never writes these. |
| Automated | 22 | Deterministic Python. API field → Excel cell. **No model involved.** |

So the workbook itself is a plumbing job — `zeep` + `requests` + `openpyxl`. No Bedrock,
no LLM, no masking problem, because no patient data ever leaves the process.

An LLM is only useful for one thing that is *not* in the 30 columns: reading the trial's
prose eligibility criteria and judging them against a medication and diagnosis picture.
That is the only call that goes out, and it goes out de-identified.

## The masking boundary

```
IntakeQ API ─┐
             ├─► [ full record, PHI ]  ── stays inside this process ──► workbook.xlsx
Tebra API ───┘            │
                          │  deidentify.to_masked_payload()
                          ▼
                   [ masked payload ]  ── the only thing that leaves ──► Claude API
                          │                  study_id: "RD-0001"
                          │                  age: 34
                          ▼                  meds: [{name, dose, days_active}]
                   [ criteria verdicts ]     dx: [{icd10, description}]
                          │
                          └──► re-joined on study_id, written to the workbook
```

What crosses the boundary:

- `RD-0001` — a sequential study ID. **Not derived from any patient attribute**, which is
  what HIPAA §164.514(c) requires of a re-identification code.
- Age as an integer, or `"90+"` for 90 and over.
- Medications: name, dose, and **durations in days** — never start/stop dates.
- Diagnoses: ICD-10 code and description.

What never crosses: name, DOB, address, city, ZIP, phone, email, Tebra/IntakeQ IDs,
appointment dates, free-text notes.

### Why durations instead of dates

Safe Harbor strips all date elements finer than a year. But the trial's 8-week stability
rule needs date precision. Both hold if Python does the arithmetic and sends the *result*:
`{"name": "sertraline", "days_active": 94}` instead of `{"start": "2026-06-14"}`.
A duration is not a date, so it is not an identifier — and the model gets more useful
input than a raw date anyway.

### The guard

`deidentify.assert_no_phi()` re-reads the serialized payload and refuses to send if the
patient's actual name, phone, email, DOB or ZIP appears anywhere in it. We know the real
identifiers at that moment, so this is an exact-match check, not heuristic scrubbing. It
raises `PHILeakError` and the run stops.

## What this does not get you out of

- **The BAA with the practice is still required.** De-identification removes the need for a BAA
  with the *AI vendor*. This process still reads charts, still handles PHI, still writes
  names into the workbook. Symbiosys is still a business associate.
- **API access still has to be granted.** IntakeQ's key and Tebra's Customer Care approval
  are vendor terms, not HIPAA. Masking changes nothing there.
- **Free-text notes stay out.** Names leak inside narrative text and exact-match scrubbing
  can't catch every form. If you later want notes read, run Microsoft Presidio locally
  first — don't widen the payload.

## Open risk: Tebra clinical data

The Tebra **SOAP** API is practice management — patients, appointments, encounters,
billing, documents. Medications and problem lists are not on that surface.

Available documentation indicates Tebra's clinical/FHIR data is a **patient-access** API,
scoped to an individual patient's own record, not a bulk practice-wide query. If that
holds, columns 11, 21 and 22 (Dx Codes, Active Meds, Inactive Meds) cannot be pulled
automatically at all — and those three are exactly what the screening depends on.

**Confirm this in the Tebra Customer Care case before building anything past the matcher.**

`tebra.py` handles both outcomes. `SoapClinicalSource` raises with an explanation.
`CsvExportClinicalSource` reads a chart export your coordinator saves — slower, needs a
manual step, but every other part of the pipeline works unchanged.

## Layout

```
run.py              orchestrator — one referral in, one workbook row out
src/config.py       env loading
src/vault.py        study ID ↔ real identity crosswalk (SQLite, never leaves)
src/intakeq.py      REST client, rate-limited to 10/min and 500/day
src/tebra.py        SOAP client + the clinical-source fallback
src/match.py        deterministic DOB + name + phone/email matching
src/deidentify.py   the masking boundary and the PHI guard
src/screen.py       criteria screening on masked data (the only outbound call)
src/workbook.py     openpyxl writer, staff columns protected
```

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env     # fill in credentials
python run.py --once <intakeq_client_id>
```

Credentials come from the environment. Nothing is committed — `.env` is gitignored.

## Rate limits

IntakeQ standard PracticeQ allows ~10 requests/minute and 500/day. `intakeq.py` enforces
both with a token bucket and blocks rather than erroring. This is why the design is
webhook-driven: polling for new referrals would spend the daily budget on empty checks.
