"""A practice that does not exist, answering as IntakeQ and Tebra would.

This exists so the integration can be shown working before anybody has issued a
credential, and so that the day credentials arrive, nothing has to be rewritten
to use them.

**Where the seam is, and why it is there.** The obvious way to fake an
integration is to swap the client class for a pretend one. That demonstrates the
pretend one. This instead intercepts at the *HTTP boundary*: the real
`Api._open` and `Tebra.call` run, build their real request, and receive bytes
that look exactly like the far end's. Everything above the wire is the
production code path - the SOAP envelope, the XML parsing, the JSON paging, the
quota accounting, the field mapping, the error handling. A demo that exercises
all of that is evidence; one that exercises a stub is a slideshow.

So when real credentials replace the simulated ones, the only thing that changes
is that the bytes come from the network. No code moves.

**It never pretends to be real.** Every simulated response is derived from the
fixed roster below, the Connections screen says "simulated" rather than
"connected", and the patients it creates carry a source that marks them as
demonstration data. A mock that dresses itself up as a live system is worse than
no mock: somebody eventually believes it, and the first person to believe it is
usually the one giving the demonstration.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta

SETTING = "DEMO_MODE"

#  Marks every row this module causes to exist. A demonstration that leaves
#  rows behind indistinguishable from real ones is a demonstration that
#  contaminates the database it ran against.
SOURCE = "simulated"


def enabled(db=None) -> bool:
    from . import credentials as creds

    return (creds.resolve(db, SETTING) or "").strip().lower() in ("1", "true", "on", "yes")


def set_enabled(db, on: bool, who: str = "") -> None:
    from . import credentials as creds

    creds.put(db, SETTING, "1" if on else "0", who=who)


# --- the practice that does not exist -------------------------------------
#
# Fixed, not random. A demonstration that shows different numbers each time it
# runs cannot be rehearsed, and a bug that only appears for one seed is a bug
# nobody can reproduce.

FIRST = ["Alice", "Marcus", "Priya", "Daniel", "Sofia", "Wei", "Amara",
         "Tomas", "Leah", "Idris", "Nadia", "Owen", "Farah", "Julian"]
LAST = ["Whitfield", "Okonkwo", "Raman", "Beaumont", "Castillo", "Chen",
        "Diallo", "Nowak", "Bergström", "Haddad", "Petrov", "Sullivan"]
CITIES = [("Ontario", "California", "91761"), ("Rancho Cucamonga", "California", "91730"),
          ("Upland", "California", "91786"), ("Fontana", "California", "92335")]
PACKETS = ["Adult Intake Packet", "ADHD Screening Questionnaire",
           "Consent and Privacy Acknowledgement", "Medication History Update"]
CLINICIANS = ["Dr Amelia Hart", "Dr Ravi Menon", "Dr Grace Oduya"]

CLIENT_COUNT = 14
INTAKE_COUNT = 22


def _client(n: int) -> dict:
    """One IntakeQ client, shaped the way IntakeQ shapes one.

    Deliberately not uniform. Two of every seven have no date of birth and one
    in five no phone number, because a real export is ragged and an importer
    that has only ever seen complete records is an importer nobody has tested.
    """
    first = FIRST[n % len(FIRST)]
    last = LAST[(n * 3) % len(LAST)]
    city, state, postal = CITIES[n % len(CITIES)]
    born = date(1948 + (n * 5) % 55, 1 + (n * 7) % 12, 1 + (n * 11) % 28)

    row = {
        "ClientId": 4000 + n,
        "ClientNumber": 4000 + n,
        "Name": f"{first} {last}",
        "FirstName": first,
        "LastName": last,
        "Email": f"{first.lower()}.{last.lower().replace('ö','o')}@example.com",
        "City": city,
        "StateShort": state,
        "PostalCode": postal,
        "DateCreated": _epoch_ms(date(2026, 1, 1) + timedelta(days=n * 9)),
    }
    #  Dates of birth arrive as epoch milliseconds, which is what IntakeQ sends
    #  and the single most likely thing for an importer to get wrong.
    if n % 7 not in (3, 5):
        row["DateOfBirth"] = _epoch_ms(born)
    if n % 5:
        row["Phone"] = f"(909) 555-{1000 + n * 7:04d}"[:14]
    return row


def _intake(n: int) -> dict:
    client_n = n % CLIENT_COUNT
    submitted = datetime(2026, 3, 1) + timedelta(days=n * 8, hours=(n * 5) % 9)
    return {
        "Id": f"iq-{7700 + n}",
        "ClientId": 4000 + client_n,
        "ClientName": f"{FIRST[client_n % len(FIRST)]} "
                      f"{LAST[(client_n * 3) % len(LAST)]}",
        "QuestionnaireName": PACKETS[n % len(PACKETS)],
        "PractitionerName": CLINICIANS[n % len(CLINICIANS)],
        "DateSubmitted": _epoch_ms(submitted),
        "Status": "Completed" if n % 6 else "Partial",
    }


def _epoch_ms(when) -> int:
    if isinstance(when, date) and not isinstance(when, datetime):
        when = datetime(when.year, when.month, when.day)
    return int((when - datetime(1970, 1, 1)).total_seconds() * 1000)


def _pdf(title: str, who: str, when: str) -> bytes:
    """A real, openable one-page PDF.

    Built by hand rather than pulled from a library: the importer stores bytes
    and the browser opens them, and a placeholder that is not actually a PDF
    turns a working feature into a broken-looking one at the only moment
    anybody is watching.
    """
    lines = [
        f"BT /F1 16 Tf 62 726 Td ({_pdf_esc(title)}) Tj ET",
        f"BT /F1 11 Tf 62 700 Td ({_pdf_esc(who)}) Tj ET",
        f"BT /F1 11 Tf 62 682 Td (Submitted {_pdf_esc(when)}) Tj ET",
        "BT /F1 10 Tf 62 640 Td (SIMULATED DOCUMENT - demonstration data only.) Tj ET",
        "BT /F1 10 Tf 62 624 Td (No real patient completed this form.) Tj ET",
    ]
    stream = "\n".join(lines).encode("latin-1", "replace")
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream
        + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    start = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += (f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\n"
            f"startxref\n{start}\n%%EOF\n").encode()
    return bytes(out)


def _pdf_esc(text: str) -> str:
    return text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


# --- answering as IntakeQ -------------------------------------------------


def intakeq_response(url: str) -> bytes:
    """Bytes for a GET the real client just built, chosen by its URL.

    Reads the real query string - page, and the /pdf suffix - so the importer's
    own paging logic decides what it gets, exactly as it would live.
    """
    from urllib.parse import parse_qs, urlparse

    parts = urlparse(url)
    query = parse_qs(parts.query)
    page = int((query.get("page") or ["1"])[0])

    if parts.path.endswith("/pdf"):
        intake_id = parts.path.split("/intakes/", 1)[1].split("/", 1)[0]
        n = int(intake_id.rsplit("-", 1)[-1]) - 7700 if "-" in intake_id else 0
        row = _intake(max(0, n))
        return _pdf(row["QuestionnaireName"], row["ClientName"],
                    datetime.utcfromtimestamp(row["DateSubmitted"] / 1000)
                    .strftime("%d %b %Y"))

    if "/questionnaires" in parts.path:
        return json.dumps([{"Id": f"tmpl-{i}", "Name": name, "Archived": False}
                           for i, name in enumerate(PACKETS)]).encode()

    if "/intakes/summary" in parts.path:
        rows = [_intake(i) for i in range(INTAKE_COUNT)]
    elif "/clients" in parts.path:
        rows = [_client(i) for i in range(CLIENT_COUNT)]
    else:
        return b"[]"

    #  Paged the way IntakeQ pages: 100 to a page, and a short page means the
    #  end. The roster is smaller than a page, so page 1 is also the last -
    #  which is exactly the signal the importer uses to move on.
    size = 100
    return json.dumps(rows[(page - 1) * size:page * size]).encode()


# --- answering as Tebra ---------------------------------------------------

_created: dict[str, int] = {"patients": 0, "documents": 0, "appointments": 0}


def tebra_response(operation: str, request_xml: str) -> bytes:
    """A SOAP response body for the operation the real client just built.

    Ids are derived from the request rather than counted, so the same patient
    pushed twice would come back with the same id - which is what a real system
    with a uniqueness rule does, and it means the importer's own duplicate
    guard is exercised rather than assumed.
    """
    import re

    def field(name: str) -> str:
        m = re.search(rf"<kareo:{name}>(.*?)</kareo:{name}>", request_xml, re.S)
        return (m.group(1) or "").strip() if m else ""

    if operation == "GetPractices":
        return _soap("GetPracticesResult",
                     "<Practices><PracticeData><ID>1</ID>"
                     "<PracticeName>Meridian Behavioral Health (simulated)"
                     "</PracticeName></PracticeData></Practices>")

    if operation == "CreatePatient":
        external = field("PatientExternalID") or field("LastName") or "x"
        digest = hashlib.sha1(external.encode()).hexdigest()[:6].upper()
        _created["patients"] += 1
        return _soap("CreatePatientResult", f"<PatientID>TBR-{digest}</PatientID>")

    if operation == "CreateDocument":
        _created["documents"] += 1
        name = field("Name") or "document"
        digest = hashlib.sha1(name.encode()).hexdigest()[:6].upper()
        return _soap("CreateDocumentResult", f"<DocumentId>DOC-{digest}</DocumentId>")

    if operation == "CreateAppointment":
        _created["appointments"] += 1
        digest = hashlib.sha1(field("StartTime").encode()).hexdigest()[:6].upper()
        return _soap("CreateAppointmentResult",
                     f"<AppointmentId>APT-{digest}</AppointmentId>")

    if operation == "CreateEncounter":
        _created.setdefault("encounters", 0)
        _created["encounters"] += 1
        digest = hashlib.sha1(
            (field("PostDate") + field("ProcedureCode")).encode()).hexdigest()[:6].upper()
        return _soap("CreateEncounterResult",
                     f"<EncounterID>ENC-{digest}</EncounterID>")

    if operation == "CreatePayment":
        _created.setdefault("payments", 0)
        _created["payments"] += 1
        digest = hashlib.sha1(
            (field("PostDate") + field("AmountPaid")).encode()).hexdigest()[:6].upper()
        return _soap("CreatePaymentResult",
                     f"<PaymentID>PMT-{digest}</PaymentID>")

    if operation == "GetPatients":
        rows = "".join(
            f"<PatientData><PatientID>TBR-{4000 + i}</PatientID>"
            f"<FirstName>{FIRST[i % len(FIRST)]}</FirstName>"
            f"<LastName>{LAST[(i * 3) % len(LAST)]}</LastName>"
            f"<DateofBirth>{date(1948 + (i * 5) % 55, 1 + (i * 7) % 12, 1 + (i * 11) % 28)}"
            f"T00:00:00</DateofBirth>"
            f"<City>{CITIES[i % len(CITIES)][0]}</City></PatientData>"
            for i in range(CLIENT_COUNT))
        return _soap("GetPatientsResult", f"<Patients>{rows}</Patients>")

    if operation == "GetAppointments":
        return _soap("GetAppointmentsResult", "<Appointments/>")

    #  An operation the simulator has not been taught. Saying so is the point:
    #  a mock that silently returns an empty success for anything it does not
    #  recognise teaches you that unimplemented calls work.
    return _soap(f"{operation}Result",
                 f"<ErrorMessage>The simulator does not implement "
                 f"{operation}.</ErrorMessage>")


def _soap(result_tag: str, inner: str) -> bytes:
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body>'
        f"<{result_tag} xmlns=\"http://www.kareo.com/api/schemas/\">"
        "<SecurityResponse><Authorized>true</Authorized>"
        "<SecurityResult>Simulated</SecurityResult></SecurityResponse>"
        f"{inner}</{result_tag}></s:Body></s:Envelope>"
    ).encode("utf-8")


def counters() -> dict:
    return dict(_created)
