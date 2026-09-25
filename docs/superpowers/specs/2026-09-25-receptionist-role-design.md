# Receptionist role

## Problem

Front-desk staff need a role that shows them, in one place: which patients are
coming in today (and how — online or in person), a fast way to register a new
patient, and each doctor's patient list so they can route referral-based
cases correctly.

## Existing state

The app already has a `front_desk` role (`UserRole.front_desk` in
`app/models.py`), shown on screen as "front desk," with this permission set
(`app/permissions.py`):

- `FORMS_VIEW`
- `CLIENTS_VIEW`, `CLIENTS_EDIT` (registration is `CLIENTS_EDIT` — already
  covers adding a new patient via `POST /clients/new`)
- `SCHEDULE_EDIT` (booking appointments)
- `SUBMISSIONS_VIEW`, `SUBMISSIONS_SEND`
- `BILLING_EDIT`

There is no structured referral-source field (only a free-text intake
question, "Who referred you to our practice?", and a "Referral letter"
document category) — but the request turned out to mean the doctor a patient
is already assigned to (`Client.provider_id`), which the app already tracks
and already powers the `/my-patients` caseload screen. `/my-patients` only
requires `CLIENTS_VIEW`, which `front_desk` already has — it's just missing a
nav link and doesn't handle a non-practitioner default sensibly beyond
showing everyone.

Appointments (`app/schedule.py`) have a `kind` field (visit purpose:
Screening visit, Consultation, Follow-up, Trial visit, Telehealth,
Assessment, Other) but nothing that separately captures *how* the visit
happens (online vs in person). There is no patient self-service booking
anywhere in the app — every appointment is entered by staff.

## Design

### 1. Relabel, don't duplicate

Keep the `front_desk` enum value as-is in the database (avoids a Postgres
`ALTER TYPE` and touching every existing row). Change only the display label:

```python
# app/permissions.py
ROLE_LABEL = {
    "practitioner": "doctor",
    "front_desk": "Receptionist",   # was "front desk"
    "read_only": "read only",
}
```

No change to `ROLE_PERMISSIONS[UserRole.front_desk]` — the existing set
already covers registration, scheduling, and viewing the by-doctor caseload.

### 2. Appointment channel field

Add a new column, orthogonal to `kind`:

```python
# app/schedule.py
class Channel(str, enum.Enum):
    in_person = "in_person"
    online = "online"

class Appointment(Base):
    ...
    channel: Mapped[Channel] = mapped_column(Enum(Channel), default=Channel.in_person)
```

Migration script `add_appointment_channel_column.py` at the repo root,
following the existing pattern (see `add_provider_column.py`): checks
whether the column exists, adds it with `ALTER TABLE appointments ADD COLUMN
channel ...` if not, safe to run twice, supports `TARGET_DATABASE_URL` for
the hosted database.

The booking form on the calendar screen (`app/templates/calendar.html`,
booked via `main.py`'s appointment-creation route around line 2092) gets a
channel radio/select (In person / Online), defaulting to In person.

### 3. `/reception` — the Receptionist home page

New route, gated the same way `/my-patients` is (`CLIENTS_VIEW`), rendering
a new template `reception.html`. Three sections on one page:

**Today's patients** — every `Appointment` where `on_day == today`, ordered
by `at_time`, showing time, patient name (linking to their chart), assigned
doctor, channel (Online / In person badge), and status. Same query shape as
the `todays_appts` block already built in `home()` for the admin dashboard —
reused, not reinvented.

**Register new patient** — the existing add-patient form, posting to the
existing `POST /clients/new` (no backend change), just presented directly
on this page instead of requiring a trip to `/clients` first.

**Cases by doctor** — a doctor picker (same `doctors` query already used by
`/my-patients`) that lists that doctor's full patient roster. Implemented by
linking to `/my-patients?provider={id}` (already reachable given
`CLIENTS_VIEW`) rather than re-implementing the caseload query a second
time — this page shows per-doctor patient *counts* as a preview, each one
linking through to the existing screen for the full list.

### 4. Routing and navigation

`home()` in `main.py` gets a second role-based redirect, next to the
existing practitioner one:

```python
if me and me.role == UserRole.practitioner:
    return RedirectResponse("/my-patients", status_code=303)
if me and me.role == UserRole.front_desk:
    return RedirectResponse("/reception", status_code=303)
```

Add a nav entry for `/reception` (visible under the same condition as the
existing `CLIENTS_VIEW`-gated links) and give `/my-patients` a nav link
visible to `front_desk` too (today it only shows under `CLINICAL_EDIT`,
which `front_desk` doesn't have), so the doctor-list view stays reachable
from the top nav, not just by clicking through from `/reception`.

## Out of scope

- No structured "outside referring doctor" field — confirmed this request
  meant the internal assigned doctor, which already exists.
- No patient self-service online booking — "online" is a label staff apply
  when they book, not a new booking channel for patients.
- No change to who can *change* another role's permissions — this only adds
  a label change, a new column, and a new page; `app/permissions.py`'s
  policy table and `/users` guardrails are untouched.

## Addendum (implemented 2026-09-25): permission trim and demo account rename

After the initial design was approved, the request was narrowed further:
receptionist should see **only** patients and the calendar — no billing,
documents, messages, forms, consents, submissions, outbox, broadcasts,
experience surveys, or the research section (records/archive/trials/
reports).

Checked what `front_desk`'s existing permission set actually gated in the
nav (`app/templates/base.html`) and found `FORMS_VIEW`, `SUBMISSIONS_VIEW`,
`SUBMISSIONS_SEND`, and `BILLING_EDIT` between them covered every one of
those excluded items. Removing all four from `front_desk` leaves exactly
`CLIENTS_VIEW`, `CLIENTS_EDIT`, `SCHEDULE_EDIT` — patients and scheduling,
nothing else.

One item didn't fall out cleanly: **Documents** was gated by `CLIENTS_VIEW`,
the same permission needed for patients and the calendar, so removing
`CLIENTS_VIEW` would have taken those away too. Fixed by splitting a new
`DOCUMENTS_VIEW` permission out of `CLIENTS_VIEW` (`app/permissions.py`),
granted to `owner`, `admin`, `practitioner`, and `read_only` (preserving
their current access) but not `front_desk`. The five non-delete document
routes in `app/main.py` (`documents_list`, `document_upload`, `document_file`,
`document_file_to`, `document_processed`) and the Documents nav link in
`app/templates/base.html` now check `DOCUMENTS_VIEW` instead of
`CLIENTS_VIEW`/`CLIENTS_EDIT`. `document_delete` is unchanged — it was
already gated on `USERS_MANAGE`, the narrowest permission in the app, which
this change doesn't touch.

Also: the seeded demo `front_desk` account's display name (`User.name`,
separate from `role_label()`, which was already becoming "Receptionist" per
this spec's main body) was "Research Coordinator" — renamed to
"Receptionist" in both `app/seed.py` (for future fresh installs) and the
already-seeded row in the running database.

## Testing

- Migration script runs clean on a fresh DB and is a no-op on a second run.
- A `front_desk` user logging in lands on `/reception`, not the admin
  dashboard.
- `/reception` shows today's appointments with the correct channel badge,
  the add-patient form successfully creates a client (same as `/clients/new`
  today), and the by-doctor counts link through to the correct filtered
  `/my-patients` view.
- A `practitioner` or `admin` user is unaffected — no behavior change for
  any other role.
