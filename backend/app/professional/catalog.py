"""The professional documents a user may see, counted once (round 10 U8: R24; KTD11).

Identical text stored more than once is one text to search and to list: versions of professional documents whose
``text_digest`` (``sections``) and stated edition (``profile``) are both the same collapse to the earliest-created visible
one. Different editions never collapse: the same digest with another edition, or one edition stated and another not,
are different documents. Nothing is deleted or hidden from the owner — the copies stay stored, searchable by their own
document id, and listed under the text they repeat ("אותו נוסח כמו …"). Only versions the caller may see take part
(row-level security answers the query), so the earliest-created *visible* version is the one a user is shown.

A version whose digest was not derived yet is derived here (``sections.ensure_in``: lazily, like every other reader of
a standard); one whose digest cannot be had stays unique."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import Connection, text

from app.platform.source_kinds import PROFESSIONALS_SQL
from app.professional import sections
from app.professional.profile import PROFILE_VERSION

_SQL = (
    "SELECT d.id AS document_id, d.title, d.source_kind, v.id AS version_id, v.created_at,"
    " v.ingestion->>'text_digest' AS digest, p.fields->'edition' AS edition FROM documents d"
    " JOIN document_versions v ON v.document_id = d.id AND v.is_current AND v.status IN ('ready', 'needs_review')"
    " LEFT JOIN standard_profiles p ON p.version_id = v.id AND p.reading_id = COALESCE(v.ingestion->>'reading_id', '')"
    f" AND p.profile_version = '{PROFILE_VERSION}'"
    f" WHERE d.deleted_at IS NULL AND {PROFESSIONALS_SQL} ORDER BY v.created_at, v.id")


@dataclass(frozen=True)
class Entry:
    """One visible current professional version: its document, its text digest (None: not available) and the edition
    it states (None: it states none)."""

    document_id: UUID
    version_id: UUID
    title: str
    source_kind: str
    created_at: datetime
    digest: str | None
    edition: str | None

    @property
    def key(self) -> tuple[str, str | None] | None:
        """What makes two entries the same text: the digest and the stated edition, as written without extra spaces."""
        return None if not self.digest else (self.digest, " ".join(self.edition.split()) if self.edition else None)


def _edition(raw) -> str | None:
    return raw.get("value") if isinstance(raw, dict) and raw.get("status") == "stated" else None


def entries(conn: Connection, derive: bool = True) -> list[Entry]:
    """Every visible current professional version, earliest created first. ``derive``: derive the digest of a version
    that has none yet (a reader that must write nothing passes False: such a version counts as unique)."""
    rows = conn.execute(text(_SQL)).all()
    digests: dict[UUID, str] = {}
    for r in rows:
        if not r.digest and derive:
            sections.ensure_in(conn, r.version_id)  # stores the digest with the sections
            found = conn.execute(text("SELECT ingestion->>'text_digest' FROM document_versions WHERE id = :v"),
                                 {"v": r.version_id}).scalar_one_or_none()
            if found:
                digests[r.version_id] = found
    return [Entry(r.document_id, r.version_id, r.title, r.source_kind, r.created_at,
                  r.digest or digests.get(r.version_id), _edition(r.edition)) for r in rows]


def canonical(found: list[Entry]) -> dict[UUID, Entry]:
    """Each entry's version -> the earliest-created entry of its text (itself when it is the first, or unique)."""
    first: dict[tuple, Entry] = {}
    out: dict[UUID, Entry] = {}
    for e in found:  # earliest first
        key = e.key
        out[e.version_id] = first.setdefault(key, e) if key is not None else e
    return out


def copies(found: list[Entry]) -> dict[UUID, list[Entry]]:
    """The later copies of each text, under the version id of the entry that stands for it."""
    heads = canonical(found)
    out: dict[UUID, list[Entry]] = {}
    for e in found:
        head = heads[e.version_id]
        if head.version_id != e.version_id:
            out.setdefault(head.version_id, []).append(e)
    return out


def excluded(found: list[Entry]) -> tuple[UUID, ...]:
    """The versions a search of the professional pool leaves out: every later copy of a text."""
    heads = canonical(found)
    return tuple(e.version_id for e in found if heads[e.version_id].version_id != e.version_id)
