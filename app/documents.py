"""Scans, faxes and outside records - the paper a practice receives.

The gap this fills: an insurance card photographed at the desk, a referral
letter, a discharge summary faxed from a hospital. All of it currently has
nowhere to go, so it lives in somebody's email or a shared folder, which is
where records go to be lost.

**The bytes are stored, not just the metadata.** It is tempting to keep a row
saying a document exists and leave the file on disk - until the app is running
somewhere with a read-only filesystem, which is exactly where this one runs. A
row that says "insurance card received" pointing at a file nobody can open is
worse than no row: it reports coverage that cannot be produced when a claim is
questioned. So the file goes in the database, the same way the imported IntakeQ
PDFs already do.

**Unfiled is a real state, not a mistake.** A fax arrives before anybody knows
whose it is. Forcing a patient to be chosen at upload means somebody guesses,
and a discharge summary on the wrong chart is worse than one on no chart. So a
document may be filed later, and the queue of unfiled ones is a screen people
work through rather than an error nobody cleared.
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime

from sqlalchemy import (Boolean, Date, DateTime, ForeignKey, Integer,
                        LargeBinary, String, Text)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .models import Base

#  Generous for a scan, small enough that one upload cannot exhaust memory on a
#  serverless instance. A multi-page fax of a hospital discharge runs to a few
#  megabytes; anything past this is a mistake or an attack, and both deserve the
#  same answer.
MAX_BYTES = 12 * 1024 * 1024

#  What may be stored, by what the browser claims and what the bytes actually
#  begin with. The claim alone is not enough - it is set by the client - so both
#  are checked and the magic number wins.
ALLOWED = {
    "application/pdf": "pdf",
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/gif": "gif",
    "image/webp": "webp",
    "image/tiff": "tiff",
    "text/plain": "txt",
}

MAGIC = [
    (b"%PDF-", "application/pdf"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"II*\x00", "image/tiff"),
    (b"MM\x00*", "image/tiff"),
]

LABELS = [
    "Insurance card", "Photo ID", "Referral letter", "Outside records",
    "Lab or imaging report", "Signed consent", "Correspondence", "Other",
]


def sniff(head: bytes, claimed: str) -> str | None:
    """The real media type, or None if it is not one we accept.

    The magic number is trusted over the declared type, because the declared
    type comes from the client. A file called scan.pdf whose bytes are a Windows
    executable is not a scan, and storing it because the browser said so is how
    a records system becomes a malware host.

    Plain text has no magic number, so it is accepted on the declared type
    alone - and only after everything with a signature has failed to match,
    which keeps the loose case from swallowing the strict ones.
    """
    for prefix, media in MAGIC:
        if head.startswith(prefix):
            return media
    if claimed == "text/plain":
        #  Reject anything with a null byte: real text has none, and a binary
        #  that claims to be text is the thing this check exists for.
        return None if b"\x00" in head else "text/plain"
    if claimed == "image/webp" and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return None


class Document(Base):
    """One received file, and where it belongs.

    `sha256` is stored so the same fax arriving twice can be recognised. It is
    not enforced as unique: a practice legitimately receives the same form for
    two patients, and refusing the second is worse than showing both.
    """

    __tablename__ = "documents"
    id: Mapped[int] = mapped_column(primary_key=True)

    #  Nullable on purpose - see the module docstring on unfiled documents.
    client_id: Mapped[int | None] = mapped_column(
        ForeignKey("clients.id"), nullable=True, index=True)

    name: Mapped[str] = mapped_column(String(255))
    file_name: Mapped[str] = mapped_column(String(255), default="")
    media_type: Mapped[str] = mapped_column(String(64), default="")
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    sha256: Mapped[str] = mapped_column(String(64), default="", index=True)
    content: Mapped[bytes] = mapped_column(LargeBinary)

    label: Mapped[str] = mapped_column(String(64), default="Other")
    received_on: Mapped[date] = mapped_column(Date, default=date.today)
    received_from: Mapped[str] = mapped_column(String(255), default="")
    notes: Mapped[str] = mapped_column(Text, default="")

    #  Processed means a human has dealt with it - filed it, acted on it, or
    #  decided nothing is needed. Separate from being attached to a patient,
    #  because a document can be on the right chart and still be waiting for
    #  somebody to read it.
    processed: Mapped[bool] = mapped_column(Boolean, default=False)
    processed_by: Mapped[str] = mapped_column(String(160), default="")
    processed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    uploaded_by: Mapped[str] = mapped_column(String(160), default="")
    uploaded_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    client: Mapped["Client | None"] = relationship()               # noqa: F821

    @property
    def is_filed(self) -> bool:
        return self.client_id is not None

    @property
    def extension(self) -> str:
        return ALLOWED.get(self.media_type, "bin")

    @property
    def size_human(self) -> str:
        size = float(self.size_bytes or 0)
        for unit in ("bytes", "KB", "MB"):
            if size < 1024 or unit == "MB":
                return f"{size:.0f} {unit}" if unit == "bytes" else f"{size:.1f} {unit}"
            size /= 1024
        return f"{size:.1f} MB"

    @property
    def is_image(self) -> bool:
        return (self.media_type or "").startswith("image/")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def download_name(doc: "Document") -> str:
    """The filename to serve this document under.

    The extension is appended only if the name does not already carry a
    sensible one. Without this a document uploaded as "insurance card.pdf" is
    served as "insurance card.pdf.pdf" - harmless but sloppy, and the kind of
    thing that makes a records system look untended.

    The stored name is still authoritative for what the document *is*; this
    only decides what the browser calls the file.
    """
    stem = safe_name(doc.name)
    want = ALLOWED.get(doc.media_type, "")
    if not want:
        return stem
    #  Compare against every extension we know, not just the matching one: a
    #  JPEG named "scan.jpeg" should not become "scan.jpeg.jpg" either.
    known = set(ALLOWED.values()) | {"jpeg", "tif"}
    head, _, tail = stem.rpartition(".")
    if head and tail.lower() in known:
        stem = head
    return f"{stem}.{want}"


def safe_name(raw: str) -> str:
    """A filename safe to put in a Content-Disposition header.

    Quotes and newlines are stripped rather than escaped: a header injection
    through a filename is a real attack, and no legitimate scan is named in a
    way this breaks.
    """
    cleaned = "".join(c for c in (raw or "") if c.isalnum() or c in " ._-()")
    return cleaned.strip()[:120] or "document"
