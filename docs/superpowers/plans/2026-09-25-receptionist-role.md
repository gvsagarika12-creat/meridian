# Receptionist Role Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the existing `front_desk` role a receptionist-facing home page (`/reception`) showing today's patients (with an online/in-person channel badge), a quick patient-registration form, and a by-doctor case list — and relabel the role "Receptionist" on screen.

**Architecture:** This is additive work inside an existing FastAPI + SQLAlchemy + Jinja2 app (`app/main.py` is the single route file, `app/schedule.py` holds appointment logic, `app/permissions.py` holds the role/permission policy). No new modules — everything is new routes/template blocks alongside the existing ones, following the file's own conventions exactly.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2 (declarative, `Mapped`/`mapped_column`), Jinja2 templates, PostgreSQL (via Docker, `ipmg-postgres` container). No frontend build step — templates are server-rendered, styling is the existing `app/static/app.css` classes only.

> **STATUS: all 7 tasks complete (2026-09-25).** Executed inline, not via subagent-driven-development. Commits: `b33047a` (Task 1, expanded per the design spec's Addendum), `8bf585a` (Tasks 2-3 — note the `channel` enum type had to be renamed to `appointment_channel`; `app/broadcasts.py` already had an unrelated `Channel` enum that collided on Postgres's default type-naming), `a637c11` (Tasks 4-7, combined). End-to-end verified against a live test server (login → redirect → render → nav contents → by-doctor links), not just per-task manual steps as originally written.

## Global Constraints

- **No automated test suite exists in this repository** (confirmed: no `pytest`, no `tests/` directory, only a manual `test-email.py` script). Do not introduce a testing framework as part of this work — that would be unrelated scope. Every task below is verified manually instead: with `psql` against the running `ipmg-postgres` container, `curl` against the running app, and/or the design spec's own testing checklist. This matches the codebase's existing convention of hand-run, idempotent migration scripts with no test harness.
- The app is normally launched via `RUN.bat` (starts Docker, then `pythonw desktop.py`) or directly with `python desktop.py` / `python -m uvicorn app.main:app` for a console you can see errors in. For this work, run `python desktop.py` in a foreground terminal you control so you can see tracebacks and confirm the server is up before testing routes.
- Follow existing code style exactly: comments explain *why*, not *what* (see any existing docstring in `app/main.py` or `app/schedule.py` for the tone); inline CSS on template elements, no new CSS classes unless reusing `app/static/app.css` ones that already exist (`panel`, `body`, `crumb`, `actions`, `btn`, `btn-green`, `btn-red`, `stats`, `stat`, `hint`, `case-head`, `case-filter`).
- Every DB-touching route must call `log(db, ...)` before `db.commit()`, matching every other route in `app/main.py` (this is the audit trail; there is no code path that skips it).
- The `front_desk` **enum value** in the database is never renamed — only its display label (`ROLE_LABEL`) changes. Renaming the Postgres enum value would require an `ALTER TYPE` migration against every copy of the database and is explicitly out of scope per the design spec.

---

## File Structure

| File | Change |
|---|---|
| `app/permissions.py` | `ROLE_LABEL["front_desk"]` → `"Receptionist"` |
| `app/schedule.py` | New `Channel` enum, new `Appointment.channel` column |
| `add_appointment_channel_column.py` (new, repo root) | Migration script, mirrors `add_provider_column.py` |
| `app/templates/calendar.html` | Add a channel selector to the existing booking form |
| `app/main.py` | `calendar_book()` accepts `channel`; new `GET /reception` route; `home()` gets a `front_desk` redirect |
| `app/templates/reception.html` (new) | The receptionist home page: today's patients, by-doctor counts, add-patient form |
| `app/templates/base.html` | Nav: add `/reception` link; make `/my-patients` link visible to `front_desk` too |

---

### Task 1: Relabel `front_desk` as "Receptionist"

**Files:**
- Modify: `app/permissions.py:123-127`

**Interfaces:**
- Consumes: nothing new
- Produces: `perms.role_label(UserRole.front_desk)` now returns `"Receptionist"`. Every later task and every existing template that calls `role_label()` picks this up automatically — no template changes needed for the label itself.

- [ ] **Step 1: Make the change**

In `app/permissions.py`, the `ROLE_LABEL` dict currently reads:

```python
ROLE_LABEL = {
    "practitioner": "doctor",
    "front_desk": "front desk",
    "read_only": "read only",
}
```

Change the `front_desk` line:

```python
ROLE_LABEL = {
    "practitioner": "doctor",
    "front_desk": "Receptionist",
    "read_only": "read only",
}
```

- [ ] **Step 2: Verify manually**

Start the app (`python desktop.py` in a terminal, or `python -m uvicorn app.main:app --port 8000` if you'd rather hit it with `curl`). Sign in as any `front_desk` user (seed data has one — check `app/seed.py:184` for the seeded account, or use an existing one from `SELECT email FROM users WHERE role = 'front_desk';` via `docker exec ipmg-postgres psql -U ipmg -d ipmg_intake -c "SELECT email FROM users WHERE role='front_desk';"`).

Visit `/account` — the "Role" row should now read **Receptionist**, not "front desk". Visit `/permissions` — the column header for this role should also read **Receptionist**.

- [ ] **Step 3: Commit**

```bash
git add app/permissions.py
git commit -m "Relabel the front_desk role as Receptionist on screen"
```

> **DONE (2026-09-25), scope expanded beyond this task's original text:**
> the request was narrowed further mid-plan, before any task was formally
> executed, to strip `front_desk` down to only `CLIENTS_VIEW`, `CLIENTS_EDIT`,
> `SCHEDULE_EDIT` (no forms, submissions, billing, or documents), and to
> rename the seeded demo account from "Research Coordinator" to
> "Receptionist". This required a new `DOCUMENTS_VIEW` permission (split out
> of `CLIENTS_VIEW`, since Documents shared that permission with Clients/
> Calendar) applied across `app/permissions.py`, five routes in
> `app/main.py`, and the nav in `app/templates/base.html`. Full detail in the
> design spec's "Addendum" section
> (`docs/superpowers/specs/2026-09-25-receptionist-role-design.md`). This
> already-committed work supersedes this task's original narrower diff —
> nothing further to do here.

---

### Task 2: Add the appointment `channel` field and its migration

**Files:**
- Modify: `app/schedule.py:30-40` (add `Channel` enum, add `channel` column to `Appointment`)
- Create: `add_appointment_channel_column.py` (repo root)

**Interfaces:**
- Produces: `schedule.Channel` — a `str, enum.Enum` with members `in_person = "in_person"` and `online = "online"`. `schedule.Appointment.channel` — a `Mapped[Channel]` column, `default=Channel.in_person`. Later tasks (3, 4) read `a.channel` and `a.channel.value`, and Task 3 writes to it from a form field named `channel`.

- [ ] **Step 1: Add the enum and column**

In `app/schedule.py`, the file currently has:

```python
class Attendance(str, enum.Enum):
    scheduled = "scheduled"
    attended = "attended"
    cancelled = "cancelled"
    no_show = "no show"


KINDS = ["Screening visit", "Consultation", "Follow-up", "Trial visit",
         "Telehealth", "Assessment", "Other"]
```

Add a new enum right after `Attendance`, before `KINDS`:

```python
class Attendance(str, enum.Enum):
    scheduled = "scheduled"
    attended = "attended"
    cancelled = "cancelled"
    no_show = "no show"


class Channel(str, enum.Enum):
    """How the visit happens - separate from `kind`, which is what the visit
    is for. A Follow-up can be either; conflating the two would mean losing
    one fact to record the other."""
    in_person = "in_person"
    online = "online"


KINDS = ["Screening visit", "Consultation", "Follow-up", "Trial visit",
         "Telehealth", "Assessment", "Other"]
```

Then, in the `Appointment` class, the field block currently reads:

```python
    kind: Mapped[str] = mapped_column(String(64), default="Screening visit")
    location: Mapped[str] = mapped_column(String(128), default="")
    notes: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[Attendance] = mapped_column(Enum(Attendance),
                                               default=Attendance.scheduled)
```

Add the `channel` column right after `kind`:

```python
    kind: Mapped[str] = mapped_column(String(64), default="Screening visit")
    channel: Mapped[Channel] = mapped_column(Enum(Channel), default=Channel.in_person)
    location: Mapped[str] = mapped_column(String(128), default="")
    notes: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[Attendance] = mapped_column(Enum(Attendance),
                                               default=Attendance.scheduled)
```

- [ ] **Step 2: Write the migration script**

Create `add_appointment_channel_column.py` at the repo root (same directory as `add_provider_column.py`), copying its exact structure:

```python
"""Add appointments.channel - how the visit happens (in person or online),
separate from `kind`, which is what the visit is for.

    python add_appointment_channel_column.py                  # this machine's database
    TARGET_DATABASE_URL=... python add_appointment_channel_column.py   # the hosted one

SQLAlchemy's create_all() builds tables that do not exist; it never alters one
that does. A column added to a model after the table was created therefore has
to be added by hand, once per copy of the database.

Safe to run twice: it checks for the column first and says so rather than
failing, so nobody has to remember whether they have already run it.
"""

from __future__ import annotations

import os
import sys

import sqlalchemy as sa


def add_column(url: str, label: str) -> None:
    engine = sa.create_engine(
        url.replace("postgres://", "postgresql+psycopg://", 1)
           .replace("postgresql://", "postgresql+psycopg://", 1)
        if url.startswith("postgres") else url)

    with engine.begin() as conn:
        existing = {c["name"] for c in sa.inspect(conn).get_columns("appointments")}
        if "channel" in existing:
            print(f"  {label}: channel already there, nothing to do")
            return
        conn.execute(sa.text(
            "CREATE TYPE channel AS ENUM ('in_person', 'online')"))
        conn.execute(sa.text(
            "ALTER TABLE appointments ADD COLUMN channel channel "
            "NOT NULL DEFAULT 'in_person'"))
        print(f"  {label}: channel added")


if __name__ == "__main__":
    target = (os.environ.get("TARGET_DATABASE_URL") or "").strip()
    if target:
        add_column(target, "hosted database")
    else:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from app.models import engine
        add_column(str(engine.url.render_as_string(hide_password=False)),
                   "local database")
```

Note the `CREATE TYPE channel AS ENUM (...)` step: unlike `provider_id` (a plain `INTEGER`), this is a Postgres enum column, so the enum type itself has to exist before the `ALTER TABLE` can reference it. If the script is run twice, the early-return on the column check means the second run never reaches `CREATE TYPE` either, so it stays safe to re-run.

- [ ] **Step 3: Run the migration against the local database**

Confirm `ipmg-postgres` is up first:

```bash
docker ps --filter name=ipmg-postgres --format "{{.Names}}: {{.Status}}"
```

Then run the script:

```bash
cd "C:\Users\admin\Desktop\IPMG intakeq"
python add_appointment_channel_column.py
```

Expected output: `local database: channel added`.

- [ ] **Step 4: Verify idempotency**

Run it again:

```bash
python add_appointment_channel_column.py
```

Expected output: `local database: channel already there, nothing to do`. If it errors instead (e.g. on `CREATE TYPE channel already exists`), the early-return check is wrong — fix it before moving on.

- [ ] **Step 5: Verify the column directly**

```bash
docker exec ipmg-postgres psql -U ipmg -d ipmg_intake -c "\d appointments"
```

Confirm a `channel` column appears with type `channel` (the enum) and default `'in_person'::channel`.

- [ ] **Step 6: Commit**

```bash
git add app/schedule.py add_appointment_channel_column.py
git commit -m "Add appointments.channel: online vs in-person, separate from visit kind"
```

---

### Task 3: Surface `channel` on the booking form and calendar

**Files:**
- Modify: `app/main.py:2080-2102` (`calendar_book` route)
- Modify: `app/templates/calendar.html` (booking form + day-cell tooltip)

**Interfaces:**
- Consumes: `schedule.Channel` from Task 2.
- Produces: booking a new appointment via `POST /calendar/book` now sets `channel` from the submitted form field (defaulting to `in_person` if blank or invalid); `Appointment.channel` is populated on every new booking from here on.

- [ ] **Step 1: Accept and validate the field in the route**

In `app/main.py`, `calendar_book` currently reads:

```python
@app.post("/calendar/book")
def calendar_book(request: Request, client_id: int = F(...), on_day: str = F(""),
                  at_time: str = F(""), minutes: str = F("30"),
                  kind: str = F("Screening visit"), provider_id: str = F(""),
                  location: str = F(""), notes: str = F(""),
                  db: Session = Depends(get_db),
                  _=Depends(needs(perms.SCHEDULE_EDIT))):
    day = _date(on_day)
    if not day:
        request.session["calendar_error"] = "Pick a date for the appointment."
        return RedirectResponse("/calendar", status_code=303)
    client = get_or_404(db, Client, client_id)
    booking = schedule.Appointment(
        client_id=client.id, on_day=day, at_time=at_time.strip()[:5],
        minutes=_int_or_none(minutes) or 30, kind=kind.strip() or "Screening visit",
        provider_id=_int_or_none(provider_id), location=location.strip(),
        notes=notes.strip())
    db.add(booking)
    cache.drop_nav()
    log(db, f"Appointment booked ({day:%d %b})", "client", client.id)
    db.commit()
    return RedirectResponse(f"/calendar?year={day.year}&month={day.month}",
                            status_code=303)
```

Change it to:

```python
@app.post("/calendar/book")
def calendar_book(request: Request, client_id: int = F(...), on_day: str = F(""),
                  at_time: str = F(""), minutes: str = F("30"),
                  kind: str = F("Screening visit"), provider_id: str = F(""),
                  channel: str = F("in_person"),
                  location: str = F(""), notes: str = F(""),
                  db: Session = Depends(get_db),
                  _=Depends(needs(perms.SCHEDULE_EDIT))):
    day = _date(on_day)
    if not day:
        request.session["calendar_error"] = "Pick a date for the appointment."
        return RedirectResponse("/calendar", status_code=303)
    client = get_or_404(db, Client, client_id)
    try:
        appt_channel = schedule.Channel(channel)
    except ValueError:
        appt_channel = schedule.Channel.in_person
    booking = schedule.Appointment(
        client_id=client.id, on_day=day, at_time=at_time.strip()[:5],
        minutes=_int_or_none(minutes) or 30, kind=kind.strip() or "Screening visit",
        provider_id=_int_or_none(provider_id), channel=appt_channel,
        location=location.strip(), notes=notes.strip())
    db.add(booking)
    cache.drop_nav()
    log(db, f"Appointment booked ({day:%d %b})", "client", client.id)
    db.commit()
    return RedirectResponse(f"/calendar?year={day.year}&month={day.month}",
                            status_code=303)
```

- [ ] **Step 2: Add the selector to the booking form**

In `app/templates/calendar.html`, the booking form currently has (per the earlier grep, around line 80-88):

```html
          <label>Kind
            <select name="kind">
              {% for k in kinds %}<option>{{ k }}</option>{% endfor %}
            </select></label>
          <label>Provider
            <select name="provider_id">
              <option value="">Unassigned</option>
              {% for p in providers %}<option value="{{ p.id }}">{{ p.name }}</option>{% endfor %}
            </select></label>
```

Add a `Channel` selector right after `Kind`:

```html
          <label>Kind
            <select name="kind">
              {% for k in kinds %}<option>{{ k }}</option>{% endfor %}
            </select></label>
          <label>Channel
            <select name="channel">
              <option value="in_person">In person</option>
              <option value="online">Online</option>
            </select></label>
          <label>Provider
            <select name="provider_id">
              <option value="">Unassigned</option>
              {% for p in providers %}<option value="{{ p.id }}">{{ p.name }}</option>{% endfor %}
            </select></label>
```

- [ ] **Step 3: Show it on the day cell tooltip**

Still in `app/templates/calendar.html`, the day-cell link currently reads (per the earlier grep, around line 53-55):

```html
            <a ... href="/clients/{{ a.client_id }}"
               title="{{ a.kind }}{{ ' · ' ~ a.location if a.location }}{{ ' · ' ~ a.notes if a.notes }}">
```

Add the channel into the same tooltip, right after `a.kind`:

```html
            <a ... href="/clients/{{ a.client_id }}"
               title="{{ a.kind }} ({{ 'online' if a.channel.value == 'online' else 'in person' }}){{ ' · ' ~ a.location if a.location }}{{ ' · ' ~ a.notes if a.notes }}">
```

- [ ] **Step 4: Verify manually**

Restart the app if it's running (`Ctrl+C`, then `python desktop.py` again, so the reloaded `app/main.py`/`app/schedule.py` take effect — this app is not run with `--reload`).

1. Sign in as a `front_desk` (Receptionist) or `admin` user.
2. Go to `/calendar`, book a new appointment for any patient, picking **Online** in the new Channel field.
3. After the redirect back to `/calendar`, hover the new appointment's entry on the day cell — the tooltip should show `(online)`.
4. Confirm directly in the database:
   ```bash
   docker exec ipmg-postgres psql -U ipmg -d ipmg_intake -c "SELECT id, kind, channel FROM appointments ORDER BY id DESC LIMIT 1;"
   ```
   The newest row's `channel` should read `online`.
5. Book a second appointment leaving Channel at its default — confirm that row's `channel` is `in_person`.

- [ ] **Step 5: Commit**

```bash
git add app/main.py app/templates/calendar.html
git commit -m "Let staff set an appointment's channel (online/in-person) when booking"
```

---

### Task 4: `/reception` route with the today's-patients section

**Files:**
- Modify: `app/main.py` (new route, placed near `my_patients` at line 2372 for locality — add it directly above `@app.get("/my-patients", ...)`)
- Create: `app/templates/reception.html`

**Interfaces:**
- Consumes: `schedule.Appointment`, `schedule.Channel` (Task 2), `perms.CLIENTS_VIEW`, `needs()`, `ctx()`, `render()`, `log()` — all already defined in `app/main.py`.
- Produces: `GET /reception`, gated by `perms.CLIENTS_VIEW`, rendering `reception.html` with context keys `today_appts` (list of `schedule.Appointment`, today only, ordered by `at_time`) and `nav="reception"`. Task 5 and Task 6 add more context keys to this same route and more sections to this same template — they do not replace what this task produces.

- [ ] **Step 1: Add the route**

In `app/main.py`, directly above the existing `@app.get("/my-patients", ...)` (line 2372), add:

```python
@app.get("/reception", response_class=HTMLResponse)
def reception(request: Request, db: Session = Depends(get_db),
             _=Depends(needs(perms.CLIENTS_VIEW))):
    """The receptionist's own home: who is coming in today, a fast way to
    register somebody new, and each doctor's patient list for routing.

    Same idea as /my-patients being a clinician's home instead of the
    practice-wide dashboard - the receptionist's first question is "who is
    walking in today", not "how is the practice doing this week"."""
    today = date.today()
    today_appts = (db.query(schedule.Appointment)
                   .filter(schedule.Appointment.on_day == today)
                   .options(joinedload(schedule.Appointment.client),
                            joinedload(schedule.Appointment.provider))
                   .order_by(schedule.Appointment.at_time).all())

    log(db, f"Reception desk viewed ({len(today_appts)} today)", "clients",
        len(today_appts))
    db.commit()
    return render("reception.html", ctx(
        request, db, nav="reception", today_appts=today_appts))
```

- [ ] **Step 2: Create the template**

Create `app/templates/reception.html`:

```html
{% extends "base.html" %}
{% block title %}Reception{% endblock %}
{% block chrome %}
<div class="crumb">
  <div>Hello, {{ user.name if user else 'there' }}!</div>
</div>
{% endblock %}

{% block content %}

<div class="panel" style="margin-bottom:24px">
  <h2>&#128197; Today's patients</h2>
  <div class="body" style="padding:0 22px 18px">
    <table>
      <tr><th>Time</th><th>Name</th><th>Doctor</th><th>Channel</th><th>Status</th></tr>
      {% for a in today_appts %}
      <tr>
        <td>{{ a.at_time or '—' }}</td>
        <td><a href="/clients/{{ a.client_id }}">{{ a.client.name if a.client else '—' }}</a></td>
        <td>{{ a.provider.name if a.provider else '—' }}</td>
        <td>
          {% if a.channel.value == 'online' %}<span style="color:var(--accent);font-weight:600">Online</span>
          {% else %}<span class="hint">In person</span>{% endif %}
        </td>
        <td>
          {% if a.status.value == 'attended' %}<span style="color:var(--good);font-weight:600">attended</span>
          {% elif a.status.value == 'cancelled' %}<span class="hint">cancelled</span>
          {% elif a.status.value == 'no show' %}<span style="color:var(--bad);font-weight:600">no show</span>
          {% else %}<span style="color:var(--warn);font-weight:600">scheduled</span>{% endif %}
        </td>
      </tr>
      {% else %}
      <tr><td colspan="5" class="hint">Nobody on the calendar for today.</td></tr>
      {% endfor %}
    </table>
  </div>
</div>

{% endblock %}
```

- [ ] **Step 3: Verify manually**

Restart the app. Sign in as the `front_desk` user directly, then navigate to `/reception` in the browser (there's no nav link yet — that's Task 7 — so type the URL directly).

Confirm: the page loads without error, shows a "Today's patients" table. If there are no appointments today, confirm it shows "Nobody on the calendar for today." If there are (from Task 3's manual test), confirm the row shows the right time, patient name, doctor, and an **Online** or **In person** badge matching what was booked.

Also confirm a `practitioner` or `admin` user can still load `/my-patients` and `/` as before — this task didn't touch either.

- [ ] **Step 4: Commit**

```bash
git add app/main.py app/templates/reception.html
git commit -m "Add /reception showing today's patients"
```

---

### Task 5: Add the by-doctor case list to `/reception`

**Files:**
- Modify: `app/main.py` (`reception` route from Task 4)
- Modify: `app/templates/reception.html` (from Task 4)

**Interfaces:**
- Consumes: `User`, `UserRole` (already imported in `app/main.py`), `func` (already imported), `Client` (already imported).
- Produces: adds `doctors` (list of `User`, role `practitioner`, active) and `doctor_counts` (`dict[int, int]`, doctor id → active patient count) to the `reception` route's context. `unassigned_count` (`int`) is also added.

- [ ] **Step 1: Extend the route**

In `app/main.py`, change the `reception` route from Task 4 to also gather the by-doctor counts:

```python
@app.get("/reception", response_class=HTMLResponse)
def reception(request: Request, db: Session = Depends(get_db),
             _=Depends(needs(perms.CLIENTS_VIEW))):
    """The receptionist's own home: who is coming in today, a fast way to
    register somebody new, and each doctor's patient list for routing.

    Same idea as /my-patients being a clinician's home instead of the
    practice-wide dashboard - the receptionist's first question is "who is
    walking in today", not "how is the practice doing this week"."""
    today = date.today()
    today_appts = (db.query(schedule.Appointment)
                   .filter(schedule.Appointment.on_day == today)
                   .options(joinedload(schedule.Appointment.client),
                            joinedload(schedule.Appointment.provider))
                   .order_by(schedule.Appointment.at_time).all())

    doctors = (db.query(User)
               .filter(User.is_active.is_(True), User.role == UserRole.practitioner)
               .order_by(User.name).all())
    counts = dict(db.query(Client.provider_id, func.count(Client.id))
                  .filter(Client.archived.is_(False), Client.provider_id.isnot(None))
                  .group_by(Client.provider_id).all())
    unassigned_count = (db.query(Client)
                        .filter_by(archived=False, provider_id=None).count())

    log(db, f"Reception desk viewed ({len(today_appts)} today)", "clients",
        len(today_appts))
    db.commit()
    return render("reception.html", ctx(
        request, db, nav="reception", today_appts=today_appts,
        doctors=doctors, doctor_counts=counts, unassigned_count=unassigned_count))
```

- [ ] **Step 2: Add the section to the template**

In `app/templates/reception.html`, add a new panel after the "Today's patients" panel (still inside `{% block content %}`):

```html
<div class="panel" style="margin-bottom:24px">
  <h2>&#9878; Cases by doctor</h2>
  <div class="body" style="padding:0 22px 18px">
    <table>
      <tr><th>Doctor</th><th>Patients</th><th></th></tr>
      {% for d in doctors %}
      <tr>
        <td>{{ d.name }}</td>
        <td>{{ doctor_counts.get(d.id, 0) }}</td>
        <td><a class="btn btn-sm" href="/my-patients?provider={{ d.id }}">View list &rarr;</a></td>
      </tr>
      {% else %}
      <tr><td colspan="3" class="hint">No doctors on staff yet.</td></tr>
      {% endfor %}
      <tr>
        <td>Unassigned</td>
        <td>{{ unassigned_count }}</td>
        <td><a class="btn btn-sm" href="/my-patients?provider=-2">View list &rarr;</a></td>
      </tr>
    </table>
  </div>
</div>
```

- [ ] **Step 3: Verify manually**

Restart the app, reload `/reception` as the `front_desk` user. Confirm: a "Cases by doctor" table appears below "Today's patients", listing each active practitioner with a patient count, plus an "Unassigned" row. Confirm the counts match reality:

```bash
docker exec ipmg-postgres psql -U ipmg -d ipmg_intake -c "SELECT provider_id, COUNT(*) FROM clients WHERE archived = false GROUP BY provider_id;"
```

Click "View list" next to one doctor — confirm it lands on `/my-patients?provider={that doctor's id}` and shows only that doctor's patients (this route already existed and already worked for `front_desk` given `CLIENTS_VIEW` — this step is confirming the link, not new route behavior).

- [ ] **Step 4: Commit**

```bash
git add app/main.py app/templates/reception.html
git commit -m "Add the by-doctor case list to /reception"
```

---

### Task 6: Add the register-new-patient form to `/reception`

**Files:**
- Modify: `app/templates/reception.html` (from Tasks 4-5)

**Interfaces:**
- Consumes: the existing `POST /clients/new` route (`app/main.py:1316`, unchanged) and its exact field names: `first_name`, `last_name`, `dob`, `email`, `phone`, `hospital_id`, `allow_email`, `allow_sms`.
- Produces: nothing new for later tasks — this is the last content section on the page.

- [ ] **Step 1: Add the form to the template**

In `app/templates/reception.html`, add this panel — gated the same way the equivalent panel is gated on `/clients` (`app/templates/clients.html:20`) — after the "Cases by doctor" panel:

```html
{% if can(user, P.CLIENTS_EDIT) %}
<div class="panel" style="margin-bottom:24px">
  <h2>&#10010; Register a new patient</h2>
  <div class="body">
    <form method="post" action="/clients/new"
          style="display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));
                 gap:13px;align-items:end">
      <label>First name<input type="text" name="first_name" required></label>
      <label>Last name<input type="text" name="last_name"></label>
      <label>Date of birth<input type="date" name="dob"></label>
      <label>Email<input type="email" name="email"></label>
      <label>Phone<input type="text" name="phone" placeholder="(909) 555-0100"></label>
      <label>Hospital ID<input type="text" name="hospital_id" inputmode="numeric"
             placeholder="e.g. 1097"></label>

      <div style="grid-column:1 / -1;display:flex;gap:22px;flex-wrap:wrap;
                  border-top:1px solid var(--rule);padding-top:13px;margin-top:4px">
        <label style="display:flex;align-items:center;gap:8px">
          <input type="checkbox" name="allow_email" value="1">
          Consents to email contact</label>
        <label style="display:flex;align-items:center;gap:8px">
          <input type="checkbox" name="allow_sms" value="1">
          Consents to SMS contact</label>
        <button class="btn btn-green" style="margin-left:auto">Add patient</button>
      </div>
    </form>
  </div>
</div>
{% endif %}
```

This posts to the same `/clients/new` endpoint the existing `/clients` page uses, unchanged — on success it redirects to the new patient's chart at `/clients/{id}`, exactly as it does from `/clients` today.

- [ ] **Step 2: Verify manually**

Restart the app, reload `/reception` as the `front_desk` user. Confirm the "Register a new patient" panel appears (it's gated on `CLIENTS_EDIT`, which `front_desk` has, so it should show). Fill in at least a first name and submit.

Confirm: you land on the new patient's chart page (`/clients/{id}`), and:

```bash
docker exec ipmg-postgres psql -U ipmg -d ipmg_intake -c "SELECT id, first_name, last_name FROM clients ORDER BY id DESC LIMIT 1;"
```

shows the patient you just added.

- [ ] **Step 3: Commit**

```bash
git add app/templates/reception.html
git commit -m "Add patient registration form to /reception"
```

---

### Task 7: Route receptionists to `/reception`, and wire up navigation

**Files:**
- Modify: `app/main.py:286-294` (`home()`)
- Modify: `app/templates/base.html:29-32` (nav)

**Interfaces:**
- Consumes: `UserRole.front_desk` (already imported), the `/reception` route (Task 4).
- Produces: a `front_desk` user visiting `/` is redirected to `/reception`. Nav shows a "Reception" link to `front_desk` users, and the existing "My Patients" link becomes visible to `front_desk` too (previously only `practitioner`-visible via `CLINICAL_EDIT`).

- [ ] **Step 1: Add the redirect**

In `app/main.py`, `home()` currently starts:

```python
@app.get("/", response_class=HTMLResponse)
def home(request: Request, db: Session = Depends(get_db)):
    #  A clinician's home is their caseload, not the practice's activity feed.
    #  The dashboard below answers "what is happening across the practice",
    #  which is an administrator's question; a doctor signing in wants to know
    #  who is waiting on them. Same app, different first screen.
    me = auth.current_user(request, db)
    if me and me.role == UserRole.practitioner:
        return RedirectResponse("/my-patients", status_code=303)
```

Add a second redirect directly below the practitioner one:

```python
@app.get("/", response_class=HTMLResponse)
def home(request: Request, db: Session = Depends(get_db)):
    #  A clinician's home is their caseload, not the practice's activity feed.
    #  The dashboard below answers "what is happening across the practice",
    #  which is an administrator's question; a doctor signing in wants to know
    #  who is waiting on them. Same app, different first screen.
    me = auth.current_user(request, db)
    if me and me.role == UserRole.practitioner:
        return RedirectResponse("/my-patients", status_code=303)
    if me and me.role == UserRole.front_desk:
        return RedirectResponse("/reception", status_code=303)
```

- [ ] **Step 2: Add nav links**

In `app/templates/base.html`, the relevant block currently reads:

```html
    {% if can(user, P.CLINICAL_EDIT) %}
    <a href="/my-patients" class="{{ 'on' if nav=='mypatients' }}"><i>✚</i>My Patients</a>{% endif %}
    {% if can(user, P.CLIENTS_VIEW) %}
    <a href="/clients" class="{{ 'on' if nav=='clients' }}"><i>☺</i>All Patients</a>{% endif %}
```

Change it to add a `/reception` link and make the `/my-patients` link's visibility also include `front_desk`:

```html
    {% if can(user, P.CLINICAL_EDIT) or (user and user.role.value == 'front_desk') %}
    <a href="/my-patients" class="{{ 'on' if nav=='mypatients' }}"><i>✚</i>My Patients</a>{% endif %}
    {% if user and user.role.value == 'front_desk' %}
    <a href="/reception" class="{{ 'on' if nav=='reception' }}"><i>&#127973;</i>Reception</a>{% endif %}
    {% if can(user, P.CLIENTS_VIEW) %}
    <a href="/clients" class="{{ 'on' if nav=='clients' }}"><i>☺</i>All Patients</a>{% endif %}
```

(A direct `user.role.value == 'front_desk'` check, not a new permission, because this is about which role a person holds, not a capability grant — `perms.can()` is reserved for actual permission checks per this file's own stated convention in `app/permissions.py`'s module docstring. Nothing about who can view `/reception` changes: the route itself is still gated on `CLIENTS_VIEW` via `needs()`, this only controls whether the link is drawn.)

- [ ] **Step 3: Verify manually**

Restart the app.

1. Sign in as the `front_desk` user. Confirm you land directly on `/reception`, not the admin dashboard.
2. Confirm the left nav now shows both **Reception** and **My Patients** links, and clicking each lands on the right page with the correct link highlighted (`on` class).
3. Sign out, sign in as a `practitioner`. Confirm they still land on `/my-patients` as before, and do **not** see a "Reception" link.
4. Sign out, sign in as `owner` or `admin`. Confirm they still land on the general dashboard (`/`) as before, unaffected by this change.

- [ ] **Step 4: Commit**

```bash
git add app/main.py app/templates/base.html
git commit -m "Route Receptionist users to /reception and add nav links"
```

---

## Final Verification (matches the design spec's Testing section)

After all 7 tasks, do one end-to-end pass as a fresh check:

- [ ] `python add_appointment_channel_column.py` run a third time (from a different terminal state) still prints "already there, nothing to do" — confirms the migration is genuinely idempotent, not just idempotent in the session that wrote it.
- [ ] A `front_desk` user's full login-to-action path works in one sitting: sign in → land on `/reception` → see today's appointments with correct channel badges → register a new patient from the embedded form → land on that patient's chart → go back to `/reception` → click through a doctor's "View list" link → land on the correctly filtered `/my-patients` view.
- [ ] `practitioner`, `admin`, `owner`, and `read_only` users each still log in to their existing, unchanged landing pages and retain their existing nav links — run through each once.
- [ ] `docker exec ipmg-postgres psql -U ipmg -d ipmg_intake -c "\d appointments"` still shows exactly one new column (`channel`) versus before this work — no accidental extra columns from a re-run gone wrong.
