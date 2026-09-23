"""Pulling the practice's own IntakeQ account into this app.

Two things arrive: the people, and the paperwork they signed.

**The people** become Client rows - the same rows staff already edit, so an
imported patient is not a second-class citizen living in a side table.

**The paperwork** stays a document. IntakeQ's answers could in principle be
re-modelled into Question and Answer rows, but a completed intake is a signed
legal artefact, and re-modelling one is lossy in the direction that matters: the
PDF is what a court would ask for. So the import keeps the PDF and lifts only
the demographics out of it.

Three decisions worth stating, because each one cost a design argument:

**PDFs are fetched lazily.** The obvious import downloads every PDF as it goes,
which costs one request per intake - four days of backfill at IntakeQ's 500/day
on a two thousand patient practice. Metadata comes back a hundred rows to a
request, so the backfill pulls only that and fetches a PDF the first time
somebody opens it. A practice where nobody ever opens an old intake pays
nothing, which is most practices.

**Imported data loses to typed data.** A sync that overwrites is a sync that
silently reverts the correction a receptionist made this morning. So an import
fills blanks and never replaces a value that is already there.

**Consent is not imported.** IntakeQ may hold contact preferences, but consent
recorded there was given to that system in that wording. Copying a boolean
across and then emailing somebody on the strength of it is exactly the reasoning
this app refuses everywhere else.

The runner is deliberately built to work in *slices* rather than as one long
job. A serverless request is measured in seconds and a backfill is not, so each
call does what it can inside a budget and writes down where it got to. The same
code then runs identically on the hosted copy and the desktop one, and a run
that is interrupted resumes instead of restarting.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy import (DateTime, ForeignKey, Integer, LargeBinary, String,
                        Text)
from sqlalchemy.orm import Mapped, mapped_column

from .models import Base

API_BASE = "https://intakeq.com/api/v1"

#  IntakeQ's published limits for a standard PracticeQ subscription. Both are
#  enforced here rather than discovered through 429s: a rate limit you find out
#  about by being refused is one you have already half-broken, and a backfill
#  that trips the daily cap at record 400 leaves somebody guessing why.
PER_MINUTE = 10
PER_DAY = 500
PAGE_SIZE = 100                      # the maximum IntakeQ will return

#  How long a single slice may run. Comfortably inside a serverless function's
#  ceiling with room for the redirect and the page render afterwards.
SLICE_SECONDS = 20
SLICE_REQUESTS = 8


# --- provenance -----------------------------------------------------------
#
# Mirrors the vocabulary clinical.py already uses for its own rows. A fourth
# value rather than reusing SYNCED, because "from Tebra" and "from IntakeQ" are
# different answers to "may I overwrite this?" and collapsing them would make
# the one question this column exists to answer unanswerable.

TYPED = "typed"
INTAKEQ = "intakeq"

SOURCE_LABEL = {
    "": "entered here",
    TYPED: "entered here",
    INTAKEQ: "from IntakeQ",
}


# --- tables ---------------------------------------------------------------


class ImportRun(Base):
    """One backfill, and where it got to.

    A run is a checkpoint, not a log. It exists so that a job stopped by a
    daily quota, a serverless timeout or somebody closing the laptop can be
    picked up at the row it reached rather than from the beginning - and so
    that "is it still going?" has an answer that is not a guess.
    """

    __tablename__ = "import_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    #  running - work remains and the quota allows it
    #  paused  - the daily cap is spent, or a person pressed stop
    #  done    - every page of both stages was read
    #  failed  - something the runner could not interpret
    status: Mapped[str] = mapped_column(String(16), default="running")

    #  Which half of the import is in progress: clients, then intakes.
    stage: Mapped[str] = mapped_column(String(16), default="clients")
    page: Mapped[int] = mapped_column(Integer, default=1)

    clients_seen: Mapped[int] = mapped_column(Integer, default=0)
    clients_new: Mapped[int] = mapped_column(Integer, default=0)
    clients_filled: Mapped[int] = mapped_column(Integer, default=0)
    intakes_seen: Mapped[int] = mapped_column(Integer, default=0)
    intakes_new: Mapped[int] = mapped_column(Integer, default=0)

    #  Requests spent today, and the day they were spent on. Stored rather than
    #  counted from a log because the daily cap is IntakeQ's, not ours, and it
    #  has to survive a restart to mean anything.
    requests_used: Mapped[int] = mapped_column(Integer, default=0)
    quota_day: Mapped[str] = mapped_column(String(10), default="")

    #  The high-water mark for incremental syncs after the first full pass.
    updated_since: Mapped[str] = mapped_column(String(10), default="")

    message: Mapped[str] = mapped_column(Text, default="")
    failures: Mapped[str] = mapped_column(Text, default="")

    @property
    def is_active(self) -> bool:
        return self.status == "running"

    @property
    def quota_left(self) -> int:
        if self.quota_day != date.today().isoformat():
            return PER_DAY
        return max(0, PER_DAY - self.requests_used)

    def note_failure(self, line: str) -> None:
        """Record one bad record without abandoning the run.

        A single malformed intake in two thousand is a fact to look at later,
        not a reason to stop - but it must not vanish either, or the import
        reports success over a hole.
        """
        kept = [l for l in (self.failures or "").splitlines() if l][-199:]
        kept.append(f"{datetime.utcnow():%d %b %H:%M}  {line}")
        self.failures = "\n".join(kept)


class ImportedIntake(Base):
    """A completed IntakeQ questionnaire, kept as the document it is.

    The PDF column is nullable and usually empty: metadata arrives in bulk, the
    document is fetched the first time a human asks for it. `pdf_error` is
    separate from a null `pdf` on purpose - "nobody has opened this yet" and
    "we tried and IntakeQ refused" look identical otherwise, and only one of
    them is a problem.
    """

    __tablename__ = "imported_intakes"
    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int | None] = mapped_column(
        ForeignKey("clients.id"), nullable=True, index=True)

    intakeq_intake_id: Mapped[str] = mapped_column(
        String(64), unique=True, index=True)
    questionnaire_name: Mapped[str] = mapped_column(String(255), default="")
    practitioner_name: Mapped[str] = mapped_column(String(255), default="")
    client_name: Mapped[str] = mapped_column(String(255), default="")
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="")

    pdf: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    pdf_fetched_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    pdf_error: Mapped[str] = mapped_column(String(255), default="")

    @property
    def have_pdf(self) -> bool:
        return self.pdf is not None and len(self.pdf) > 0


# --- talking to IntakeQ ---------------------------------------------------


class QuotaSpent(Exception):
    """The daily allowance is gone. Not an error - a reason to come back."""


class Refused(Exception):
    """IntakeQ answered, and the answer was no."""


@dataclass
class Budget:
    """What one slice is allowed to spend."""

    requests: int = SLICE_REQUESTS
    seconds: float = SLICE_SECONDS
    started: float = 0.0

    def __post_init__(self) -> None:
        self.started = time.monotonic()

    @property
    def spent(self) -> bool:
        return (self.requests <= 0
                or time.monotonic() - self.started >= self.seconds)


class Api:
    """A thin IntakeQ client that counts what it spends.

    Named Api rather than Client because this module also deals in Client rows,
    and two meanings of one word in one file is a bug waiting for a tired
    afternoon.

    Every call goes through `_get`, which is also the only place that touches
    the run's quota counters. Keeping the accounting inseparable from the
    request is deliberate: an un-counted request is how a cap gets broken.
    """

    def __init__(self, key: str, run: ImportRun, budget: Budget,
                 simulated: bool = False):
        self.key = key
        self.run = run
        self.budget = budget
        self.simulated = simulated
        self._last_call = 0.0

    def _spend(self) -> None:
        today = date.today().isoformat()
        if self.run.quota_day != today:
            self.run.quota_day = today
            self.run.requests_used = 0
        if self.run.requests_used >= PER_DAY:
            raise QuotaSpent(
                f"IntakeQ's daily allowance of {PER_DAY} requests is spent. "
                f"The import resumes tomorrow from where it stopped.")

        #  Pace to stay under the per-minute limit. A sleep inside a request is
        #  normally a smell, but the alternative is a 429 and a retry that costs
        #  the same wall-clock and a request from the daily budget as well.
        gap = 60.0 / PER_MINUTE
        wait = gap - (time.monotonic() - self._last_call)
        if self._last_call and wait > 0:
            time.sleep(wait)

        self._last_call = time.monotonic()
        self.run.requests_used += 1
        self.budget.requests -= 1

    def _open(self, path: str, params: dict | None = None, limit: int = 2_000_000):
        self._spend()
        url = f"{API_BASE}{path}"
        if params:
            clean = {k: v for k, v in params.items() if v not in ("", None)}
            if clean:
                url += "?" + urllib.parse.urlencode(clean)
        #  The simulation seam. Everything above this line is the production
        #  path - the URL, the paging parameters, the quota already spent - so
        #  a demonstration exercises the real client and not a stand-in.
        if self.simulated:
            from . import simulation

            return simulation.intakeq_response(url)

        request = urllib.request.Request(
            url, headers={"X-Auth-Key": self.key, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.read(limit)
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise Refused("IntakeQ rejected the API key. Check "
                              "INTAKEQ_API_KEY on the Connections screen.") from exc
            if exc.code == 429:
                raise QuotaSpent("IntakeQ is rate limiting us. The import will "
                                 "pick up where it stopped.") from exc
            raise Refused(f"IntakeQ replied HTTP {exc.code}.") from exc

    def json(self, path: str, params: dict | None = None):
        raw = self._open(path, params)
        try:
            return json.loads(raw or b"[]")
        except ValueError as exc:
            raise Refused(f"IntakeQ sent something that is not JSON "
                          f"({len(raw)} bytes).") from exc

    def pdf(self, intake_id: str) -> bytes:
        #  25 MB ceiling. An intake PDF is tens of kilobytes; anything near this
        #  is a fault, and reading it into memory unbounded is how one bad
        #  record takes the process with it.
        return self._open(f"/intakes/{urllib.parse.quote(intake_id)}/pdf",
                          limit=25_000_000)


def simulated(db) -> bool:
    from . import simulation

    return simulation.enabled(db)


def api_key(db) -> str:
    #  Imported inside the function: credentials imports models, models registers
    #  this module at the end of its own import, and a module-level import here
    #  would close that loop while models is still half-built.
    from . import credentials

    return credentials.resolve(db, "INTAKEQ_API_KEY")


# --- reading what IntakeQ sends -------------------------------------------
#
# Pure functions from here to the end of the section. No database, no network -
# so the mapping can be tested against a recorded payload, which is the only way
# to be sure about a field you have seen once.


def _text(payload: dict, *names: str) -> str:
    """The first of several spellings that carries a value.

    IntakeQ's objects are not uniform - a summary row and a full profile name
    the same thing differently - and guessing wrong yields a blank field rather
    than an error, which is the hardest kind of bug to notice.
    """
    for name in names:
        value = payload.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, (int, float)) and value:
            return str(value)
    return ""


def read_date(value) -> date | None:
    """A date from any of the four shapes IntakeQ uses for one."""
    if value in ("", None):
        return None
    if isinstance(value, (int, float)):
        #  Milliseconds since the epoch, which is what the JSON usually holds.
        #  Anything below this threshold is implausible as milliseconds and is
        #  read as seconds instead.
        seconds = float(value) / 1000.0 if abs(value) > 10_000_000_000 else float(value)
        try:
            #  Deliberately NOT utcfromtimestamp: on Windows it raises for any
            #  negative value, so every patient born before 1970 would silently
            #  arrive with no date of birth. That is the one field the archive
            #  matcher leans on hardest, and a blank one is indistinguishable
            #  from a patient who never gave it.
            return (datetime(1970, 1, 1) + timedelta(seconds=seconds)).date()
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()
    if not text:
        return None
    #  A bare "/Date(1234567890000)/" still turns up in older records.
    if text.startswith("/Date(") and ")" in text:
        inner = text[6:text.index(")")].split("+")[0].split("-")[0]
        if inner.lstrip("-").isdigit():
            return read_date(int(inner))
    for shape in ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text[:len(shape) + 4], shape).date()
        except ValueError:
            continue
    return None


def read_moment(value) -> datetime | None:
    day = read_date(value)
    return datetime(day.year, day.month, day.day) if day else None


def split_name(full: str) -> tuple[str, str]:
    """First and last, from a name written either way round.

    A comma means the registry convention - "Alvarez, Maria" is Maria Alvarez,
    not somebody called Alvarez. Reading it left to right regardless would file
    a tenth of a practice under their own surnames.
    """
    text = (full or "").strip()
    if not text:
        return "", ""
    if "," in text:
        last, _, first = text.partition(",")
        if last.strip() and first.strip():
            return first.strip(), last.strip()
    parts = text.split()
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def map_client(payload: dict) -> dict:
    """An IntakeQ client object as this app's own field names.

    Consent is absent from the result on purpose - see the module docstring.
    """
    first = _text(payload, "FirstName")
    last = _text(payload, "LastName")
    if not (first or last):
        first, last = split_name(_text(payload, "Name"))

    state = _text(payload, "State", "StateShort")
    return {
        "intakeq_client_id": _text(payload, "ClientId", "Id"),
        "first_name": first[:120],
        "last_name": last[:120],
        "email": _text(payload, "Email").lower()[:255],
        "phone": _text(payload, "Phone", "MobilePhone", "HomePhone")[:64],
        "dob": read_date(payload.get("DateOfBirth")),
        "city": _text(payload, "City")[:120],
        "state": state[:64],
        "postal_code": _text(payload, "PostalCode", "Zip", "ZipCode")[:16],
    }


def map_intake(payload: dict) -> dict:
    """One row of /intakes/summary as this app's own field names."""
    return {
        "intakeq_intake_id": _text(payload, "Id", "IntakeId"),
        "intakeq_client_id": _text(payload, "ClientId"),
        "client_name": _text(payload, "ClientName", "ClientNameOrEmail")[:255],
        "questionnaire_name": _text(payload, "QuestionnaireName", "Name")[:255],
        "practitioner_name": _text(payload, "PractitionerName",
                                   "Practitioner")[:255],
        "submitted_at": read_moment(payload.get("DateSubmitted")
                                    or payload.get("DateCreated")),
        "status": _text(payload, "Status")[:32],
    }


#  Which Client columns an import is allowed to write into, and never over.
FILLABLE = ("first_name", "last_name", "email", "phone", "dob",
            "city", "state", "postal_code")


def fill_blanks(client, mapped: dict) -> list[str]:
    """Copy across only the fields this app has nothing for.

    Returns the names it filled, so the run can report honestly on whether an
    import actually did anything rather than reporting rows touched.
    """
    filled = []
    for field in FILLABLE:
        incoming = mapped.get(field)
        if not incoming:
            continue
        if getattr(client, field, None):          # a value here already wins
            continue
        setattr(client, field, incoming)
        filled.append(field)
    return filled


# --- the runner -----------------------------------------------------------
#
# One slice of work per call. The shape is always the same: read a page, write
# what it held, move the checkpoint, stop when the budget or the quota runs out.
# Nothing here assumes it will be allowed to finish.


def latest_run(db) -> ImportRun | None:
    return db.query(ImportRun).order_by(ImportRun.id.desc()).first()


def begin(db, full: bool = True) -> ImportRun:
    """Start a run, carrying today's quota counter forward.

    A fresh row would reset `requests_used` to zero and hand the operator a
    second 500 requests that IntakeQ has no intention of honouring.
    """
    previous = latest_run(db)
    run = ImportRun(stage="clients", page=1, status="running")
    if previous and previous.quota_day == date.today().isoformat():
        run.quota_day = previous.quota_day
        run.requests_used = previous.requests_used
    if not full and previous and previous.status == "done":
        #  An incremental pass asks only for what changed since the last
        #  complete one. The date is deliberately the run's start, not its
        #  finish: anything edited *during* a run may have been read before the
        #  edit, and re-reading a row costs one request while missing one costs
        #  a silent gap.
        run.updated_since = previous.started_at.date().isoformat()
    db.add(run)
    db.flush()
    return run


def _client_by_intakeq_id(db, intakeq_id: str):
    from .models import Client as ClientRow

    if not intakeq_id:
        return None
    return (db.query(ClientRow)
            .filter(ClientRow.intakeq_client_id == intakeq_id).first())


def _match_existing(db, mapped: dict):
    """The person this payload is probably already a row for.

    Same two signals main.py's _same_person uses, and for the same reason: an
    email is meant to be unique to a person, and a full name with a matching
    date of birth is how every registry identifies somebody. Duplicated rather
    than imported from main to avoid a circular import - main imports this
    module, and a shared home for it is a later tidy-up, not this change.
    """
    from .models import Client as ClientRow

    live = db.query(ClientRow).filter_by(archived=False)
    email = (mapped.get("email") or "").strip().lower()
    if email:
        hit = live.filter(ClientRow.email == email).first()
        if hit:
            return hit

    first = (mapped.get("first_name") or "").strip().lower()
    last = (mapped.get("last_name") or "").strip().lower()
    born = mapped.get("dob")
    if first and last and born:
        for candidate in live.all():
            if (candidate.first_name.strip().lower() == first
                    and candidate.last_name.strip().lower() == last
                    and candidate.dob == born):
                return candidate
    return None


def _absorb_client(db, run: ImportRun, payload: dict) -> None:
    from .models import Client as ClientRow

    mapped = map_client(payload)
    intakeq_id = mapped.pop("intakeq_client_id", "")
    if not intakeq_id:
        run.note_failure("A client arrived with no ClientId - skipped.")
        return

    run.clients_seen += 1
    existing = _client_by_intakeq_id(db, intakeq_id)

    if existing is None:
        #  Somebody may already be here under the same identity, typed in by
        #  hand before the import ever ran. Claiming that row is right: the
        #  alternative is two rows for one person, which is the exact failure
        #  _same_person() was written to prevent.
        existing = _match_existing(db, mapped)
        if existing is not None:
            existing.intakeq_client_id = intakeq_id
            if fill_blanks(existing, mapped):
                run.clients_filled += 1
            return

        row = ClientRow(intakeq_client_id=intakeq_id, source=INTAKEQ)
        fill_blanks(row, mapped)
        if not (row.first_name or row.last_name):
            run.note_failure(f"IntakeQ client {intakeq_id} has no name - skipped.")
            return
        db.add(row)
        run.clients_new += 1
        return

    if fill_blanks(existing, mapped):
        run.clients_filled += 1


def _absorb_intake(db, run: ImportRun, payload: dict) -> None:
    mapped = map_intake(payload)
    intake_id = mapped.pop("intakeq_intake_id", "")
    intakeq_client_id = mapped.pop("intakeq_client_id", "")
    if not intake_id:
        run.note_failure("An intake arrived with no Id - skipped.")
        return

    run.intakes_seen += 1
    if (db.query(ImportedIntake)
            .filter(ImportedIntake.intakeq_intake_id == intake_id).first()):
        return

    owner = _client_by_intakeq_id(db, intakeq_client_id)
    #  An intake whose client is not here is still worth keeping. It is a signed
    #  document; losing it because the rows arrived in an awkward order would be
    #  the worst outcome available. It attaches later, or a human attaches it.
    if owner is None and intakeq_client_id:
        run.note_failure(f"Intake {intake_id} references IntakeQ client "
                         f"{intakeq_client_id}, who is not here yet.")

    db.add(ImportedIntake(client_id=owner.id if owner else None,
                          intakeq_intake_id=intake_id, **mapped))
    run.intakes_new += 1


def run_slice(db, run: ImportRun, budget: Budget | None = None) -> ImportRun:
    """Do as much of the run as the budget and the quota allow, then stop.

    Every exit path leaves the run in a state that describes itself, because
    the screen that reads it has no other source of truth.
    """
    budget = budget or Budget()
    if run.status != "running":
        return run

    sim = simulated(db)
    key = api_key(db) or ("SIMULATED-NOT-A-REAL-KEY" if sim else "")
    if not key:
        run.status = "failed"
        run.message = ("No IntakeQ API key. Add INTAKEQ_API_KEY on the "
                       "Connections screen, then start the import again.")
        return run

    api = Api(key, run, budget, sim)
    try:
        while not budget.spent and run.status == "running":
            if run.stage == "clients":
                rows = api.json("/clients", {"page": run.page,
                                             "includeProfile": "true",
                                             "dateUpdatedStart": run.updated_since})
                if not isinstance(rows, list):
                    raise Refused("IntakeQ sent a client page that is not a list.")
                for payload in rows:
                    if isinstance(payload, dict):
                        _absorb_client(db, run, payload)
                db.flush()
                if len(rows) < PAGE_SIZE:
                    run.stage, run.page = "intakes", 1
                else:
                    run.page += 1

            elif run.stage == "intakes":
                rows = api.json("/intakes/summary",
                                {"page": run.page,
                                 "updatedSince": run.updated_since})
                if not isinstance(rows, list):
                    raise Refused("IntakeQ sent an intake page that is not a list.")
                for payload in rows:
                    if isinstance(payload, dict):
                        _absorb_intake(db, run, payload)
                db.flush()
                if len(rows) < PAGE_SIZE:
                    run.stage = "done"
                    run.status = "done"
                    run.finished_at = datetime.utcnow()
                    run.message = "Every page read."
                else:
                    run.page += 1
            else:
                run.status = "done"
                run.finished_at = datetime.utcnow()

    except QuotaSpent as exc:
        run.status = "paused"
        run.message = str(exc)
    except Refused as exc:
        run.status = "failed"
        run.message = str(exc)
    except Exception as exc:                            # noqa: BLE001
        #  An unexpected fault must not leave a run marked running forever: the
        #  screen would show a job that nothing is working on.
        run.status = "failed"
        run.message = f"{type(exc).__name__}: {exc}"[:500]

    return run


def fetch_pdf(db, intake: ImportedIntake) -> bytes | None:
    """The signed document, fetched the first time somebody asks for it.

    This is the whole reason the backfill is cheap. It costs one request, and
    only for intakes a human actually opens.
    """
    if intake.have_pdf:
        return intake.pdf

    sim = simulated(db)
    key = api_key(db) or ("SIMULATED-NOT-A-REAL-KEY" if sim else "")
    if not key:
        intake.pdf_error = "No IntakeQ API key is configured."
        return None

    run = latest_run(db) or begin(db)
    try:
        body = Api(key, run, Budget(requests=1, seconds=30), sim).pdf(
            intake.intakeq_intake_id)
    except (QuotaSpent, Refused) as exc:
        intake.pdf_error = str(exc)[:255]
        return None
    except Exception as exc:                            # noqa: BLE001
        intake.pdf_error = f"{type(exc).__name__}: {exc}"[:255]
        return None

    intake.pdf = body
    intake.pdf_fetched_at = datetime.utcnow()
    intake.pdf_error = ""
    return body


# --- looking before leaping --------------------------------------------------


def preview(db, pages: int = 1) -> dict:
    """Fetch a page of each kind and show what the mapper makes of it.

    Writes nothing. This exists because the mapping is the one part of the
    import that cannot be proven without a real key: every field name here was
    read out of IntakeQ's documentation, and a name that is wrong returns an
    empty string rather than an error. Two thousand patients would import with
    no date of birth and nothing would complain - and the date of birth is what
    the archive matcher leans on hardest.

    So before the first real run, this asks for two records and reports three
    things side by side: the keys IntakeQ actually sent, what the mapper pulled
    out of them, and which of our fields came back empty. A blank column is
    obvious here and invisible after a backfill.
    """
    sim = simulated(db)
    key = api_key(db) or ("SIMULATED-NOT-A-REAL-KEY" if sim else "")
    if not key:
        return {"ok": False, "error": "No IntakeQ API key is configured."}

    run = ImportRun(stage="preview", status="running")
    api = Api(key, run, Budget(requests=2 * pages, seconds=60), sim)
    out = {"ok": True, "simulated": sim, "requests_used": 0, "clients": [], "intakes": [],
           "client_keys": [], "intake_keys": [], "blank_fields": {}}
    try:
        rows = api.json("/clients", {"page": 1, "includeProfile": "true"})
        sample = [r for r in (rows or []) if isinstance(r, dict)][:3]
        out["client_keys"] = sorted({k for r in sample for k in r})
        blanks = {}
        for raw in sample:
            mapped = map_client(raw)
            out["clients"].append({"raw": raw, "mapped": mapped})
            for field_name, value in mapped.items():
                if not value:
                    blanks[field_name] = blanks.get(field_name, 0) + 1
        out["blank_fields"] = blanks
        out["client_count"] = len(rows or [])

        rows = api.json("/intakes/summary", {"page": 1})
        sample = [r for r in (rows or []) if isinstance(r, dict)][:3]
        out["intake_keys"] = sorted({k for r in sample for k in r})
        for raw in sample:
            out["intakes"].append({"raw": raw, "mapped": map_intake(raw)})
        out["intake_count"] = len(rows or [])
    except (QuotaSpent, Refused) as exc:
        out["ok"], out["error"] = False, str(exc)
    except Exception as exc:                            # noqa: BLE001
        out["ok"], out["error"] = False, f"{type(exc).__name__}: {exc}"
    out["requests_used"] = run.requests_used
    return out
