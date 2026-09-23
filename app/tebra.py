"""Talking to Tebra, which is a SOAP service and not a REST one.

Worth stating plainly at the top, because every summary of this integration
written from the outside gets it wrong: there is no REST API here, no OAuth, no
client id and secret, and no bearer token to refresh. Tebra's integration API is
SOAP 1.1 at `webservice.kareo.com/services/soap/2.1/KareoServices.svc`, and it
authenticates by putting three values - CustomerKey, User, Password - inside a
RequestHeader element in the body of every single call. Credentials travel with
the request, every time. Code written against the imagined REST version fails on
the first call, so this module is built from the service's own published schema
rather than from a description of it.

The field names here were read out of Tebra's XSD, not guessed. That matters
more than it sounds: the date of birth field is spelled `DateofBirth`, with a
lower-case o, and a request that spells it the obvious way is accepted and
silently drops the date. Every element name in the builders below appears
exactly as the schema spells it.

**What this can and cannot do.** The service publishes 32 operations covering
patients, appointments, encounters, charges, payments and documents. It does not
publish a single clinical read - no medications, allergies, problems, vitals,
lab results or notes. So this module can create a patient, file a document into
their chart, book an appointment, and read demographics back. It cannot pull a
chart, and no amount of work here will change that; that is a C-CDA or bulk-FHIR
export, a different project entirely.

**Nothing is pushed twice.** A patient created in Tebra gets their Tebra id
written back onto the local row, and every push checks for it first. Two charts
for one person is the worst outcome available to an integration like this -
their answers land on one and their history on the other, and the practice finds
out months later.
"""

from __future__ import annotations

import base64
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime

ENDPOINT = "https://webservice.kareo.com/services/soap/2.1/KareoServices.svc"
NS = "http://www.kareo.com/api/schemas/"
ACTION = "http://www.kareo.com/api/schemas/KareoServices/"

#  Sent in every RequestHeader. Tebra uses it for their own support diagnostics;
#  a request that identifies itself is one somebody can trace when it misbehaves.
CLIENT_VERSION = "Meridian/1.0"

TIMEOUT = 45


class TebraError(Exception):
    """Tebra answered, and the answer was no."""


class NotConfigured(TebraError):
    """No credentials. Distinct from a refusal - nothing was attempted."""


# --- building the envelope ------------------------------------------------


def esc(value) -> str:
    """XML-escape a value for a text node.

    Ampersand first, or the escapes introduced by the later replacements get
    escaped in turn and a practice called "Smith & Jones" arrives as
    "Smith &amp;amp; Jones". Quotes are escaped too: these values are only ever
    used as text, but a helper that is safe in one context and not another is a
    helper that will eventually be used in the other one.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (date, datetime)):
        #  Tebra's dateTime fields accept an ISO-8601 local time. Sending a
        #  timezone-aware value gets it converted on their side, which silently
        #  moves an appointment by hours.
        return value.strftime("%Y-%m-%dT%H:%M:%S")
    return (str(value).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;").replace("'", "&apos;"))


def fields(**pairs) -> str:
    """Element string for the pairs that carry a value.

    Omitting an empty element rather than sending it blank is deliberate: for
    several of Tebra's fields an empty string is a value, and sending one on an
    update clears what is already there.
    """
    out = []
    for name, value in pairs.items():
        if value is None or value == "":
            continue
        out.append(f"<kareo:{name}>{esc(value)}</kareo:{name}>")
    return "".join(out)


def envelope(operation: str, header: dict, body: str) -> bytes:
    """A complete SOAP 1.1 envelope for one operation.

    The RequestHeader goes *inside* the request element, not in a soap:Header -
    Tebra puts authentication in the body, which is unusual enough that reading
    it as a normal WS-Security header and putting the credentials in the wrong
    place is the most common way to get a fault back.
    """
    auth = fields(CustomerKey=header["customer_key"], User=header["user"],
                  Password=header["password"], ClientVersion=CLIENT_VERSION)
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/" '
        f'xmlns:kareo="{NS}">'
        "<soap:Body>"
        f"<kareo:{operation}><kareo:request>"
        f"<kareo:RequestHeader>{auth}</kareo:RequestHeader>"
        f"{body}"
        f"</kareo:request></kareo:{operation}>"
        "</soap:Body></soap:Envelope>"
    ).encode("utf-8")


# --- reading the answer ---------------------------------------------------


def text_of(xml: str, tag: str) -> str:
    """The first <tag> value, namespace prefix or not."""
    m = re.search(rf"<(?:\w+:)?{tag}[^>]*>(.*?)</(?:\w+:)?{tag}>", xml, re.S)
    return (m.group(1) or "").strip() if m else ""


def blocks_of(xml: str, tag: str) -> list[str]:
    return re.findall(rf"<(?:\w+:)?{tag}[^>]*>(.*?)</(?:\w+:)?{tag}>", xml, re.S)


def unescape(value: str) -> str:
    return (value.replace("&lt;", "<").replace("&gt;", ">")
            .replace("&quot;", '"').replace("&apos;", "'")
            .replace("&amp;", "&"))


def row(block: str, *names: str) -> dict:
    return {n: unescape(text_of(block, n)) for n in names}


def check_for_error(xml: str) -> None:
    """Raise if Tebra reported a failure inside a 200 response.

    Two ways it says no, and only one of them is an HTTP error. A SOAP fault
    arrives as HTTP 500 and is handled by the caller; a business refusal - bad
    credentials, a missing practice, a rejected field - arrives as a perfectly
    ordinary 200 with ErrorMessage set and SecurityResponse/Authorized false.
    Treating that as success is how an integration reports that it filed two
    hundred documents nobody can find.
    """
    message = text_of(xml, "ErrorMessage")
    if message:
        raise TebraError(message[:400])
    authorized = text_of(xml, "Authorized")
    if authorized.lower() == "false":
        raise TebraError(text_of(xml, "SecurityResult")
                         or "Tebra refused the credentials.")


# --- the client -----------------------------------------------------------


@dataclass
class Credentials:
    customer_key: str = ""
    user: str = ""
    password: str = ""
    practice_id: str = ""
    practice_name: str = ""

    @property
    def complete(self) -> bool:
        return bool(self.customer_key and self.user and self.password)

    @property
    def missing(self) -> list[str]:
        return [name for name, value in
                (("TEBRA_CUSTOMER_KEY", self.customer_key),
                 ("TEBRA_USER", self.user),
                 ("TEBRA_PASSWORD", self.password)) if not value]


def credentials_from(db) -> Credentials:
    #  Imported here, not at module scope: credentials imports models, and models
    #  imports this module's siblings. A top-level import closes that loop.
    from . import credentials as creds

    return Credentials(
        customer_key=creds.resolve(db, "TEBRA_CUSTOMER_KEY"),
        user=creds.resolve(db, "TEBRA_USER"),
        password=creds.resolve(db, "TEBRA_PASSWORD"),
        practice_id=creds.resolve(db, "TEBRA_PRACTICE_ID"),
        practice_name=creds.resolve(db, "TEBRA_PRACTICE_NAME"),
    )


@dataclass
class Call:
    """One completed request, kept so the audit line can describe it."""

    operation: str
    ok: bool
    detail: str = ""
    xml: str = field(default="", repr=False)


class Tebra:
    """A SOAP client that reports what happened rather than what was attempted."""

    def __init__(self, creds: Credentials, endpoint: str = ENDPOINT):
        if not creds.complete:
            raise NotConfigured("Not set: " + ", ".join(creds.missing))
        self.creds = creds
        self.endpoint = endpoint
        self.header = {"customer_key": creds.customer_key, "user": creds.user,
                       "password": creds.password}

    def call(self, operation: str, body: str = "") -> str:
        data = envelope(operation, self.header, body)
        request = urllib.request.Request(
            self.endpoint, data=data, method="POST",
            headers={"Content-Type": "text/xml; charset=utf-8",
                     "SOAPAction": ACTION + operation})
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                xml = response.read(8_000_000).decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read(4000).decode("utf-8", "replace") if exc.fp else ""
            fault = text_of(detail, "faultstring")
            raise TebraError(
                f"Tebra replied HTTP {exc.code}"
                + (f": {fault}" if fault else f" ({exc.reason})")) from exc
        except Exception as exc:                        # noqa: BLE001
            raise TebraError(f"Could not reach Tebra ({type(exc).__name__}).") from exc

        check_for_error(xml)
        return xml

    # --- reads ------------------------------------------------------------

    def practices(self) -> list[dict]:
        """The practices this API user can see.

        The smallest call that proves all three credentials at once while
        reading nothing about any patient - a connection test should not touch a
        medical record to prove it can reach the server.
        """
        xml = self.call("GetPractices",
                        "<kareo:Fields><kareo:PracticeName>true</kareo:PracticeName>"
                        "<kareo:ID>true</kareo:ID></kareo:Fields>")
        return [row(b, "ID", "PracticeName")
                for b in blocks_of(xml, "PracticeData")]

    def patients(self, since: date | None = None, practice_id: str = "") -> list[dict]:
        """Demographics, optionally only those changed since a date.

        `FromLastModifiedDate` is what makes a second sync cheap: without it
        every run reads the entire practice, and with a real patient list that
        is the difference between a minute and an afternoon.
        """
        filters = fields(PracticeID=practice_id or self.creds.practice_id,
                         FromLastModifiedDate=since)
        want = fields(**{n: True for n in (
            "PatientID", "FirstName", "LastName", "MiddleName", "DateofBirth",
            "Gender", "EmailAddress", "HomePhone", "MobilePhone", "AddressLine1",
            "City", "State", "ZipCode", "MedicalRecordNumber", "PatientExternalID")})
        xml = self.call("GetPatients",
                        f"<kareo:Fields>{want}</kareo:Fields>"
                        f"<kareo:Filter>{filters}</kareo:Filter>")
        return [row(b, "PatientID", "FirstName", "LastName", "DateofBirth",
                    "Gender", "EmailAddress", "HomePhone", "MobilePhone",
                    "City", "State", "ZipCode", "MedicalRecordNumber",
                    "PatientExternalID")
                for b in blocks_of(xml, "PatientData")]

    def appointments(self, start: date, end: date, practice_id: str = "") -> list[dict]:
        filters = fields(PracticeID=practice_id or self.creds.practice_id,
                         StartDate=start, EndDate=end)
        xml = self.call("GetAppointments", f"<kareo:Filter>{filters}</kareo:Filter>")
        return [row(b, "AppointmentId", "PatientSummary", "StartTime", "EndTime",
                    "AppointmentStatus", "AppointmentType", "ProviderId", "Notes")
                for b in blocks_of(xml, "AppointmentData")]

    # --- writes -----------------------------------------------------------

    def create_patient(self, patient: dict) -> str:
        """Create a chart. Returns Tebra's PatientID.

        Field names are the schema's own, `DateofBirth` included. The caller
        passes a mapped dict rather than a Client row so that this layer never
        has to know what a Client is - and so the mapping can be tested without
        a database.
        """
        body = f"<kareo:Patient>{fields(**patient)}</kareo:Patient>"
        xml = self.call("CreatePatient", body)
        new_id = text_of(xml, "PatientID") or text_of(xml, "PatientId")
        if not new_id:
            raise TebraError("Tebra accepted the patient but returned no PatientID.")
        return new_id

    def create_document(self, *, practice_id: str, name: str, file_name: str,
                        content: bytes, patient_id: str = "",
                        label: str = "", notes: str = "",
                        document_date: date | None = None) -> str:
        """File a document into a chart. Returns Tebra's DocumentId.

        FileContent is base64. The schema marks FileContent, FileName, Name and
        PracticeId required; PatientId is optional, and a document sent without
        one lands in the practice's unfiled queue rather than failing - which is
        worse than a failure, because nobody is told.
        """
        if not patient_id:
            raise TebraError("Refusing to file a document with no PatientId: it "
                             "would land unfiled and nobody would be told.")
        body = "<kareo:DocumentToCreate>" + fields(
            PracticeId=practice_id or self.creds.practice_id,
            PatientId=patient_id, Name=name, FileName=file_name,
            FileContent=base64.b64encode(content).decode("ascii"),
            Label=label, DocumentNotes=notes,
            DocumentDate=document_date or date.today(),
        ) + "</kareo:DocumentToCreate>"
        xml = self.call("CreateDocument", body)
        return text_of(xml, "DocumentId") or text_of(xml, "ID")

    def create_appointment(self, appointment: dict) -> str:
        """Book an appointment. Returns Tebra's AppointmentId.

        The schema marks seven fields required - AppointmentStatus,
        AppointmentType, StartTime, EndTime, IsRecurring, PracticeId and
        ServiceLocationId - and checking them here rather than sending an
        incomplete request turns a SOAP fault nobody can read into a sentence
        naming the field.
        """
        needed = ("AppointmentStatus", "AppointmentType", "StartTime", "EndTime",
                  "IsRecurring", "PracticeId", "ServiceLocationId")
        absent = [n for n in needed if appointment.get(n) in (None, "")]
        if absent:
            raise TebraError("Tebra requires these appointment fields: "
                             + ", ".join(absent))
        body = f"<kareo:Appointment>{fields(**appointment)}</kareo:Appointment>"
        xml = self.call("CreateAppointment", body)
        return text_of(xml, "AppointmentId")


# --- mapping our rows onto Tebra's fields ---------------------------------
#
# Pure functions. No database, no network - so the mapping can be checked
# against a recorded payload, which is the only honest way to test a schema you
# cannot call yet.


def patient_payload(client, practice_id: str = "") -> dict:
    """A local Client as Tebra's PatientCreate fields.

    `PatientExternalID` carries our own id across, which is what makes the two
    systems re-joinable later without matching on a name. It is the single most
    valuable field in this mapping and the easiest one to forget.
    """
    return {
        "PracticeId": practice_id,
        "FirstName": (client.first_name or "").strip()[:50],
        "LastName": (client.last_name or "").strip()[:50],
        "DateofBirth": client.dob,                 # schema spells it this way
        "EmailAddress": (client.email or "").strip()[:100],
        "HomePhone": (client.phone or "").strip()[:20],
        "AddressLine1": "",
        "City": (client.city or "").strip()[:50],
        "State": (client.state or "").strip()[:50],
        "ZipCode": (client.postal_code or "").strip()[:10],
        "PatientExternalID": f"MER-{client.id}",
        "MedicalRecordNumber": str(client.hospital_id or ""),
    }


def appointment_payload(booking, *, practice_id: str, service_location_id: str,
                        patient_tebra_id: str) -> dict:
    """A local Appointment as Tebra's AppointmentCreate fields."""
    from datetime import timedelta

    hour, _, minute = (booking.at_time or "09:00").partition(":")
    start = datetime.combine(booking.on_day,
                             datetime.min.time()).replace(
        hour=int(hour or 9), minute=int(minute or 0))
    return {
        "PracticeId": practice_id,
        "ServiceLocationId": service_location_id,
        "PatientId": patient_tebra_id,
        "StartTime": start,
        "EndTime": start + timedelta(minutes=booking.minutes or 30),
        "AppointmentStatus": "Scheduled",
        "AppointmentType": "Patient",
        "IsRecurring": False,
        "AppointmentName": (booking.kind or "Visit")[:100],
        "Notes": (booking.notes or "")[:500],
    }


# --- the service layer ----------------------------------------------------
#
# Everything above talks to Tebra. Everything below decides whether it should,
# and writes down that it did. The split matters: the client can be exercised
# against a recorded response with no database, and the rules here can be read
# without wading through XML.


def connect(db) -> "Tebra":
    return Tebra(credentials_from(db))


def check(db):
    """Prove the credentials work, reading nothing about any patient.

    Returns the same shape integrations.py uses for every other connection, so
    the Connections screen needs no special case for this one.
    """
    from .integrations import Result

    try:
        practices = connect(db).practices()
    except NotConfigured as exc:
        return Result(False, "Credentials missing.", str(exc))
    except TebraError as exc:
        return Result(False, "Tebra refused the request.", str(exc))

    names = ", ".join(p.get("PracticeName", "?") for p in practices[:5])
    return Result(bool(practices) or True,
                  f"Connected. {len(practices)} practice(s) visible.",
                  names or "GetPractices returned without an error.")


def push_patient(db, client, *, user=None, ip: str = "") -> str:
    """Create this patient's chart in Tebra, once.

    Returns the Tebra patient id, whether it was created now or previously. The
    check for an existing id is the whole point of the function: two charts for
    one person is the worst failure this integration can produce, and it is
    silent - their answers land on one chart and their history on the other, and
    the practice finds out months later.
    """
    from .models import log

    if client.tebra_patient_id:
        return client.tebra_patient_id

    api = connect(db)
    practice_id = api.creds.practice_id
    if not practice_id:
        raise TebraError(
            "No TEBRA_PRACTICE_ID set. Tebra requires the practice on every "
            "write, and guessing it files the patient with the wrong one.")

    payload = patient_payload(client, practice_id)
    new_id = api.create_patient(payload)

    client.tebra_patient_id = new_id
    #  PHI left this system. That is the event the audit log exists for, and it
    #  is written before the commit so it cannot be separated from the change
    #  that caused it.
    log(db, f"Patient chart created in Tebra (id {new_id})", "client",
        client.id, user_id=getattr(user, "id", None), ip=ip)
    return new_id


def push_document(db, client, *, content: bytes, name: str, file_name: str,
                  label: str = "", notes: str = "", user=None,
                  ip: str = "") -> str:
    """File a completed packet into the patient's Tebra chart.

    Requires the chart to exist first and says so, rather than creating one as a
    side effect. A function that quietly creates a medical record while you
    thought you were uploading a PDF is a function nobody can reason about.
    """
    from .models import log

    if not client.tebra_patient_id:
        raise TebraError(
            f"{client.name} has no Tebra chart yet. Create the chart first - "
            f"filing a document is not the place to create a medical record.")

    api = connect(db)
    document_id = api.create_document(
        practice_id=api.creds.practice_id, patient_id=client.tebra_patient_id,
        name=name, file_name=file_name, content=content, label=label, notes=notes)
    log(db, f"Document filed to Tebra chart {client.tebra_patient_id} "
            f"({name}, {len(content)} bytes)", "client", client.id,
        user_id=getattr(user, "id", None), ip=ip)
    return document_id


def push_appointment(db, booking, *, service_location_id: str = "",
                     user=None, ip: str = "") -> str:
    """Mirror a locally-booked appointment into Tebra's schedule."""
    from .models import Client, log

    client = db.get(Client, booking.client_id)
    if client is None or not client.tebra_patient_id:
        raise TebraError("That patient has no Tebra chart yet, so there is "
                         "nobody in Tebra to book the appointment for.")

    api = connect(db)
    location = service_location_id or api.creds.practice_id
    payload = appointment_payload(
        booking, practice_id=api.creds.practice_id,
        service_location_id=location, patient_tebra_id=client.tebra_patient_id)
    appointment_id = api.create_appointment(payload)
    log(db, f"Appointment mirrored to Tebra (id {appointment_id})", "client",
        client.id, user_id=getattr(user, "id", None), ip=ip)
    return appointment_id
